from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

USERNAME = re.compile(r"^[a-z][a-z0-9_]{2,26}$")
SESSION_TTL = 12 * 60 * 60
INVITE_TTL = 24 * 60 * 60


def valid_username(value: str) -> str:
    if not USERNAME.fullmatch(value):
        raise ValueError("Account name must be 3-27 lowercase letters, digits or underscores")
    return value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TeamStore:
    def __init__(self, root: Path) -> None:
        from argon2 import PasswordHasher

        self.root = root.resolve()
        control = self.root / "control"
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        control.chmod(0o700)
        self.path = control / "accounts.db"
        self.hasher = PasswordHasher()
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts (
                  username TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('admin','member')),
                  password_hash TEXT, enabled INTEGER NOT NULL DEFAULT 1,
                  worker_socket TEXT NOT NULL UNIQUE, worker_token TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts,
                  expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invites (
                  token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts,
                  expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                  key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
                  action TEXT NOT NULL, subject TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_setups (
                  username TEXT PRIMARY KEY REFERENCES accounts,
                  status TEXT NOT NULL, error TEXT, updated_at REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row) -> dict:
        return {
            key: row[key]
            for key in (
                "username",
                "display_name",
                "role",
                "enabled",
                "created_at",
            )
        } | {"activated": bool(row["password_hash"])}

    def accounts(self) -> list[dict]:
        with self.connect() as db:
            return [
                self.public(row)
                | {
                    "setup": (
                        {"status": row["setup_status"], "error": row["setup_error"]}
                        if row["setup_status"]
                        else None
                    )
                }
                for row in db.execute(
                    "SELECT a.*, s.status AS setup_status, s.error AS setup_error "
                    "FROM accounts a LEFT JOIN account_setups s USING(username) "
                    "ORDER BY username"
                )
            ]

    def account(self, username: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None

    def add_account(
        self,
        username: str,
        display_name: str,
        *,
        admin: bool = False,
        actor: str = "cli",
        setup: bool = False,
    ) -> dict:
        valid_username(username)
        if not display_name.strip() or len(display_name) > 160:
            raise ValueError("Display name must be 1-160 characters")
        socket = self.root / "users" / username / "run" / "api.sock"
        if len(str(socket).encode()) >= 104:
            raise ValueError("Team root is too long for Unix socket paths")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                raise ValueError("Account already exists; refresh the account list to continue")
            db.execute(
                "INSERT INTO accounts VALUES (?,?,?,NULL,1,?,?,?)",
                (
                    username,
                    display_name.strip(),
                    "admin" if admin else "member",
                    str(socket),
                    secrets.token_urlsafe(48),
                    time.time(),
                ),
            )
            if setup:
                db.execute(
                    "INSERT INTO account_setups VALUES (?, 'pending', NULL, ?)",
                    (username, time.time()),
                )
            self.audit(db, actor, "account.created", username)
        return self.account(username)

    def setup(self, username: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT status,error FROM account_setups WHERE username=?", (username,)
            ).fetchone()
            return dict(row) if row else None

    def set_setup(
        self,
        username: str,
        status: str,
        actor: str,
        error: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE account_setups SET status=?,error=?,updated_at=? WHERE username=?",
                (status, error, time.time(), username),
            )
            self.audit(db, actor, f"account.setup.{status}", username)

    @staticmethod
    def audit(db, actor: str, action: str, subject: str) -> None:
        db.execute(
            "INSERT INTO audit(at,actor,action,subject) VALUES (?,?,?,?)",
            (time.time(), actor, action, subject),
        )

    def invitation(self, username: str, actor: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            if not row or not row["enabled"] or row["password_hash"]:
                raise ValueError("Only an enabled, unactivated account can be invited")
            setup = db.execute(
                "SELECT status FROM account_setups WHERE username=?", (username,)
            ).fetchone()
            if setup and setup["status"] != "ready":
                raise ValueError("Workspace is not ready; retry account initialization first")
            db.execute("DELETE FROM invites WHERE username=?", (username,))
            db.execute(
                "INSERT INTO invites VALUES (?,?,?)",
                (digest(token), username, time.time() + INVITE_TTL),
            )
            self.audit(db, actor, "invitation.created", username)
        return token

    def activate(self, token: str, password: str) -> str:
        self.validate_password(password)
        hashed = self.hasher.hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT a.* FROM invites i JOIN accounts a USING(username) "
                "WHERE i.token_hash=? AND i.expires_at>? AND a.enabled=1",
                (digest(token), time.time()),
            ).fetchone()
            if not row or row["password_hash"]:
                raise ValueError("Invitation is invalid, expired or already used")
            username = row["username"]
            db.execute("UPDATE accounts SET password_hash=? WHERE username=?", (hashed, username))
            db.execute("DELETE FROM invites WHERE username=?", (username,))
            self.audit(db, username, "account.activated", username)
        return username

    @staticmethod
    def validate_password(password: str) -> None:
        if not 12 <= len(password) <= 256:
            raise ValueError("Password must contain 12-256 characters")

    def check_password(self, username: str, password: str) -> bool:
        from argon2.exceptions import VerificationError

        row = self.account(username)
        try:
            self.hasher.verify((row or {}).get("password_hash") or self.dummy_hash, password)
        except VerificationError:
            return False
        return bool(row and row["enabled"] and row["password_hash"])

    def new_session(self, username: str) -> str:
        token = secrets.token_urlsafe(48)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (digest(token), username, time.time() + SESSION_TTL),
            )
            self.audit(db, username, "session.created", username)
        return token

    def session(self, token: str) -> dict | None:
        if not token or len(token) > 200:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT a.* FROM sessions s JOIN accounts a USING(username) "
                "WHERE s.token_hash=? AND s.expires_at>? AND a.enabled=1",
                (digest(token), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def logout(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def change_password(self, username: str, current: str, password: str) -> None:
        self.validate_password(password)
        if not self.check_password(username, current):
            raise ValueError("Current password is incorrect")
        hashed = self.hasher.hash(password)
        with self.connect() as db:
            db.execute("UPDATE accounts SET password_hash=? WHERE username=?", (hashed, username))
            db.execute("DELETE FROM sessions WHERE username=?", (username,))
            self.audit(db, username, "password.changed", username)

    def reset_login(self, username: str) -> None:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                raise ValueError("Account not found")
            db.execute("UPDATE accounts SET password_hash=NULL WHERE username=?", (username,))
            db.execute("DELETE FROM sessions WHERE username=?", (username,))
            db.execute("DELETE FROM invites WHERE username=?", (username,))
            self.audit(db, "cli", "password.reset_requested", username)

    def set_enabled(self, username: str, enabled: bool, actor: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            if not row:
                raise ValueError("Account not found")
            if not enabled and (username == actor or row["role"] == "admin"):
                raise ValueError("Administrator accounts cannot be disabled from the Web")
            db.execute("UPDATE accounts SET enabled=? WHERE username=?", (enabled, username))
            if not enabled:
                db.execute("DELETE FROM sessions WHERE username=?", (username,))
                db.execute("DELETE FROM invites WHERE username=?", (username,))
            self.audit(db, actor, "account.enabled" if enabled else "account.disabled", username)

    def rate_limit(self, key: str, limit: int = 10) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rate_limits WHERE expires_at<?", (now,))
            row = db.execute("SELECT count FROM rate_limits WHERE key=?", (digest(key),)).fetchone()
            if row and row[0] >= limit:
                return False
            db.execute(
                "INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(key) "
                "DO UPDATE SET count=count+1",
                (digest(key), now + 300),
            )
            return True
