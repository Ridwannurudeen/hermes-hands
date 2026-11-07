"""Durable gateway state in SQLite under ``plugins.plugin_storage.plugin_db`` (public keys only, no secrets).

Shared by the gateway process (commands, tools, the terminal backend) and the dashboard process (the
relay); both must resolve the same ``HERMES_HOME``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from . import protocol

PLUGIN = "hermes-hands"
OFFLINE_AFTER = (
    20  # seconds without a device request before it counts as offline; register() sets it
)
RELAY_STALE_AFTER = 15  # seconds; the relay writes its heartbeat row every 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL,
    os TEXT NOT NULL,
    home TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    reported_policy_hash TEXT NOT NULL,
    policy_ok INTEGER NOT NULL DEFAULT 1,
    paired_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revoked_devices (
    device_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    revoked_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_targets (
    session_key TEXT PRIMARY KEY,
    target TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_marks (
    session_key TEXT PRIMARY KEY,
    last_run TEXT
);
CREATE TABLE IF NOT EXISTS relay_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL NOT NULL
);
"""


class PairingError(Exception):
    """A pairing request the relay refuses; ``status`` and ``error`` go on the wire, ``name`` is the
    device name the refusal is about (the taken name, or the name the code was minted for)."""

    def __init__(self, status: int, error: str, name: str = ""):
        super().__init__(error)
        self.status = status
        self.error = error
        self.name = name


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """One connection per call, one transaction per ``with`` block (commit on success)."""
    from plugins.plugin_storage import plugin_db

    conn = plugin_db(PLUGIN, "hands.db")
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _public(row: sqlite3.Row, now: int) -> dict:
    return {
        "device_id": row["device_id"],
        "name": row["name"],
        "os": row["os"],
        "home": row["home"],
        "last_seen": row["last_seen"],
        "online": now - row["last_seen"] <= OFFLINE_AFTER,
        "policy_ok": bool(row["policy_ok"]),
    }


def mint_pairing_code(name: str, ttl: int = 600) -> str:
    """A single-use 32-byte code for pairing a device called ``name``; only its hash is stored."""
    code = secrets.token_urlsafe(32)
    with _db() as conn:
        conn.execute(
            "INSERT INTO pairing_codes (code_hash, name, expires_at) VALUES (?, ?, ?)",
            (_code_hash(code), name, int(time.time()) + ttl),
        )
    return code


def _live_code(conn: sqlite3.Connection, code: str, now: int) -> sqlite3.Row | None:
    """The live (unused, unexpired) row for ``code``. Every live digest is compared with
    ``hmac.compare_digest``, so the match is never decided by an SQL equality on the secret."""
    digest = _code_hash(code)
    match = None
    rows = conn.execute(
        "SELECT code_hash, name FROM pairing_codes WHERE used = 0 AND expires_at > ?", (now,)
    ).fetchall()
    for row in rows:
        if hmac.compare_digest(row["code_hash"], digest):
            match = row
    return match


def pairing_code_name(code: str) -> str | None:
    """The device name a live pairing code was minted for, else None."""
    with _db() as conn:
        row = _live_code(conn, code, int(time.time()))
    return None if row is None else row["name"]


def complete_pairing(
    code: str, *, name: str, public_key: str, os: str, home: str, policy_hash: str
) -> str:
    """Burn ``code`` and record the device in one transaction; returns the new device_id."""
    now = int(time.time())
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _live_code(conn, code, now)
        if row is None:
            raise PairingError(401, "bad_code")
        if row["name"] != name:
            raise PairingError(409, "name_mismatch", row["name"])
        if conn.execute("SELECT 1 FROM devices WHERE name = ?", (name,)).fetchone() is not None:
            raise PairingError(409, "name_taken", name)
        device_id = secrets.token_hex(8)
        conn.execute("UPDATE pairing_codes SET used = 1 WHERE code_hash = ?", (row["code_hash"],))
        conn.execute(
            "INSERT INTO devices (device_id, name, public_key, os, home, policy_hash, reported_policy_hash,"
            " policy_ok, paired_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (device_id, name, public_key, os, home, policy_hash, policy_hash, now, now),
        )
    return device_id


def list_devices() -> list[dict]:
    now = int(time.time())
    with _db() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
    return [_public(row, now) for row in rows]


def get_device(name: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM devices WHERE name = ?", (name,)).fetchone()
    return None if row is None else _public(row, int(time.time()))


def device_by_id(device_id: str) -> dict | None:
    """The full device record for the relay and the token provider, ``public_key`` as raw bytes."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    if row is None:
        return None
    return {
        **_public(row, int(time.time())),
        "public_key": protocol.unb64u(row["public_key"]),
        "policy_hash": row["policy_hash"],
    }


def record_seen(
    device_id: str,
    *,
    policy_hash: str | None = None,
    os: str | None = None,
    home: str | None = None,
) -> bool | None:
    """Mark the device seen now with what it reported; a policy hash different from the accepted one
    turns ``policy_ok`` off until :func:`accept_policy`. Returns ``policy_ok``, or None if unknown."""
    with _db() as conn:
        conn.execute(
            "UPDATE devices SET last_seen = ?, os = COALESCE(?, os), home = COALESCE(?, home),"
            " reported_policy_hash = COALESCE(?, reported_policy_hash),"
            " policy_ok = CASE WHEN ? IS NOT NULL AND ? != policy_hash THEN 0 ELSE policy_ok END"
            " WHERE device_id = ?",
            (int(time.time()), os, home, policy_hash, policy_hash, policy_hash, device_id),
        )
        row = conn.execute(
            "SELECT policy_ok FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
    return None if row is None else bool(row["policy_ok"])


def revoked_key(device_id: str) -> bytes | None:
    """The public key a revoked device signs with (its tombstone), else None."""
    with _db() as conn:
        row = conn.execute(
            "SELECT public_key FROM revoked_devices WHERE device_id = ?", (device_id,)
        ).fetchone()
    return None if row is None else protocol.unb64u(row["public_key"])


def revoke(name: str) -> bool:
    """Delete the device, keeping its id and public key as a tombstone so the relay can answer its
    next request with a signed ``unknown_device``; the name is free to pair again."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO revoked_devices (device_id, public_key, revoked_at)"
            " SELECT device_id, public_key, ? FROM devices WHERE name = ?",
            (int(time.time()), name),
        )
        return conn.execute("DELETE FROM devices WHERE name = ?", (name,)).rowcount > 0


def accept_policy(name: str) -> bool:
    """Accept the deny-list hash the device last reported and let the relay serve it again."""
    with _db() as conn:
        return (
            conn.execute(
                "UPDATE devices SET policy_hash = reported_policy_hash, policy_ok = 1 WHERE name = ?",
                (name,),
            ).rowcount
            > 0
        )


def set_session_target(session_key: str, target: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO session_targets (session_key, target) VALUES (?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET target = excluded.target",
            (session_key, target),
        )


def get_session_target(session_key: str) -> str | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT target FROM session_targets WHERE session_key = ?", (session_key,)
        ).fetchone()
    return None if row is None else row["target"]


def get_last_run(session_key: str) -> str | None:
    """The target the session's last command ran on. It outlives the terminal backend object (the
    idle reaper, a gateway restart), so a cwd recorded on another target is never reused."""
    with _db() as conn:
        row = conn.execute(
            "SELECT last_run FROM session_marks WHERE session_key = ?", (session_key,)
        ).fetchone()
    return None if row is None else row["last_run"]


def set_last_run(session_key: str, target: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO session_marks (session_key, last_run) VALUES (?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET last_run = excluded.last_run",
            (session_key, target),
        )


def write_relay_heartbeat() -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO relay_heartbeat (id, ts) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET ts = excluded.ts",
            (time.time(),),
        )


def relay_alive() -> bool:
    """True when the relay wrote its heartbeat row in the last ``RELAY_STALE_AFTER`` seconds."""
    with _db() as conn:
        row = conn.execute("SELECT ts FROM relay_heartbeat WHERE id = 1").fetchone()
    return row is not None and time.time() - row["ts"] <= RELAY_STALE_AFTER
