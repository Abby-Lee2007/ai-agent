"""SQLite persistence for API sessions, domain data, and idempotency records."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union


PROJECTION_TABLES = {
    "accounts": ("account_id", "accounts"),
    "transactions": ("txn_id", "transactions"),
    "payees": ("payee_id", "payees"),
    "cards": ("card_id", "cards"),
    "subscriptions": ("sub_id", "subscriptions"),
    "membership_orders": ("order_id", "membership_orders"),
    "membership_order_items": ("item_id", "membership_order_items"),
    "products": ("product_id", "products"),
    "holdings": ("holding_id", "holdings"),
    "merchant_offers": ("offer_id", "merchant_offers"),
    "upload_batches": ("batch_id", "upload_batches"),
    "tasks": ("task_id", "tasks"),
    "goals": ("goal_id", "goals"),
    "aa_sheets": ("sheet_id", "aa_sheets"),
    "alerts": ("alert_id", "alerts"),
}


class OptimisticConcurrencyError(RuntimeError):
    """Raised when another service instance changed the session first."""


class SQLiteRepository:
    """Small durable repository using only Python's standard-library sqlite3."""

    def __init__(self, path: Union[Path, str]) -> None:
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
                    version INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
            }
            if "version" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_shadow_runs (
                    shadow_run_id TEXT PRIMARY KEY,
                    application_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_status TEXT NOT NULL,
                    reliable_coverage REAL,
                    approximate_coverage REAL,
                    potential_coverage REAL,
                    reliable_feature_count INTEGER NOT NULL DEFAULT 0,
                    approximate_feature_count INTEGER NOT NULL DEFAULT 0,
                    missing_feature_count INTEGER NOT NULL DEFAULT 0,
                    policy_blocked_count INTEGER NOT NULL DEFAULT 0,
                    history_complete INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(application_id, request_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_credit_shadow_runs_application
                ON credit_shadow_runs(application_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_credit_shadow_runs_request
                ON credit_shadow_runs(request_id, created_at DESC)
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
        expected_version: Optional[int] = None,
    ) -> int:
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT version FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO sessions(
                        session_id, user_id, channel, created_at,
                        updated_at, version, payload
                    )
                    VALUES (?, ?, ?, ?, datetime('now'), 1, ?)
                    """,
                    (session_id, user_id, channel, created_at, serialized),
                )
                version = 1
            else:
                current_version = int(row[0])
                if (
                    expected_version is not None
                    and current_version != int(expected_version)
                ):
                    raise OptimisticConcurrencyError(
                        "会话已被其他请求更新，请刷新后重试"
                    )
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET user_id=?, channel=?, updated_at=datetime('now'),
                        version=version+1, payload=?
                    WHERE session_id=? AND version=?
                    """,
                    (
                        user_id,
                        channel,
                        serialized,
                        session_id,
                        current_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OptimisticConcurrencyError(
                        "会话已被其他请求更新，请刷新后重试"
                    )
                version = current_version + 1
            snapshot = payload.get("store")
            if isinstance(snapshot, dict):
                self._sync_store_projection(connection, session_id, snapshot)
        return version

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT user_id, channel, created_at, version, payload "
                "FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": session_id,
            "user_id": row[0],
            "channel": row[1],
            "created_at": row[2],
            "version": int(row[3]),
            **json.loads(row[4]),
        }

    def list_session_ids_with_tasks(self) -> list[str]:
        """Return sessions that still have pending scheduled work."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT session_id, payload FROM tasks ORDER BY session_id"
            ).fetchall()
        session_ids: set[str] = set()
        for session_id, payload in rows:
            try:
                task = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(task, dict) and task.get("status") == "pending":
                session_ids.add(str(session_id))
        return sorted(session_ids)

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

    def save_credit_shadow_run(
        self,
        payload: Dict[str, Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """Insert once by application/request and return the canonical row."""
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO credit_shadow_runs(
                    shadow_run_id, application_id, request_id, status,
                    model_status, reliable_coverage, approximate_coverage,
                    potential_coverage, reliable_feature_count,
                    approximate_feature_count, missing_feature_count,
                    policy_blocked_count, history_complete, created_at,
                    completed_at, schema_version, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["shadow_run_id"]),
                    str(payload["application_id"]),
                    str(payload["request_id"]),
                    str(payload["status"]),
                    str(payload["model_status"]),
                    payload.get("reliable_coverage"),
                    payload.get("approximate_coverage"),
                    payload.get("potential_coverage"),
                    int(payload.get("reliable_feature_count") or 0),
                    int(payload.get("approximate_feature_count") or 0),
                    int(payload.get("missing_feature_count") or 0),
                    int(payload.get("policy_blocked_count") or 0),
                    (
                        None
                        if payload.get("history_complete") is None
                        else 1 if payload["history_complete"] else 0
                    ),
                    str(payload["created_at"]),
                    str(payload["completed_at"]),
                    str(payload["schema_version"]),
                    serialized,
                ),
            )
            inserted = cursor.rowcount == 1
            row = connection.execute(
                """
                SELECT payload FROM credit_shadow_runs
                WHERE application_id=? AND request_id=?
                """,
                (
                    str(payload["application_id"]),
                    str(payload["request_id"]),
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT payload FROM credit_shadow_runs WHERE shadow_run_id=?",
                    (str(payload["shadow_run_id"]),),
                ).fetchone()
            if row is None:
                raise RuntimeError("shadow run insert did not produce a record")
            return inserted, json.loads(row[0])

    def load_credit_shadow_run(
        self,
        shadow_run_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM credit_shadow_runs WHERE shadow_run_id=?",
                (shadow_run_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def load_credit_shadow_run_by_identity(
        self,
        application_id: str,
        request_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload FROM credit_shadow_runs
                WHERE application_id=? AND request_id=?
                """,
                (application_id, request_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_credit_shadow_runs_by_application(
        self,
        application_id: str,
    ) -> list[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM credit_shadow_runs
                WHERE application_id=?
                ORDER BY created_at DESC
                """,
                (application_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_credit_shadow_runs_by_request(
        self,
        request_id: str,
    ) -> list[Dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM credit_shadow_runs
                WHERE request_id=?
                ORDER BY created_at DESC
                """,
                (request_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def claim_idempotency(
        self,
        session_id: str,
        request_id: str,
        request_fingerprint: str,
        claimed_at: str,
        claim_token: str,
    ) -> Tuple[str, Dict[str, Any]]:
        """Atomically claim a request before any business tool can run.

        The existing payload column is the state store, so this remains
        compatible with databases created by earlier releases.
        """
        claim = {
            "session_id": session_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "request_status": "processing",
            "claimed_at": claimed_at,
            "claim_token": claim_token,
            "replayed": False,
        }
        serialized_claim = json.dumps(claim, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency(
                    session_id, request_id, created_at, payload
                )
                VALUES (?, ?, ?, ?)
                """,
                (session_id, request_id, claimed_at, serialized_claim),
            )
            if cursor.rowcount == 1:
                return "claimed", claim

            row = connection.execute(
                "SELECT payload FROM idempotency "
                "WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if row is None:
                return "conflict", claim
            raw_payload = str(row[0])
            existing = json.loads(raw_payload)
            stored_fingerprint = str(
                existing.get("request_fingerprint") or ""
            )
            if (
                stored_fingerprint
                and stored_fingerprint != request_fingerprint
            ):
                return "fingerprint_mismatch", existing

            status = str(existing.get("request_status") or "")
            if status:
                return status, existing

            # Legacy rows did not record lifecycle state. Successful replies
            # remain replayable; old handled failures keep the pre-H-01 retry
            # behavior by allowing a single owner to replace the legacy row.
            if self._legacy_replayable(existing):
                return "completed", existing
            cursor = connection.execute(
                """
                UPDATE idempotency
                SET created_at=?, payload=?
                WHERE session_id=? AND request_id=? AND payload=?
                """,
                (
                    claimed_at,
                    serialized_claim,
                    session_id,
                    request_id,
                    raw_payload,
                ),
            )
            if cursor.rowcount == 1:
                return "claimed", claim
            current = connection.execute(
                "SELECT payload FROM idempotency "
                "WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if current is None:
                return "conflict", claim
            return "conflict", json.loads(current[0])

    def complete_idempotency(
        self,
        session_id: str,
        request_id: str,
        payload: Dict[str, Any],
        updated_at: str,
        claim_token: str,
    ) -> Dict[str, Any]:
        """Persist a terminal result without overwriting another executor."""
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM idempotency "
                "WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if row is not None:
                existing = json.loads(row[0])
                if str(existing.get("request_status") or "") == "completed":
                    return existing
                existing_token = str(existing.get("claim_token") or "")
                if existing_token and existing_token != claim_token:
                    return existing
            connection.execute(
                """
                INSERT INTO idempotency(session_id, request_id, created_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, request_id) DO UPDATE SET
                    created_at=excluded.created_at,
                    payload=excluded.payload
                """,
                (session_id, request_id, updated_at, serialized),
            )
        return payload

    def mark_idempotency_outcome_unknown(
        self,
        session_id: str,
        request_id: str,
        updated_at: str,
        claim_token: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Fail closed when execution or result persistence becomes ambiguous."""
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM idempotency "
                "WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            if str(payload.get("request_status") or "") == "completed":
                return payload
            existing_token = str(payload.get("claim_token") or "")
            if (
                claim_token
                and existing_token
                and existing_token != claim_token
            ):
                return payload
            payload["request_status"] = "outcome_unknown"
            payload["updated_at"] = updated_at
            payload["replayed"] = False
            connection.execute(
                """
                UPDATE idempotency
                SET created_at=?, payload=?
                WHERE session_id=? AND request_id=?
                """,
                (
                    updated_at,
                    json.dumps(payload, ensure_ascii=False),
                    session_id,
                    request_id,
                ),
            )
        return payload

    @staticmethod
    def _legacy_replayable(payload: Dict[str, Any]) -> bool:
        reply = payload.get("reply") or {}
        return (
            isinstance(reply, dict)
            and bool(reply.get("ok", True))
            and reply.get("kind") != "error"
        )

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
