"""Session orchestration shared by HTTP, H5, and IM adapters."""
from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from finagent.agent.core import BankingAgent
from finagent.data.store import DataStore


class SessionNotFound(KeyError):
    """Raised when a client references an unknown session."""


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    channel: str
    created_at: str
    agent: BankingAgent


class AgentSessionService:
    """Owns isolated dialogue sessions on top of a shared demo data store."""

    def __init__(
        self,
        store: DataStore,
        llm_mode: str = "off",
        store_factory: Optional[Callable[[], DataStore]] = None,
        repository: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.store_factory = store_factory
        self.repository = repository
        self.llm_mode = llm_mode
        self._sessions: Dict[str, SessionRecord] = {}
        self._channel_sessions: Dict[tuple[str, str], str] = {}
        self._idempotency: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create_session(
        self,
        user_id: str = "u_001",
        channel: str = "h5",
        external_user_id: str = "",
    ) -> Dict[str, Any]:
        user = self.store.get_user(user_id)
        if user is None:
            raise ValueError(f"未知用户：{user_id}")

        session_id = f"SESSION-{uuid.uuid4().hex}"
        session_store = self.store_factory() if self.store_factory else self.store
        record = SessionRecord(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            created_at=datetime.now().isoformat(timespec="seconds"),
            agent=BankingAgent(session_store, user_id, llm_mode=self.llm_mode),
        )
        with self._lock:
            self._sessions[session_id] = record
            self._persist_session(record)
        return self._session_payload(record)

    def send_message(
        self,
        session_id: str,
        message: str,
        request_id: str = "",
    ) -> Dict[str, Any]:
        text = (message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        with self._lock:
            record = self._get_or_load(session_id)

            cache_key = (session_id, request_id) if request_id else None
            if cache_key and cache_key in self._idempotency:
                cached = dict(self._idempotency[cache_key])
                cached["replayed"] = True
                return cached
            if cache_key and self.repository is not None:
                cached = self.repository.load_idempotency(session_id, request_id)
                if cached is not None:
                    cached = dict(cached)
                    cached["replayed"] = True
                    self._idempotency[cache_key] = cached
                    return cached

            reply = record.agent.handle(text)
            payload = {
                "session_id": session_id,
                "request_id": request_id,
                "reply": asdict(reply),
                "replayed": False,
            }
            # Persist the mutated business state before the replay cache so a
            # crash cannot return an old response without its side effects.
            self._persist_session(record)
            if cache_key:
                self._idempotency[cache_key] = payload
                if self.repository is not None:
                    self.repository.save_idempotency(
                        session_id,
                        request_id,
                        payload,
                        record.created_at,
                    )
            return payload

    def handle_channel_message(
        self,
        channel: str,
        external_user_id: str,
        message: str,
        message_id: str = "",
        user_id: str = "u_001",
    ) -> Dict[str, Any]:
        if not external_user_id:
            raise ValueError("external_user_id 不能为空")

        mapping_key = (channel, external_user_id)
        with self._lock:
            session_id = self._channel_sessions.get(mapping_key)
            if session_id is None:
                session_id = self.create_session(
                    user_id=user_id,
                    channel=channel,
                    external_user_id=external_user_id,
                )["session_id"]
                self._channel_sessions[mapping_key] = session_id
            payload = self.send_message(
                session_id=session_id,
                message=message,
                request_id=message_id,
            )
            payload["channel"] = channel
            return payload

    def _get_or_load(self, session_id: str) -> SessionRecord:
        record = self._sessions.get(session_id)
        if record is not None:
            return record
        if self.repository is None:
            raise SessionNotFound(session_id)
        data = self.repository.load_session(session_id)
        if data is None:
            raise SessionNotFound(session_id)
        store = self._restore_store(data["store"])
        agent = BankingAgent(store, str(data["user_id"]), llm_mode=self.llm_mode)
        agent.risk_answers = dict(data.get("risk_answers") or {})
        agent.risk_level = data.get("risk_level")
        agent.risk_assessed_at = str(data.get("risk_assessed_at") or "")
        agent.risk_inference_confirmed_at = str(
            data.get("risk_inference_confirmed_at") or ""
        )
        agent.session.history = list(data.get("history") or [])
        agent.session.transcript = list(data.get("transcript") or [])
        agent.session.turn = int(data.get("turn") or 0)
        agent.last_intent_name = str(data.get("last_intent_name") or "")
        record = SessionRecord(
            session_id=session_id,
            user_id=str(data["user_id"]),
            channel=str(data["channel"]),
            created_at=str(data["created_at"]),
            agent=agent,
        )
        self._sessions[session_id] = record
        return record

    @staticmethod
    def _restore_store(snapshot: Dict[str, Any]) -> DataStore:
        from finagent.data.store import Clock

        clock_data = snapshot.get("clock") or datetime.now().isoformat(timespec="seconds")
        store = DataStore(clock=Clock(datetime.fromisoformat(clock_data)))
        store.load_snapshot(snapshot)
        return store

    def _persist_session(self, record: SessionRecord) -> None:
        if self.repository is None:
            return
        agent = record.agent
        self.repository.save_session(
            session_id=record.session_id,
            user_id=record.user_id,
            channel=record.channel,
            created_at=record.created_at,
            payload={
                "store": agent.store.snapshot(),
                "risk_answers": dict(agent.risk_answers),
                "risk_level": agent.risk_level,
                "risk_assessed_at": agent.risk_assessed_at,
                "risk_inference_confirmed_at": agent.risk_inference_confirmed_at,
                "history": list(agent.session.history),
                "transcript": list(agent.session.transcript),
                "turn": agent.session.turn,
                "last_intent_name": agent.last_intent_name,
            },
        )

    @staticmethod
    def _session_payload(record: SessionRecord) -> Dict[str, Any]:
        return {
            "session_id": record.session_id,
            "user_id": record.user_id,
            "channel": record.channel,
            "created_at": record.created_at,
        }
