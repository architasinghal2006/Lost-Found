import db

# visibility: 'public' = everyone, 'claimant' = that claim's claimant + staff, 'staff' = finder + admin only.


def log_event(conn, item_id, event_type, actor, visibility="public", detail="", claim_id=None):
    # Runs inside the caller's transaction, so the event commits or rolls back with the change (FR-133).
    conn.execute(
        "INSERT INTO item_events (item_id, claim_id, event_type, actor, visibility, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (item_id, claim_id, event_type, actor, visibility, detail, db.now()),
    )


def list_events(conn, item_id, viewer):
    finder_id = conn.execute("SELECT finder_id FROM items WHERE id=?", (item_id,)).fetchone()["finder_id"]
    rows = conn.execute(
        "SELECT item_events.*, claims.claimant_id FROM item_events"
        " LEFT JOIN claims ON claims.id = item_events.claim_id"
        " WHERE item_events.item_id=? ORDER BY item_events.id",
        (item_id,),
    ).fetchall()
    is_staff = viewer is not None and (viewer["role"] == "admin" or viewer["id"] == finder_id)
    visible = []
    for row in rows:
        own_claim = viewer is not None and row["claimant_id"] == viewer["id"]
        if row["visibility"] == "public" or is_staff or (row["visibility"] == "claimant" and own_claim):
            visible.append(row)
    return visible
