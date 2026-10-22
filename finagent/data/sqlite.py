"""SQLite persistence for API sessions, domain data, and idempotency records."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional


PROJECTION_TABLES = {
    "accounts": ("account_id", "accounts"),
    "transactions": ("txn_id", "transactions"),
    "payees": ("payee_id", "payees"),
    "cards": ("card_id", "cards"),
    "subscriptions": ("sub_id", "subscriptions"),
    "products": ("product_id", "products"),
    "holdings": ("holding_id", "holdings"),
    "merchant_offers": ("offer_id", "merchant_offers"),
    "upload_batches": ("batch_id", "upload_batches"),
    "tasks": ("task_id", "tasks"),
    "goals": ("goal_id", "goals"),
    "aa_sheets": ("sheet_id", "aa_sheets"),
    "alerts": ("alert_id", "alerts"),
}


class SQLiteRepository:
    """Small durable repository using only Python's standard-library sqlite3."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (session_id, request_id)
                )
                """
            )
            for table, (id_column, _) in PROJECTION_TABLES.items():
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        session_id TEXT NOT NULL,
                        {id_column} TEXT NOT NULL,
                        user_id TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL DEFAULT '',
                        is_live INTEGER NOT NULL DEFAULT 0,
                        is_demo INTEGER NOT NULL DEFAULT 1,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (session_id, {id_column})
                    )
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    session_id TEXT NOT NULL,
                    audit_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'agent:audit',
                    is_demo INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (session_id, audit_id)
                )
                """
            )

    def save_session(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        created_at: str,
        payload: Dict[str, Any],
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, user_id, channel, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, datetime('now'), ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    channel=excluded.channel,
                    updated_at=datetime('now'),
                    payload=excluded.payload
                """,
                (session_id, user_id, channel, created_at, serialized),
            )
            snapshot = payload.get("store")
            if isinstance(snapshot, dict):
                self._sync_store_projection(connection, session_id, snapshot)

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT user_id, channel, created_at, payload FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": session_id,
            "user_id": row[0],
            "channel": row[1],
            "created_at": row[2],
            **json.loads(row[3]),
        }

    def save_idempotency(
        self,
        session_id: str,
        request_id: str,
        payload: Dict[str, Any],
        created_at: str,
    ) -> None:
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO idempotency(session_id, request_id, created_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, request_id, created_at, serialized),
            )

    def load_idempotency(self, session_id: str, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM idempotency WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _sync_store_projection(
        connection: sqlite3.Connection,
        session_id: str,
        snapshot: Dict[str, Any],
    ) -> None:
        """Keep queryable domain tables alongside the portable JSON snapshot."""
        for table, (id_column, source_table) in PROJECTION_TABLES.items():
            connection.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            objects = snapshot.get(source_table, {}) or {}
            for object_key, raw in objects.items():
                if not isinstance(raw, dict):
                    continue
                object_id = str(raw.get(id_column) or object_key)
                payload = json.dumps(raw, ensure_ascii=False)
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO {table}
                        (session_id, {id_column}, user_id, source, updated_at,
                         is_live, is_demo, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        object_id,
                        str(raw.get("user_id") or ""),
                        str(raw.get("source") or "agent:simulation"),
                        str(raw.get("updated_at") or ""),
                        1 if raw.get("is_live") else 0,
                        1 if raw.get("is_demo", True) else 0,
                        payload,
                    ),
                )

        connection.execute("DELETE FROM audit_logs WHERE session_id=?", (session_id,))
        for index, raw in enumerate(snapshot.get("audit", []) or []):
            if not isinstance(raw, dict):
                continue
            created_at = str(raw.get("at") or "")
            event = str(raw.get("event") or "unknown")
            audit_id = str(raw.get("audit_id") or f"{created_at}:{event}:{index}")
            connection.execute(
                """
                INSERT OR REPLACE INTO audit_logs
                    (session_id, audit_id, created_at, event, source, is_demo, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    audit_id,
                    created_at,
                    event,
                    "agent:audit",
                    1,
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
