# Demo data: users, found items with drawn placeholder photos, and one aged item for the expiry sweep.
# Run: python seed.py   (an existing database is renamed to *.bak first, never silently lost)
import io
import os
import shutil
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, PngImagePlugin

import auth
import db
import embedder
import items

USERS = [("admin", "admin123", "admin"), ("finder", "finder123", "user"), ("owner", "owner123", "user"),
         ("mallory", "mallory123", "user"), ("trudy", "trudy123", "user")]

# (drawing, colour, caption the stub "sees", public description, category, zone, date found, private details)
ITEMS = [
    ("bottle", "#222222", "black metal water bottle dented lid", "Dark drinks bottle", "bottle", "library", "2026-10-10",
     [("What is written on the sticker on the bottom?", "property of ravi", 5), ("What colour is the inside of the lid?", "orange", 4),
      ("What brand is it?", "Milton", 3), ("Is there a dent, and where?", "near the lid", 2), ("How many litres is it?", "1", 1)]),
    ("bottle", "#262626", "black metal water bottle carry loop", "Black flask", "bottle", "gym", "2026-10-12",
     [("What is engraved on the base?", "JS 2025", 5), ("What colour is the carry loop?", "grey", 3), ("What is inside it?", "green tea", 2)]),
    ("bottle", "#1e1e1e", "black metal water bottle steel cap", "Insulated bottle, black", "bottle", "cafeteria", "2026-09-20",
     [("What sticker is on the side?", "a cactus", 5), ("What brand is it?", "Cello", 3), ("What colour is the cap seal?", "white", 2)]),
    ("keys", "#c0c0c0", "silver keys on ring with blue orange enamel bird charm", "Metal keys, 3, with blue and orange charm",
     "keys", "car_park", "2026-10-11",
     [("What animal is the keyring charm?", "bird", 5), ("What is written on the car key fob?", "honda", 4),
      ("What colour is the smallest key?", "brass", 3), ("What is the charm made of?", "enamel", 2), ("How many keys are there?", "3", 1)]),
    ("umbrella", "#1f3a93", "navy blue folding umbrella", "Folding umbrella", "umbrella", "lecture_hall", "2026-10-09",
     [("What pattern is on the inside?", "white stars", 5), ("What colour is the handle?", "brown", 3), ("What colour is the sleeve?", "navy", 2)]),
    ("backpack", "#c0392b", "red backpack rucksack with laptop sleeve", "Red bag", "bag", "hostel", "2026-10-08",
     [("What is in the front pocket?", "a blue pen", 5), ("What brand is the bag?", "Wildcraft", 4), ("What keychain hangs on the zip?", "a panda", 3)]),
    ("headphones", "#f2f2f2", "white wireless over ear headphones", "Headphones", "electronics", "library", "2026-10-13",
     [("What name is the device paired as?", "neha's headphones", 5), ("What brand are they?", "Sony", 3), ("Which ear cup is scratched?", "left", 2)]),
    ("card", "#27ae60", "green lanyard with plastic id card holder", "ID holder on a lanyard", "id_card", "cafeteria", "2026-10-14",
     [("What name is on the ID card?", "Arjun Mehta", 5), ("What is the roll number on the card?", "21CS044", 5), ("What else is in the holder?", "metro card", 2)]),
    ("glasses", "#8e5b3a", "brown leather glasses case spectacles", "Glasses case", "glasses", "lecture_hall", "2026-10-07",
     [("What colour are the frames inside?", "tortoiseshell", 5), ("What is written inside the case?", "lenskart", 3), ("Is there a cleaning cloth? What colour?", "blue", 2)]),
    ("calculator", "#7f8c8d", "grey scientific calculator", "Calculator", "stationery", "lab_block", "2026-10-12",
     [("What name is scratched on the back?", "priya", 5), ("What model is it?", "fx-991ES", 3), ("What colour is the cover?", "black", 2)]),
    ("wallet", "#3b2a20", "black leather wallet bifold", "Wallet", "wallet", "sports_ground", "2026-10-05",
     [("Whose photo is inside?", "my dog", 5), ("Which bank card is inside?", "SBI", 4), ("How much cash, roughly?", "200 rupees", 2)]),
    ("hoodie", "#95a5a6", "grey hooded sweatshirt hoodie", "Grey top", "clothing", "gym", "2026-10-06",
     [("What size is the label?", "M", 3), ("What is printed on the back?", "IIT fest 2025", 5), ("What is in the pocket?", "a receipt", 2)]),
]
AGED_ITEM = ("umbrella", "#f1c40f", "yellow umbrella", "Yellow umbrella", "umbrella", "car_park", "2026-09-01",
             [("What is written on the handle?", "rain or shine", 5)])


def draw(kind, colour):
    image = Image.new("RGB", (512, 512), "#eef1f5")
    d = ImageDraw.Draw(image)
    if kind == "bottle":
        d.rounded_rectangle((196, 120, 316, 450), 30, fill=colour)
        d.rectangle((220, 70, 292, 125), fill="#555555")
    elif kind == "keys":
        d.ellipse((150, 120, 270, 240), outline=colour, width=14)
        for x in (250, 280, 310):
            d.rectangle((x, 220, x + 26, 420), fill=colour)
        d.polygon([(330, 160), (400, 130), (380, 200)], fill="#2e86de")
        d.ellipse((360, 140, 395, 175), fill="#e67e22")
    elif kind == "umbrella":
        d.pieslice((96, 120, 416, 400), 180, 360, fill=colour)
        d.line((256, 260, 256, 440), fill="#5d4037", width=10)
        d.arc((226, 410, 286, 470), 0, 180, fill="#5d4037", width=10)
    elif kind == "backpack":
        d.rounded_rectangle((150, 120, 362, 440), 50, fill=colour)
        d.rounded_rectangle((190, 300, 322, 410), 20, fill="#922b21")
        d.arc((200, 70, 312, 170), 180, 360, fill="#333333", width=12)
    elif kind == "headphones":
        d.arc((136, 110, 376, 350), 180, 360, fill="#999999", width=18)
        d.rounded_rectangle((110, 220, 180, 340), 25, fill=colour, outline="#999999", width=4)
        d.rounded_rectangle((332, 220, 402, 340), 25, fill=colour, outline="#999999", width=4)
    elif kind == "card":
        d.line((256, 40, 256, 180), fill=colour, width=14)
        d.rounded_rectangle((156, 180, 356, 430), 16, fill="#ffffff", outline=colour, width=8)
        d.rectangle((190, 220, 270, 310), fill="#bbbbbb")
    elif kind == "glasses":
        d.rounded_rectangle((110, 190, 402, 330), 70, fill=colour)
        d.line((140, 260, 372, 260), fill="#5a3a25", width=6)
    elif kind == "calculator":
        d.rounded_rectangle((166, 90, 346, 430), 18, fill=colour)
        d.rectangle((190, 115, 322, 180), fill="#c8e6c9")
        for row in range(4):
            for col in range(3):
                x, y = 192 + col * 46, 205 + row * 52
                d.rectangle((x, y, x + 34, y + 36), fill="#2c3e50")
    elif kind == "wallet":
        d.rounded_rectangle((116, 170, 396, 360), 20, fill=colour)
        d.line((256, 170, 256, 360), fill="#1a120d", width=6)
    elif kind == "hoodie":
        d.polygon([(176, 140), (336, 140), (396, 260), (346, 280), (346, 440), (166, 440), (166, 280), (116, 260)], fill=colour)
        d.ellipse((216, 100, 296, 170), fill="#7f8c8d")
    return image


def photo_bytes(kind, colour, caption):
    # The caption is a PNG text chunk. Only the stub embedder reads it; CLIP sees the pixels.
    info = PngImagePlugin.PngInfo()
    info.add_text("caption", caption)
    out = io.BytesIO()
    draw(kind, colour).save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def main():
    path = os.environ.get("LF_DB", "lostfound.db")
    if os.path.exists(path):
        shutil.move(path, path + ".bak")
        print(f"Existing database moved to {path}.bak")
    shutil.rmtree(items.upload_dir(), ignore_errors=True)

    conn = db.connect(path)
    db.init_db(conn)
    users = {}
    for username, password, role in USERS:
        user_id = auth.register(conn, username, password, role)
        users[username] = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    model = embedder.get_embedder()
    for kind, colour, caption, description, category, zone, found, attributes in ITEMS + [AGED_ITEM]:
        item_id = items.register_item(conn, model, users["finder"], photo_bytes(kind, colour, caption),
                                      description, category, zone, found, attributes)
        print(f"  item {item_id:2}: {description} ({category}, {zone})")

    # Pretend the last item was registered 40 days ago, so the startup sweep expires it.
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    with db.transaction(conn):
        conn.execute("UPDATE items SET created_at=? WHERE id=?", (old, item_id))
    conn.close()

    print(f"\nSeeded {len(ITEMS) + 1} items with the {model.name} embedder into {path}.")
    print("Demo logins (username / password):")
    for username, password, role in USERS:
        print(f"  {username:8} / {password:11} {role}")


if __name__ == "__main__":
    main()
