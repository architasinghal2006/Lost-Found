from datetime import datetime, timedelta, timezone

import pytest

import claims
import embedder
import items
from conftest import ATTRIBUTES, register
from helpers import make_user, png_bytes


def test_register_persists_active_item_with_embedding(conn):
    # FR-010, FR-015, FR-016
    finder = make_user(conn, "finder")
    item_id = register(conn, finder)
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "active"
    assert len(embedder.from_blob(row["embedding"])) == embedder.DIM


def test_register_logs_event_and_creates_item_budget(conn):
    finder = make_user(conn, "finder")
    item_id = register(conn, finder)
    assert conn.execute("SELECT event_type FROM item_events WHERE item_id=?", (item_id,)).fetchone()[0] == "item_registered"
    assert conn.execute("SELECT allowance FROM budgets WHERE item_id=?", (item_id,)).fetchone()[0] == claims.ITEM_BUDGET


@pytest.mark.parametrize("override", [
    {"category": ""}, {"category": "spaceship"}, {"zone": ""}, {"zone": "moon"},
    {"date_found": ""}, {"date_found": "not-a-date"}, {"date_found": "2999-01-01"}, {"description": " "},
])
def test_missing_or_invalid_metadata_is_rejected(conn, override):
    # FR-011
    finder = make_user(conn, "finder")
    with pytest.raises(items.ItemError):
        register(conn, finder, **override)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_photo_is_required_and_must_be_an_image(conn):
    finder = make_user(conn, "finder")
    for photo in [b"", b"not an image", b"<svg onload='alert(1)'></svg>"]:
        with pytest.raises(items.ItemError):
            items.register_item(conn, embedder.StubEmbedder(), finder, photo, "d", "bottle", "library",
                                "2026-10-01", ATTRIBUTES)


@pytest.mark.parametrize("attributes", [
    [], [("", "", 3)], [("Question?", "", 3)], [("Question?", "answer", 0)], [("Question?", "answer", 6)],
])
def test_private_attributes_are_validated(conn, attributes):
    finder = make_user(conn, "finder")
    with pytest.raises(items.ItemError):
        register(conn, finder, attributes=attributes)


def test_blank_attribute_rows_are_ignored(conn):
    finder = make_user(conn, "finder")
    item_id = register(conn, finder, attributes=[("Q?", "a", 2), ("", "", "3")])
    assert len(claims.attribute_lock_status(conn, item_id)) == 1


def test_failed_embedding_refuses_registration(conn):
    # OI-18: no invisible items
    class Broken:
        def embed_image(self, data):
            raise RuntimeError("model crashed")

    finder = make_user(conn, "finder")
    with pytest.raises(items.ItemError):
        items.register_item(conn, Broken(), finder, png_bytes(), "d", "bottle", "library", "2026-10-01", ATTRIBUTES)
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_finder_can_withdraw_and_repeat_is_harmless(conn, world):
    assert items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN") is True
    assert items.withdraw_item(conn, world["item"], world["finder"], "WITHDRAWN") is False
    row = conn.execute("SELECT * FROM items WHERE id=?", (world["item"],)).fetchone()
    assert (row["status"], row["closure_reason"]) == ("resolved", "WITHDRAWN")
    assert row["resolved_at"]
    closes = conn.execute("SELECT COUNT(*) FROM item_events WHERE event_type='item_closed'").fetchone()[0]
    assert closes == 1


def test_withdrawal_requires_reason_code(conn, world):
    # FR-093
    for reason in ["", None, "BORED", "HANDED_OVER", "EXPIRED"]:
        with pytest.raises(items.ItemError):
            items.withdraw_item(conn, world["item"], world["finder"], reason)


def test_admin_can_withdraw_but_claimant_cannot(conn, world):
    # FR-091
    with pytest.raises(items.ItemError):
        items.withdraw_item(conn, world["item"], world["owner"], "WITHDRAWN")
    assert items.withdraw_item(conn, world["item"], world["admin"], "WITHDRAWN") is True


def age_item(conn, item_id, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE items SET created_at=? WHERE id=?", (old, item_id))


def test_expiry_closes_old_items_only(conn, world):
    fresh = register(conn, world["finder"], caption="umbrella")
    age_item(conn, world["item"], 40)
    assert items.expire_items(conn) == [world["item"]]
    statuses = dict(conn.execute("SELECT id, closure_reason FROM items").fetchall())
    assert statuses == {world["item"]: "EXPIRED", fresh: None}


def test_expiry_skips_item_with_claim_in_verification(conn, world):
    # FR-112
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    age_item(conn, world["item"], 40)
    assert items.expire_items(conn) == []


def test_expiry_times_out_stale_verifying_claims_first(conn, world):
    # T5, then the item is free to expire
    claim_id = claims.open_claim(conn, world["item"], world["owner"])
    claims.start_verification(conn, claim_id, world["owner"])
    conn.execute("UPDATE challenges SET issued_at='2000-01-01 00:00:00'")
    age_item(conn, world["item"], 40)
    assert items.expire_items(conn) == [world["item"]]
    assert conn.execute("SELECT state FROM claims WHERE id=?", (claim_id,)).fetchone()[0] == "EXPIRED"
