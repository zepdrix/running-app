"""Optional password protection, for opening the app from your phone.

Set a password with `python -m runapp password`. Once set, every page and API
call needs a login; sessions last 90 days per device. The password is stored
only as a salted scrypt hash in data/auth.json.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta

from . import config

COOKIE = "runapp_session"
SESSION_DAYS = 90
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _auth_file():
    return config.DATA_DIR / "auth.json"


def enabled() -> bool:
    return _auth_file().exists()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)


def set_password(conn: sqlite3.Connection, password: str) -> None:
    salt = secrets.token_bytes(16)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _auth_file()
    path.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(password, salt).hex()}))
    path.chmod(0o600)
    conn.execute("DELETE FROM auth_sessions")  # a new password signs every device out
    conn.commit()


def remove_password(conn: sqlite3.Connection) -> None:
    _auth_file().unlink(missing_ok=True)
    conn.execute("DELETE FROM auth_sessions")
    conn.commit()


def check_password(password: str) -> bool:
    if not enabled():
        return False
    data = json.loads(_auth_file().read_text())
    return hmac.compare_digest(_hash(password, bytes.fromhex(data["salt"])).hex(), data["hash"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(conn: sqlite3.Connection) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO auth_sessions (token_hash, expires_at) VALUES (?, ?)", (_token_hash(token), expires))
    conn.commit()
    return token


def valid_session(conn: sqlite3.Connection, token: str | None) -> bool:
    if not token:
        return False
    row = conn.execute("SELECT expires_at FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),)).fetchone()
    return bool(row) and row["expires_at"] > datetime.now().isoformat(timespec="seconds")


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))
        conn.commit()
