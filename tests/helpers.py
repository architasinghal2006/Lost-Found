import io

from PIL import Image, PngImagePlugin

import auth
import embedder


def png_bytes(caption=None, colour=(30, 30, 30)):
    # A tiny PNG; the stub embedder reads the optional caption chunk as "what the photo shows".
    image = Image.new("RGB", (8, 8), colour)
    info = PngImagePlugin.PngInfo()
    if caption:
        info.add_text("caption", caption)
    out = io.BytesIO()
    image.save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def make_user(conn, name, role="user"):
    user_id = auth.register(conn, name, "secret123", role=role)
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def raw_item(conn, finder_id, caption, category="bottle", zone="library", date_found="2026-10-01"):
    vector = embedder.StubEmbedder().embed_image(png_bytes(caption))
    cur = conn.execute(
        "INSERT INTO items (finder_id, photo_path, public_description, category, zone, date_found, embedding, created_at)"
        " VALUES (?, 'p.png', ?, ?, ?, ?, ?, '2026-10-01 00:00:00')",
        (finder_id, caption, category, zone, date_found, embedder.to_blob(vector)),
    )
    return cur.lastrowid
