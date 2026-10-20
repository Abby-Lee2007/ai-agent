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
    ) -> None:
        self.store = store
        self.store_factory = store_factory
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
            record = self._sessions.get(session_id)
            if record is None:
                raise SessionNotFound(session_id)

            cache_key = (session_id, request_id) if request_id else None
            if cache_key and cache_key in self._idempotency:
                cached = dict(self._idempotency[cache_key])
                cached["replayed"] = True
                return cached

            reply = record.agent.handle(text)
            payload = {
                "session_id": session_id,
                "request_id": request_id,
                "reply": asdict(reply),
                "replayed": False,
            }
            if cache_key:
                self._idempotency[cache_key] = payload
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

    @staticmethod
    def _session_payload(record: SessionRecord) -> Dict[str, Any]:
        return {
            "session_id": record.session_id,
            "user_id": record.user_id,
            "channel": record.channel,
            "created_at": record.created_at,
        }
