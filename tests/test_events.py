import auth
import events


def setup_item(conn):
    finder = auth.register(conn, "finder", "secret123")
    claimant = auth.register(conn, "claimant", "secret123")
    other = auth.register(conn, "other", "secret123")
    admin = auth.register(conn, "admin", "secret123", role="admin")
    conn.execute(
        "INSERT INTO items (finder_id, photo_path, public_description, category, zone, date_found, embedding, created_at)"
        " VALUES (?, 'p.png', 'd', 'bottle', 'library', '2026-10-01', x'00', 't')", (finder,))
    conn.execute("INSERT INTO claims (item_id, claimant_id, state, created_at) VALUES (1, ?, 'PENDING', 't')", (claimant,))
    users = {name: conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
             for name, uid in [("finder", finder), ("claimant", claimant), ("other", other), ("admin", admin)]}
    return users


def test_event_records_type_entity_actor_and_time(conn):
    # FR-123
    setup_item(conn)
    events.log_event(conn, 1, "item_registered", "finder")
    row = conn.execute("SELECT * FROM item_events").fetchone()
    for field in ("event_type", "item_id", "actor", "created_at"):
        assert row[field] is not None


def test_timeline_is_scoped_to_viewer(conn):
    # UI-016, NFR-SEC-004
    users = setup_item(conn)
    events.log_event(conn, 1, "item_registered", "finder")
    events.log_event(conn, 1, "claim_filed", "claimant", visibility="claimant", claim_id=1)
    events.log_event(conn, 1, "challenge_issued", "system", visibility="staff", claim_id=1)

    def types(viewer):
        return [e["event_type"] for e in events.list_events(conn, 1, viewer)]

    assert types(None) == ["item_registered"]
    assert types(users["other"]) == ["item_registered"]
    assert types(users["claimant"]) == ["item_registered", "claim_filed"]
    assert types(users["finder"]) == ["item_registered", "claim_filed", "challenge_issued"]
    assert types(users["admin"]) == ["item_registered", "claim_filed", "challenge_issued"]
