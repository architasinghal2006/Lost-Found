import pytest

import auth
import claim_views
import claims
import db
import embedder
import items
from helpers import make_user, png_bytes

ATTRIBUTES = [
    ("What colour is the strap?", "red", 5),
    ("What is engraved on the lid?", "AB 2024", 4),
    ("What brand is it?", "Nalgene", 3),
    ("What sticker is on the side?", "a cat", 2),
    ("What colour is the cap?", "blue", 1),
]
TRUTH = {label: answer for label, answer, _ in ATTRIBUTES}


@pytest.fixture(autouse=True)
def test_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("LF_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setattr(auth, "ITERATIONS", 1000)  # fast hashing in tests only


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


def register(conn, finder, caption="black metal water bottle", attributes=ATTRIBUTES, **overrides):
    fields = {"description": "Black bottle found near the desks", "category": "bottle",
              "zone": "library", "date_found": "2026-10-01"}
    fields.update(overrides)
    return items.register_item(conn, embedder.StubEmbedder(), finder, png_bytes(caption), fields["description"],
                               fields["category"], fields["zone"], fields["date_found"], attributes)


@pytest.fixture
def world(conn):
    users = {name: make_user(conn, name) for name in ["finder", "owner", "fraud", "fraud2", "fraud3"]}
    users["admin"] = make_user(conn, "admin", role="admin")
    users["item"] = register(conn, users["finder"])
    return users


def answer_all(conn, claim_id, user, answer_for):
    # Keeps answering until the claim stops asking. answer_for(question_text) -> answer string.
    claims.start_verification(conn, claim_id, user)
    while True:
        question = claim_views.current_question(conn, claim_id, user)
        if question is None:
            break
        claims.answer_question(conn, claim_id, question["id"], user, answer_for(question["question_text"]))
    return claim_views.get_claim(conn, claim_id)["state"]


def budget(conn, column, owner_id):
    return conn.execute(f"SELECT * FROM budgets WHERE {column}=?", (owner_id,)).fetchone()
