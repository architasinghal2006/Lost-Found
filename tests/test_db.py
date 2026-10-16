import sqlite3

import pytest

import db


def insert_user(conn):
    conn.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES ('u', 'x', 'user', 't')")
    return conn.execute("SELECT id FROM users").fetchone()["id"]


def insert_item(conn, finder_id):
    conn.execute(
        "INSERT INTO items (finder_id, photo_path, public_description, category, zone, date_found, embedding, created_at)"
        " VALUES (?, 'p.png', 'desc', 'bottle', 'library', '2026-10-01', x'00', 't')",
        (finder_id,),
    )
    return conn.execute("SELECT id FROM items").fetchone()["id"]


def test_all_tables_exist(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"users", "sessions", "items", "private_attributes", "claims", "challenges",
                "challenge_responses", "verification_records", "budgets", "item_events"}
    assert expected <= names


def test_new_item_is_active(conn):
    item_id = insert_item(conn, insert_user(conn))
    assert conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()["status"] == "active"


def test_items_cannot_be_deleted(conn):
    item_id = insert_item(conn, insert_user(conn))
    with pytest.raises(sqlite3.DatabaseError, match="soft-deleted"):
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))


def test_resolved_item_needs_timestamp_and_reason(conn):
    # INV-5
    item_id = insert_item(conn, insert_user(conn))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE items SET status='resolved' WHERE id=?", (item_id,))


def test_events_are_append_only(conn):
    item_id = insert_item(conn, insert_user(conn))
    conn.execute("INSERT INTO item_events (item_id, event_type, actor, visibility, created_at)"
                 " VALUES (?, 'x', 'u', 'public', 't')", (item_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE item_events SET event_type='y'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM item_events")


def test_budget_consumption_never_goes_down(conn):
    # INV-6 / NFR-PRIV-005
    item_id = insert_item(conn, insert_user(conn))
    conn.execute("INSERT INTO budgets (item_id, allowance, consumed_total) VALUES (?, 20, 5)", (item_id,))
    with pytest.raises(sqlite3.DatabaseError, match="never go down"):
        conn.execute("UPDATE budgets SET consumed_total=0")


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            insert_user(conn)
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_transaction_commits(conn):
    with db.transaction(conn):
        insert_user(conn)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
