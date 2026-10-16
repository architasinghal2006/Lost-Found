import io
import os
import uuid
from datetime import date, datetime, timedelta, timezone

from PIL import Image

import claims
import db
import embedder
import events

CATEGORIES = ["bag", "bottle", "clothing", "electronics", "glasses", "id_card", "jewellery",
              "keys", "stationery", "umbrella", "wallet", "other"]
ZONES = ["library", "cafeteria", "lecture_hall", "lab_block", "gym", "hostel", "car_park", "sports_ground"]
PHOTO_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
EXPIRY_DAYS = 30  # OI-14
STATES_IN_VERIFICATION = ("VERIFYING", "VERIFIED", "MANUAL_REVIEW", "DISPUTED")


class ItemError(Exception):
    pass


def upload_dir():
    return os.environ.get("LF_UPLOADS", os.path.join("static", "uploads"))


def check_public_fields(description, category, zone, date_found):
    if not (description or "").strip():
        raise ItemError("A public description is required.")
    if category not in CATEGORIES:
        raise ItemError("Choose a category from the list.")
    if zone not in ZONES:
        raise ItemError("Choose a zone from the list.")
    try:
        found = date.fromisoformat(date_found or "")
    except ValueError:
        raise ItemError("Enter the date found as YYYY-MM-DD.")
    if found > date.today():
        raise ItemError("The date found cannot be in the future.")


def check_photo(photo_bytes):
    # Returns the file extension. Only real raster images are accepted (an SVG could carry script).
    if not photo_bytes:
        raise ItemError("A photograph is required.")
    try:
        image = Image.open(io.BytesIO(photo_bytes))
        image_format = image.format
        image.verify()
    except Exception:
        raise ItemError("The photo could not be read as an image.")
    if image_format not in PHOTO_FORMATS:
        raise ItemError("Upload a JPEG, PNG, WebP or GIF photo.")
    return PHOTO_FORMATS[image_format]


def clean_attributes(rows):
    # rows: list of (label, answer, weight). Fully blank rows are skipped.
    cleaned = []
    for label, answer, weight in rows:
        label, answer = (label or "").strip(), (answer or "").strip()
        if not label and not answer:
            continue
        if not label or not answer:
            raise ItemError("Each private detail needs both a question and an answer.")
        try:
            weight = int(weight)
        except (TypeError, ValueError):
            raise ItemError("Weights must be whole numbers from 1 to 5.")
        if not 1 <= weight <= 5:
            raise ItemError("Weights must be whole numbers from 1 to 5.")
        cleaned.append((label, answer, weight))
    if not cleaned:
        raise ItemError("Add at least one private detail, or the owner can never be verified.")
    return cleaned


def register_item(conn, model, finder, photo_bytes, description, category, zone, date_found, attributes):
    check_public_fields(description, category, zone, date_found)
    extension = check_photo(photo_bytes)
    attributes = clean_attributes(attributes)
    try:
        vector = model.embed_image(photo_bytes)
    except Exception:
        # OI-18: never accept an item that search could not find.
        raise ItemError("The photo could not be processed, so the item was not registered. Please try again.")

    filename = f"{uuid.uuid4().hex}.{extension}"
    os.makedirs(upload_dir(), exist_ok=True)
    with open(os.path.join(upload_dir(), filename), "wb") as f:
        f.write(photo_bytes)

    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO items (finder_id, photo_path, public_description, category, zone, date_found, embedding, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (finder["id"], filename, description.strip(), category, zone, date_found,
             embedder.to_blob(vector), db.now()),
        )
        item_id = cur.lastrowid
        claims.insert_private_attributes(conn, item_id, attributes)
        claims.create_item_budget(conn, item_id)
        events.log_event(conn, item_id, "item_registered", finder["username"], detail="found item registered")
    return item_id


def get_item(conn, item_id):
    item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item is None:
        raise ItemError("No such item.")
    return item


def withdraw_item(conn, item_id, user, reason):
    # Closure path 2: finder withdrawal (an administrator may do it too, FR-091).
    item = get_item(conn, item_id)
    if user["id"] != item["finder_id"] and user["role"] != "admin":
        raise ItemError("Only the finder or an administrator can close this item.")
    if reason != "WITHDRAWN":
        raise ItemError("Choose the reason code WITHDRAWN to withdraw this item.")
    try:
        with db.transaction(conn):
            return claims.close_item_in_tx(conn, item_id, "WITHDRAWN", user["username"])
    except claims.ClaimError as error:
        raise ItemError(str(error))


def expire_items(conn, now=None):
    # Closure path 3: the scheduler. Called at startup and from the admin page.
    now = now or datetime.now(timezone.utc)
    claims.expire_stale_claims(conn, now)
    cutoff = (now - timedelta(days=EXPIRY_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM items WHERE status='active' AND created_at < ? ORDER BY id", (cutoff,))]
    expired = []
    placeholders = ",".join("?" * len(STATES_IN_VERIFICATION))
    for item_id in old_ids:
        with db.transaction(conn):
            busy = conn.execute(
                f"SELECT 1 FROM claims WHERE item_id=? AND state IN ({placeholders})",
                (item_id, *STATES_IN_VERIFICATION),
            ).fetchone()
            if busy:
                continue  # FR-112: never expire an item with a claim in verification
            if claims.close_item_in_tx(conn, item_id, "EXPIRED", "scheduler"):
                expired.append(item_id)
    return expired
