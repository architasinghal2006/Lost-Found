import hashlib
import hmac
import secrets
import sqlite3

import db

ITERATIONS = 100_000


class AuthError(Exception):
    pass


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password, stored):
    salt = stored.split("$")[0]
    return hmac.compare_digest(hash_password(password, salt), stored)


def register(conn, username, password, role="user"):
    username = username.strip()
    if not username or len(password) < 6:
        raise AuthError("Choose a username and a password of at least 6 characters.")
    try:
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role, db.now()),
            )
    except sqlite3.IntegrityError:
        raise AuthError("That username is already taken.")
    return cur.lastrowid


def login(conn, username, password):
    user = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if user is None or not verify_password(password, user["password_hash"]):
        raise AuthError("Wrong username or password.")
    token = secrets.token_urlsafe(32)
    with db.transaction(conn):
        conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                     (token, user["id"], db.now()))
    return token


def get_user(conn, token):
    if not token:
        return None
    return conn.execute(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token=?",
        (token,),
    ).fetchone()


def logout(conn, token):
    with db.transaction(conn):
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
