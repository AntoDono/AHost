"""Dashboard users and sessions (SQLite, owned by the ahost user)."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import Config

USER_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
MIN_PASSWORD = 12
_ph = PasswordHasher()  # argon2id with library defaults
_DUMMY = _ph.hash("timing-equalizer-not-a-password")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name    TEXT PRIMARY KEY,
    pw      TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user       TEXT NOT NULL REFERENCES users(name) ON DELETE CASCADE,
    created    INTEGER NOT NULL,
    expires    INTEGER NOT NULL,
    ip         TEXT
);
"""


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, cfg: Config, path: Path | None = None):
        self.path = path or Path(cfg.paths.state_dir) / "ui" / "ui.db"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.hours = cfg.ui.session_hours
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ---- users
    def add_user(self, name: str, password: str) -> None:
        if not USER_RE.match(name):
            raise ValueError(f"username must match {USER_RE.pattern}")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        with self._lock, self.db:
            if self.db.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
                raise ValueError(f"user {name} already exists")
            self.db.execute("INSERT INTO users VALUES (?,?,?)", (name, _ph.hash(password), time.strftime("%F %T")))

    def set_password(self, name: str, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        with self._lock, self.db:
            if not self.db.execute("UPDATE users SET pw=? WHERE name=?", (_ph.hash(password), name)).rowcount:
                raise ValueError(f"no user {name}")
            self.db.execute("DELETE FROM sessions WHERE user=?", (name,))

    def delete_user(self, name: str) -> None:
        with self._lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE user=?", (name,))
            self.db.execute("DELETE FROM users WHERE name=?", (name,))

    def users(self) -> list[dict]:
        return [{"name": n, "created": c} for n, c in self.db.execute("SELECT name, created FROM users ORDER BY name")]

    def verify(self, name: str, password: str) -> bool:
        row = self.db.execute("SELECT pw FROM users WHERE name=?", (name,)).fetchone()
        try:
            _ph.verify(row[0] if row else _DUMMY, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if not row:
            return False
        if _ph.check_needs_rehash(row[0]):
            with self._lock, self.db:
                self.db.execute("UPDATE users SET pw=? WHERE name=?", (_ph.hash(password), name))
        return True

    # ---- sessions
    def create_session(self, name: str, ip: str | None) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock, self.db:
            self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                            (_h(token), name, now, now + self.hours * 3600, ip))
        return token

    def session_user(self, token: str | None) -> str | None:
        if not token:
            return None
        row = self.db.execute("SELECT user, expires FROM sessions WHERE token_hash=?", (_h(token),)).fetchone()
        if not row or row[1] < time.time():
            return None
        return row[0]

    def end_session(self, token: str | None) -> None:
        if token:
            with self._lock, self.db:
                self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


class RateLimiter:
    """Failed logins: at most `limit` per key per `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self.hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self.hits.get(key, []) if now - t < self.window]
            self.hits[key] = hits
            return len(hits) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self.hits.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self.hits.pop(key, None)
