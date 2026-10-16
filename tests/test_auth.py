import pytest

import auth


def test_password_is_hashed_not_stored_plain(conn):
    user_id = auth.register(conn, "alice", "secret123")
    stored = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()[0]
    assert "secret123" not in stored
    assert auth.verify_password("secret123", stored)
    assert not auth.verify_password("wrong", stored)


def test_login_returns_session_for_correct_password(conn):
    auth.register(conn, "alice", "secret123")
    token = auth.login(conn, "alice", "secret123")
    assert auth.get_user(conn, token)["username"] == "alice"


def test_login_fails_with_wrong_password(conn):
    auth.register(conn, "alice", "secret123")
    with pytest.raises(auth.AuthError):
        auth.login(conn, "alice", "nope")


def test_duplicate_username_rejected(conn):
    auth.register(conn, "alice", "secret123")
    with pytest.raises(auth.AuthError):
        auth.register(conn, "alice", "other123")


def test_logout_invalidates_session(conn):
    auth.register(conn, "alice", "secret123")
    token = auth.login(conn, "alice", "secret123")
    auth.logout(conn, token)
    assert auth.get_user(conn, token) is None


def test_role_defaults_to_user_and_admin_possible(conn):
    auth.register(conn, "u1", "secret123")
    auth.register(conn, "boss", "secret123", role="admin")
    roles = dict(conn.execute("SELECT username, role FROM users").fetchall())
    assert roles == {"u1": "user", "boss": "admin"}
