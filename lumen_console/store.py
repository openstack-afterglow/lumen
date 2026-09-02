"""Local-only user and session persistence for the Lumen console."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_PASSWORD_ITERATIONS = 310_000
_SESSION_TTL = timedelta(days=7)


@dataclass(frozen=True)
class LocalUser:
    id: int
    username: str


class LocalUserStore:
    """Small SQLite auth store; upstream Lumen credentials never enter this database."""

    def __init__(self, database_path: str | Path):
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS console_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS console_sessions (
                    token_hash BLOB PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES console_users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL
                );
                """
            )

    def needs_bootstrap(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM console_users LIMIT 1").fetchone() is None

    def bootstrap(self, username: str, password: str) -> LocalUser:
        if not self.needs_bootstrap():
            raise ValueError("local console already has a user")
        return self._create_user(username, password)

    def _create_user(self, username: str, password: str) -> LocalUser:
        normalized_username = username.strip()
        if not 3 <= len(normalized_username) <= 64:
            raise ValueError("username must contain 3 to 64 characters")
        if len(password) < 10:
            raise ValueError("password must contain at least 10 characters")
        salt = secrets.token_bytes(16)
        digest = self._password_digest(password, salt)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO console_users (username, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (normalized_username, salt, digest, datetime.now(UTC).isoformat()),
                )
                return LocalUser(id=int(cursor.lastrowid), username=normalized_username)
        except sqlite3.IntegrityError as exc:
            raise ValueError("username is already in use") from exc

    def authenticate(self, username: str, password: str) -> LocalUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, username, password_salt, password_hash FROM console_users WHERE username = ?",
                (username.strip(),),
            ).fetchone()
        if row is None or not hmac.compare_digest(self._password_digest(password, row["password_salt"]), row["password_hash"]):
            return None
        return LocalUser(id=int(row["id"]), username=str(row["username"]))

    def create_session(self, user: LocalUser) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO console_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (self._token_digest(token), user.id, (datetime.now(UTC) + _SESSION_TTL).isoformat()),
            )
        return token

    def session_user(self, token: str | None) -> LocalUser | None:
        if not token:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.id, u.username, s.expires_at
                FROM console_sessions AS s
                JOIN console_users AS u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (self._token_digest(token),),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(UTC):
                connection.execute("DELETE FROM console_sessions WHERE token_hash = ?", (self._token_digest(token),))
                return None
            return LocalUser(id=int(row["id"]), username=str(row["username"]))

    def delete_session(self, token: str | None) -> None:
        if token:
            with self._connect() as connection:
                connection.execute("DELETE FROM console_sessions WHERE token_hash = ?", (self._token_digest(token),))

    @staticmethod
    def _password_digest(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)

    @staticmethod
    def _token_digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
