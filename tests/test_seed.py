import db
import items
import seed


def test_seed_creates_demo_world_and_aged_item_expires(tmp_path, monkeypatch):
    path = tmp_path / "seed.db"
    monkeypatch.setenv("LF_DB", str(path))
    seed.main()
    conn = db.connect(str(path))
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == len(seed.ITEMS) + 1
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == len(seed.USERS)
    expired = items.expire_items(conn)
    assert expired == [len(seed.ITEMS) + 1]


def test_seed_keeps_a_backup_of_an_existing_database(tmp_path, monkeypatch):
    path = tmp_path / "seed.db"
    path.write_text("old data")
    monkeypatch.setenv("LF_DB", str(path))
    seed.main()
    assert (tmp_path / "seed.db.bak").read_text() == "old data"
