"""Session orchestration shared by HTTP, H5, and IM adapters."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from finagent.agent.core import AgentReply, BankingAgent
from finagent.data.models import Card
from finagent.data.market import build_market_overview
from finagent.data.official_sources import load_official_data
from finagent.data.sqlite import OptimisticConcurrencyError
from finagent.data.store import DataStore
from finagent.integrations.sandbox_adapters import execution_payload
from finagent.risk.credit_risk.integration import BusinessCreditShadowIntegration
from finagent.risk.credit_risk.run_repository import CreditShadowRunRepository


_LOGGER = logging.getLogger(__name__)


class SessionNotFound(KeyError):
    """Raised when a client references an unknown session."""


class SessionAuthError(PermissionError):
    """Raised when a session token is missing or invalid."""


class RequestNotFound(KeyError):
    """Raised when a request lifecycle record does not exist."""


class StepUpRequired(PermissionError):
    """Raised when a sensitive profile change needs card-level verification."""

    def __init__(self, message: str, challenge: Dict[str, Any]) -> None:
        super().__init__(message)
        self.challenge = challenge


@dataclass
class StepUpChallenge:
    token: str
    session_id: str
    card_id: str
    digest: str
    expires_at: float
    attempts: int = 0


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    channel: str
    created_at: str
    agent: BankingAgent
    version: int = 0
    session_token_hash: str = ""
    processed_requests: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class AgentSessionService:
    """Owns isolated dialogue sessions on top of a shared demo data store."""

    def __init__(
        self,
        store: DataStore,
        llm_mode: str = "off",
        store_factory: Optional[Callable[[], DataStore]] = None,
        repository: Optional[Any] = None,
        step_up_ttl_seconds: int = 300,
        now_utc: Optional[Callable[[], datetime]] = None,
        processing_stale_seconds: int = 300,
    ) -> None:
        self.store = store
        self.store_factory = store_factory
        self.repository = repository
        self.llm_mode = llm_mode
        self._sessions: Dict[str, SessionRecord] = {}
        self._channel_sessions: Dict[tuple[str, str], str] = {}
        self._idempotency: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._step_up_challenges: Dict[str, StepUpChallenge] = {}
        self._step_up_ttl_seconds = max(30, int(step_up_ttl_seconds))
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        self._processing_stale_seconds = max(
            30,
            int(processing_stale_seconds),
        )
        self._lock = threading.RLock()
        self._credit_shadow_runs = (
            CreditShadowRunRepository(repository)
            if repository is not None else None
        )

    def _build_agent(self, store: DataStore, user_id: str) -> BankingAgent:
        return BankingAgent(
            store,
            user_id,
            llm_mode=self.llm_mode,
            credit_shadow=BusinessCreditShadowIntegration.from_env(
                run_repository=self._credit_shadow_runs,
            ),
        )

    def _discard_credit_shadow_pending(
        self,
        record: SessionRecord,
        *,
        reason: str,
        error_type: str,
    ) -> None:
        try:
            record.agent.discard_credit_shadow_runs(
                reason=reason,
                error_type=error_type,
            )
        except Exception as exc:
            _LOGGER.error(
                "credit shadow pending cleanup failed: %s",
                type(exc).__name__,
            )

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
        session_token = secrets.token_urlsafe(32)
        session_token_hash = hashlib.sha256(
            session_token.encode("utf-8")
        ).hexdigest()
        session_store = self.store_factory() if self.store_factory else self.store
        record = SessionRecord(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            created_at=datetime.now().isoformat(timespec="seconds"),
            agent=self._build_agent(session_store, user_id),
            version=0,
            session_token_hash=session_token_hash,
        )
        with self._lock:
            self._sessions[session_id] = record
            self._persist_session(record)
        payload = self._session_payload(record)
        payload["session_token"] = session_token
        return payload

    def send_message(
        self,
        session_id: str,
        message: str,
        request_id: str = "",
    ) -> Dict[str, Any]:
        text = (message or "").strip()
        if not text:
            raise ValueError("message 不能为空")
        request_fingerprint = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

        with self._lock:
            record = self._get_or_load(session_id)

            cache_key = (session_id, request_id) if request_id else None
            claim_token = ""
            if request_id:
                embedded = record.processed_requests.get(request_id)
                if embedded is not None:
                    self._require_matching_fingerprint(
                        embedded,
                        request_fingerprint,
                    )
                    embedded_status = self._request_status(embedded)
                    if embedded_status == "completed":
                        replayed = self._public_request_payload(embedded)
                        replayed["replayed"] = True
                        return replayed
                    if embedded_status in {"processing", "outcome_unknown"}:
                        return self._public_request_payload(embedded)
            if cache_key and cache_key in self._idempotency:
                cached = dict(self._idempotency[cache_key])
                self._require_matching_fingerprint(
                    cached,
                    request_fingerprint,
                )
                cached_status = self._request_status(cached)
                if cached_status == "completed":
                    replayed = self._public_request_payload(cached)
                    replayed["replayed"] = True
                    return replayed
                if cached_status in {"processing", "outcome_unknown"}:
                    if self.repository is None:
                        return self._public_request_payload(cached)
                self._idempotency.pop(cache_key, None)

            if cache_key:
                claim_token = uuid.uuid4().hex
                if self.repository is not None:
                    claim_state, cached = self.repository.claim_idempotency(
                        session_id,
                        request_id,
                        request_fingerprint,
                        self._utc_now_iso(),
                        claim_token,
                    )
                    if claim_state == "fingerprint_mismatch":
                        raise ValueError("同一 request_id 不能用于不同消息")
                    if claim_state == "conflict":
                        claim_state = "outcome_unknown"
                    if claim_state != "claimed":
                        self._idempotency[cache_key] = dict(cached)
                        if claim_state == "processing":
                            if self._processing_is_stale(cached):
                                self._record_outcome_unknown(
                                    cache_key,
                                    str(cached.get("claim_token") or ""),
                                )
                                return self._public_request_payload(
                                    self._idempotency[cache_key]
                                )
                            return self._public_request_payload(cached)
                        if claim_state == "outcome_unknown":
                            return self._public_request_payload({
                                **cached,
                                "request_status": "outcome_unknown",
                            })
                        if claim_state == "completed":
                            replayed = self._public_request_payload(cached)
                            replayed["replayed"] = True
                            return replayed
                        if claim_state == "failed":
                            return self._public_request_payload({
                                **cached,
                                "request_status": "failed",
                            })
                        raise RuntimeError("无法确认请求执行状态")
                else:
                    self._idempotency[cache_key] = {
                        "session_id": session_id,
                        "request_id": request_id,
                        "request_fingerprint": request_fingerprint,
                        "request_status": "processing",
                        "claimed_at": self._utc_now_iso(),
                        "claim_token": claim_token,
                        "replayed": False,
                    }

            try:
                reply = record.agent.handle(text, request_id=request_id)
            except Exception as exc:
                self._discard_credit_shadow_pending(
                    record,
                    reason="business_execution_not_committed",
                    error_type=type(exc).__name__,
                )
                if cache_key:
                    self._record_outcome_unknown(
                        cache_key,
                        claim_token,
                    )
                raise
            payload = {
                "session_id": session_id,
                "request_id": request_id,
                "request_fingerprint": request_fingerprint,
                "reply": asdict(reply),
                "request_status": "completed",
                "completed_at": self._utc_now_iso(),
                "replayed": False,
            }
            # The result and its request fingerprint share one session snapshot.
            # A crash can therefore lose both or persist both, never only one.
            if request_id:
                record.processed_requests[request_id] = dict(payload)
                while len(record.processed_requests) > 500:
                    record.processed_requests.pop(
                        next(iter(record.processed_requests))
                    )
            try:
                self._persist_session(record)
            except Exception as exc:
                self._discard_credit_shadow_pending(
                    record,
                    reason="business_session_not_saved",
                    error_type=type(exc).__name__,
                )
                if cache_key:
                    self._record_outcome_unknown(
                        cache_key,
                        claim_token,
                    )
                raise
            try:
                record.agent.flush_credit_shadow_runs()
            except Exception as exc:
                _LOGGER.error(
                    "credit shadow post-session flush failed: %s",
                    type(exc).__name__,
                )
            if cache_key:
                self._idempotency[cache_key] = payload
                if self.repository is not None:
                    completed = self.repository.complete_idempotency(
                        session_id,
                        request_id,
                        payload,
                        self._utc_now_iso(),
                        claim_token,
                    )
                    if str(
                        completed.get("request_status") or ""
                    ) != "completed":
                        raise RuntimeError("请求结果状态持久化失败")
            return payload

    def get_request_status(
        self,
        session_id: str,
        request_id: str,
    ) -> Dict[str, Any]:
        """Return a request's durable lifecycle state without executing it."""
        if not session_id or not request_id:
            raise ValueError("session_id 和 request_id 不能为空")

        with self._lock:
            embedded: Optional[Dict[str, Any]] = None
            record = self._sessions.get(session_id)
            if record is not None:
                embedded = record.processed_requests.get(request_id)
            elif self.repository is not None:
                data = self.repository.load_session(session_id)
                if data is None:
                    raise SessionNotFound(session_id)
                processed = data.get("processed_requests") or {}
                if isinstance(processed, dict):
                    candidate = processed.get(request_id)
                    if isinstance(candidate, dict):
                        embedded = dict(candidate)
            else:
                raise SessionNotFound(session_id)

            cached = self._idempotency.get((session_id, request_id))
            if cached is None and self.repository is not None:
                loaded = self.repository.load_idempotency(
                    session_id,
                    request_id,
                )
                if isinstance(loaded, dict):
                    cached = dict(loaded)
                    self._idempotency[(session_id, request_id)] = cached

            if embedded is not None:
                embedded_status = self._request_status(embedded)
                if embedded_status == "completed":
                    token = str((cached or {}).get("claim_token") or "")
                    if (
                        self.repository is not None
                        and token
                        and self._request_status(cached or {}) != "completed"
                    ):
                        self.repository.complete_idempotency(
                            session_id,
                            request_id,
                            embedded,
                            self._utc_now_iso(),
                            token,
                        )
                    result = self._public_request_payload(embedded)
                    result["replayed"] = True
                    return result
                if embedded_status in {
                    "processing",
                    "outcome_unknown",
                    "failed",
                }:
                    return self._public_request_payload(embedded)

            if cached is None:
                raise RequestNotFound(request_id)

            status = self._request_status(cached)
            if status == "processing" and self._processing_is_stale(cached):
                if self.repository is not None:
                    updated = self.repository.mark_idempotency_outcome_unknown(
                        session_id,
                        request_id,
                        self._utc_now_iso(),
                        str(cached.get("claim_token") or ""),
                    )
                    if isinstance(updated, dict):
                        cached = dict(updated)
                else:
                    cached = {
                        **cached,
                        "request_status": "outcome_unknown",
                        "updated_at": self._utc_now_iso(),
                    }
                self._idempotency[(session_id, request_id)] = cached
                status = "outcome_unknown"

            result = self._public_request_payload({
                **cached,
                "request_status": status,
            })
            if status == "completed":
                result["replayed"] = True
            return result

    def run_due_tasks(self) -> Dict[str, list[str]]:
        """Run due tasks for every active session and persist side effects."""
        results: Dict[str, list[str]] = {}
        with self._lock:
            session_ids = set(self._sessions)
            if self.repository is not None:
                session_ids.update(
                    self.repository.list_session_ids_with_tasks()
                )
            for session_id in sorted(session_ids):
                record = self._get_or_load(session_id)
                messages = record.agent.run_due_tasks()
                if messages:
                    results[session_id] = messages
                    self._persist_session(record)
        return results

    def authorize_session(self, session_id: str, token: str) -> None:
        """Validate a client-held session token against its stored hash."""
        with self._lock:
            record = self._get_or_load(session_id)
            expected = record.session_token_hash
            supplied = hashlib.sha256(
                str(token or "").encode("utf-8")
            ).hexdigest()
            if not expected or not hmac.compare_digest(supplied, expected):
                raise SessionAuthError("会话令牌无效")

    @staticmethod
    def _is_replayable(payload: Dict[str, Any]) -> bool:
        """Completed requests always replay their original terminal result."""
        if payload.get("request_status") == "completed":
            return True
        if payload.get("request_status") in {
            "processing",
            "outcome_unknown",
            "failed",
        }:
            return False
        reply = payload.get("reply") or {}
        return bool(reply.get("ok", True)) and reply.get("kind") != "error"

    @staticmethod
    def _request_status(payload: Dict[str, Any]) -> str:
        explicit = str(payload.get("request_status") or "")
        if explicit:
            return explicit
        if "reply" in payload:
            if AgentSessionService._is_replayable(payload):
                return "completed"
            return "failed"
        return "outcome_unknown"

    @staticmethod
    def _require_matching_fingerprint(
        payload: Dict[str, Any],
        request_fingerprint: str,
    ) -> None:
        stored = str(payload.get("request_fingerprint") or "")
        if stored and stored != request_fingerprint:
            raise ValueError("同一 request_id 不能用于不同消息")

    @staticmethod
    def _public_request_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        allowed = (
            "session_id",
            "request_id",
            "request_fingerprint",
            "request_status",
            "reply",
            "claimed_at",
            "completed_at",
            "updated_at",
        )
        result = {
            key: payload[key]
            for key in allowed
            if key in payload
        }
        if "request_status" not in result:
            result["request_status"] = AgentSessionService._request_status(
                payload
            )
        result["replayed"] = bool(payload.get("replayed"))
        return result

    def _utc_now(self) -> datetime:
        value = self._now_utc()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _utc_now_iso(self) -> str:
        return self._utc_now().isoformat(timespec="milliseconds")

    def _processing_is_stale(self, payload: Dict[str, Any]) -> bool:
        claimed_at = str(payload.get("claimed_at") or "")
        try:
            claimed = datetime.fromisoformat(claimed_at)
        except (TypeError, ValueError):
            return True
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)
        age = self._utc_now() - claimed.astimezone(timezone.utc)
        return age > timedelta(seconds=self._processing_stale_seconds)

    def _record_outcome_unknown(
        self,
        cache_key: tuple[str, str],
        claim_token: str,
    ) -> None:
        session_id, request_id = cache_key
        unknown = {
            **self._idempotency.get(cache_key, {}),
            "session_id": session_id,
            "request_id": request_id,
            "request_status": "outcome_unknown",
            "updated_at": self._utc_now_iso(),
            "replayed": False,
        }
        self._idempotency[cache_key] = unknown
        if self.repository is not None:
            updated = self.repository.mark_idempotency_outcome_unknown(
                session_id,
                request_id,
                self._utc_now_iso(),
                claim_token,
            )
            if isinstance(updated, dict):
                self._idempotency[cache_key] = dict(updated)

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

    def list_uploads(self, session_id: str) -> Dict[str, Any]:
        """List imported bill batches for the active browser session."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            batches = [asdict(batch) for batch in store.upload_batches_of(record.user_id)]
            legacy = [
                txn for txn in store.transactions_of(record.user_id)
                if "receipt_import" in txn.tags and not txn.import_batch_id
            ]
            if legacy:
                batches.append({
                    "batch_id": "legacy-imports",
                    "user_id": record.user_id,
                    "filename": "历史上的上传流水",
                    "imported_at": min(txn.timestamp for txn in legacy),
                    "record_count": len(legacy),
                    "total_expense": round(
                        sum(abs(txn.amount) for txn in legacy if txn.amount < 0), 2
                    ),
                    "backend": "",
                    "transaction_ids": [txn.txn_id for txn in legacy],
                    "source": "upload:user",
                    "is_demo": False,
                    "legacy": True,
                })
            return {"session_id": session_id, "uploads": batches}

    def delete_upload(self, session_id: str, batch_id: str) -> Dict[str, Any]:
        """Delete one uploaded batch and persist the resulting session snapshot."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            if batch_id == "legacy-imports":
                result = store.delete_legacy_uploads(record.user_id)
            else:
                result = store.delete_upload_batch(record.user_id, batch_id)
            self._persist_session(record)
            return result

    def get_transaction_detail(self, session_id: str, txn_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            txn = record.agent.store.transactions.get(txn_id)
            if txn is None or txn.user_id != record.user_id:
                raise ValueError(f"交易不存在：{txn_id}")
            account = record.agent.store.accounts.get(txn.account_id)
            card = next(
                (
                    item for item in record.agent.store.cards_of(record.user_id)
                    if item.account_id == txn.account_id
                ),
                None,
            )
            return {
                "session_id": session_id,
                "transaction": {
                    "txn_id": txn.txn_id,
                    "timestamp": txn.timestamp,
                    "amount": txn.amount,
                    "merchant": txn.merchant,
                    "category": txn.category,
                    "channel": txn.channel,
                    "counterparty": txn.counterparty,
                    "note": txn.note,
                    "status": txn.status,
                    "lifecycle_status": txn.lifecycle_status,
                    "submitted_at": txn.submitted_at or txn.timestamp,
                    "completed_at": txn.completed_at or (
                        txn.timestamp if txn.lifecycle_status == "completed" else ""
                    ),
                    "expected_arrival_at": txn.expected_arrival_at,
                    "bank_reference": txn.bank_reference or f"BANK-{txn.txn_id[-10:].upper()}",
                    "failure_reason": txn.failure_reason,
                    "risk_notes": txn.risk_notes,
                    "metadata": txn.metadata,
                    "source": txn.source,
                    "is_live": txn.is_live,
                    "is_demo": txn.is_demo,
                },
                "account": {
                    "account_id": account.account_id,
                    "name": account.name,
                    "tail": account.tail,
                    "balance": account.balance,
                    "available": account.available,
                } if account else None,
                "card": {
                    "card_id": card.card_id,
                    "brand": card.brand,
                    "kind": card.kind,
                    "tail": card.tail,
                    "status": card.status,
                } if card else None,
            }

    def handle_sandbox_callback(
        self,
        session_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply a signed provider callback through the unified state machine."""
        execution_id = str(payload.get("execution_id") or "").strip()
        status = str(payload.get("status") or "").strip().lower()
        if not execution_id or not status:
            raise ValueError("execution_id 和 status 不能为空")
        with self._lock:
            record = self._get_or_load(session_id)
            execution = record.agent.sandbox.gateway.callback(
                execution_id,
                status=status,
                provider_reference=str(payload.get("provider_reference") or ""),
                reason=str(payload.get("reason") or ""),
                payload=dict(payload.get("metadata") or {}),
            )
            self._persist_session(record)
            transaction = record.agent.store.transactions.get(
                execution.reference_id
            )
            detail = (
                self.get_transaction_detail(session_id, transaction.txn_id)["transaction"]
                if transaction is not None else None
            )
            return {
                "session_id": session_id,
                "execution": execution_payload(execution),
                "transaction": detail,
            }

    def get_sandbox_status(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            registry = record.agent.sandbox
            return {
                "session_id": session_id,
                **registry.status(),
                "executions": [
                    execution_payload(item)
                    for item in registry.gateway.list_for_user(record.user_id)
                ],
            }

    def get_market_status(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            return {
                "session_id": session_id,
                **record.agent.market.status(),
            }

    def get_market_overview(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            return {
                "session_id": session_id,
                **build_market_overview(record.agent.market),
            }

    def get_official_data(self, refresh: bool = False) -> Dict[str, Any]:
        """Load official public macro data; refresh only when explicitly asked."""
        return load_official_data(refresh=refresh)

    def simulate_forecast(
        self,
        session_id: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            tool = record.agent.registry.get("forecast.cashflow")
            if tool is None:
                raise ValueError("现金流预测工具未注册")
            result = tool.run(
                record.agent.context(),
                months=int(params.get("months") or 3),
                purchase_amount=float(params.get("purchase_amount") or 0),
                purchase_date=str(params.get("purchase_date") or ""),
                income_change_pct=float(params.get("income_change_pct") or 0),
                extra_monthly_expense=float(params.get("extra_monthly_expense") or 0),
                investment_amount=float(params.get("investment_amount") or 0),
                investment_return_pct=float(params.get("investment_return_pct") or 0),
            )
            reply = AgentReply(
                result.message,
                intent="forecast.cashflow",
                ok=result.ok,
                kind="executed" if result.ok else "error",
                trace=[{
                    "step": 1,
                    "tool": "forecast.cashflow",
                    "args": {
                        key: value for key, value in params.items()
                        if key not in {"session_id"}
                    },
                    "ok": result.ok,
                    "summary": result.summary,
                }],
                data=dict(result.data or {}),
            )
            reply.data["workflow"] = {
                "workflow_id": (
                    f"{record.agent.current_workflow_id or 'WF-SIM'}-SCENARIO"
                ),
                "status": "completed" if result.ok else "blocked",
                "intent": "forecast.cashflow",
                "agents": ["understanding", "advisory", "execution"],
                "steps": [{
                    "tool": "forecast.cashflow",
                    "ok": result.ok,
                    "summary": result.summary,
                }],
                "next_action": "已完成",
            }
            return {
                "session_id": session_id,
                "reply": asdict(reply),
            }

    def get_financial_events(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            tool = record.agent.registry.get("advisor.events")
            if tool is None:
                raise ValueError("事件中心工具未注册")
            result = tool.run(record.agent.context(), action="list")
            return {
                "session_id": session_id,
                "events": result.data.get("events", []),
                "counts": result.data.get("counts", {}),
            }

    def get_pending_receipt(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            cache = record.agent.scratch.get("receipt_cache") or {}
            return {
                "session_id": session_id,
                "available": bool(cache.get("records")),
                "source": cache.get("source", ""),
                "backend": cache.get("backend", ""),
                "backend_label": cache.get("backend_label", ""),
                "records": cache.get("records", []),
                "quality": cache.get("quality", {}),
                "validation": cache.get("validation", {}),
            }

    def update_pending_receipt(
        self,
        session_id: str,
        records: Any,
    ) -> Dict[str, Any]:
        from finagent.vision.receipt_parser import ParsedTransaction
        from finagent.vision.receipt_reader import ReceiptResult

        with self._lock:
            record = self._get_or_load(session_id)
            cache = record.agent.scratch.get("receipt_cache")
            if not cache:
                raise ValueError("当前会话没有待核对的账单")
            def safe_confidence(value: Any, default: float = 1.0) -> float:
                try:
                    return max(0.0, min(float(value), 1.0))
                except (TypeError, ValueError):
                    return default

            parsed = []
            for item in records if isinstance(records, list) else []:
                if not isinstance(item, dict):
                    continue
                merchant = str(item.get("merchant") or "").strip()
                try:
                    amount = float(item.get("amount"))
                except (TypeError, ValueError):
                    continue
                parsed.append(ParsedTransaction(
                    date=str(item.get("date") or "").strip(),
                    time=str(item.get("time") or "").strip(),
                    merchant=merchant,
                    amount=amount,
                    category=str(item.get("category") or "其他").strip() or "其他",
                    confidence=safe_confidence(item.get("confidence")),
                    field_confidence={
                        str(key): safe_confidence(value, 0.5)
                        for key, value in (item.get("field_confidence") or {}).items()
                        if value is not None
                    },
                    needs_review=[
                        str(value)
                        for value in (item.get("needs_review") or [])
                    ],
                    raw_line=str(item.get("raw_line") or ""),
                ))
            if not parsed:
                raise ValueError("核对后的交易记录不能为空")
            result = ReceiptResult(
                ok=True,
                backend=cache.get("backend", ""),
                backend_label=cache.get("backend_label", ""),
                records=parsed,
                raw_text=str(cache.get("raw_text") or ""),
                image_path=cache.get("source", ""),
            )
            cache["records"] = [item.to_payload() for item in parsed]
            cache["quality"] = result.quality
            cache["validation"] = result.validation
            self._persist_session(record)
            return {
                "session_id": session_id,
                "records": cache["records"],
                "quality": result.quality,
                "validation": result.validation,
            }

    def update_financial_event(
        self,
        session_id: str,
        event_id: str,
        action: str,
    ) -> Dict[str, Any]:
        with self._lock:
            record = self._get_or_load(session_id)
            tool = record.agent.registry.get("advisor.events")
            if tool is None:
                raise ValueError("事件中心工具未注册")
            result = tool.run(
                record.agent.context(),
                action=action,
                event_id=event_id,
            )
            self._persist_session(record)
            return {
                "session_id": session_id,
                "ok": result.ok,
                "message": result.message,
                "events": result.data.get("events", []),
                "counts": result.data.get("counts", {}),
            }

    def get_profile_settings(self, session_id: str) -> Dict[str, Any]:
        """Return editable demo profile, surprise preferences, and cards."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            user = store.get_user(record.user_id)
            if user is None:
                raise ValueError(f"用户不存在：{record.user_id}")
            store.ensure_card_ledger()
            prefs = dict(user.preferences or {})
            sanitized_people = self._sanitize_care_people_storage(
                prefs.get("care_people") or []
            )
            if sanitized_people != (prefs.get("care_people") or []):
                prefs["care_people"] = sanitized_people
                user.preferences = prefs
                store.touch(user, source="user:profile")
                self._persist_session(record)
            return {
                "session_id": session_id,
                "user": {
                    "user_id": user.user_id,
                    "name": user.name,
                    "phone": user.phone,
                    "city": user.city,
                    "age": user.age,
                    "occupation": user.occupation,
                    "monthly_income": user.monthly_income,
                    "birthday": user.birthday,
                    "risk_profile": user.risk_profile,
                    "risk_score": user.risk_score,
                    "risk_assessed_at": prefs.get("risk_assessed_at", ""),
                    "risk_assessment_score": prefs.get("risk_assessment_total", 0),
                    "risk_assessment_method": prefs.get("risk_assessment_method", ""),
                    "verified": user.verified,
                    "source": user.source,
                    "updated_at": user.updated_at,
                    "is_demo": user.is_demo,
                },
                "preferences": {
                    "spouse_name": prefs.get("spouse", ""),
                    "spouse_birthday": prefs.get("spouse_birthday", ""),
                    "anniversary": prefs.get("anniversary", ""),
                    "care_people": self._care_people_payload(
                        prefs.get("care_people") or []
                    ),
                    "favorite_gift": list(prefs.get("favorite_gift") or []),
                    "avoid": list(prefs.get("avoid") or []),
                    "notify_channel": prefs.get("notify_channel", "APP"),
                },
                "cards": [asdict(card) for card in store.cards_of(record.user_id)],
                "goals": [asdict(goal) for goal in store.goals_of(record.user_id)],
            }

    def update_profile_settings(self, session_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Update profile and birthday surprise fields for the current session."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            today = store.clock.today()
            user = store.get_user(record.user_id)
            if user is None:
                raise ValueError(f"用户不存在：{record.user_id}")
            if values.get("name") is not None:
                name = str(values["name"]).strip()
                if not name:
                    raise ValueError("姓名不能为空")
                user.name = name[:40]
            if values.get("phone") is not None:
                phone = "".join(ch for ch in str(values["phone"]) if ch.isdigit())
                if phone and (len(phone) != 11 or not phone.startswith("1")):
                    raise ValueError("手机号格式不正确，应为以 1 开头的 11 位数字")
                user.phone = phone
            if values.get("city") is not None:
                user.city = str(values["city"]).strip()[:30]
            if values.get("occupation") is not None:
                user.occupation = str(values["occupation"]).strip()[:60]
            if values.get("age") is not None:
                try:
                    age = int(values["age"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("年龄必须是整数") from exc
                if not 18 <= age <= 100:
                    raise ValueError("年龄必须在 18-100 之间")
                user.age = age
            if values.get("monthly_income") is not None:
                try:
                    income = float(values["monthly_income"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("月收入必须是数字") from exc
                if income < 0:
                    raise ValueError("月收入不能为负数")
                user.monthly_income = round(income, 2)
            if values.get("birthday") is not None:
                birthday = self._validate_birthday(values["birthday"], "生日", today)
                user.birthday = birthday

            prefs = dict(user.preferences or {})
            if values.get("spouse_name") is not None:
                prefs["spouse"] = str(values["spouse_name"]).strip()[:40]
            if values.get("spouse_birthday") is not None:
                prefs["spouse_birthday"] = self._validate_birthday(
                    values["spouse_birthday"], "爱人生日", today
                )
            if values.get("anniversary") is not None:
                prefs["anniversary"] = self._validate_date(
                    values["anniversary"], "纪念日"
                )
            if values.get("care_people") is not None:
                existing_people = {
                    str(item.get("person_id") or ""): item
                    for item in prefs.get("care_people") or []
                    if isinstance(item, dict)
                }
                incoming_people = []
                for item in values["care_people"]:
                    if not isinstance(item, dict):
                        continue
                    person = dict(item)
                    existing = existing_people.get(
                        str(person.get("person_id") or "")
                    )
                    if (
                        existing
                        and not str(person.get("bank_card") or "").strip()
                        and not str(person.get("bank_card_tail") or "").strip()
                    ):
                        old_card = "".join(
                            ch for ch in str(existing.get("bank_card") or "")
                            if ch.isdigit()
                        )
                        person["bank_card_tail"] = (
                            old_card[-4:]
                            or existing.get("bank_card_tail")
                            or ""
                        )
                    incoming_people.append(person)
                prefs["care_people"] = self._normalize_care_people(
                    incoming_people,
                    today,
                )
            if values.get("favorite_gift") is not None:
                prefs["favorite_gift"] = self._list_values(values["favorite_gift"])
            if values.get("avoid") is not None:
                prefs["avoid"] = self._list_values(values["avoid"])
            if values.get("notify_channel") is not None:
                prefs["notify_channel"] = str(values["notify_channel"]).strip()[:20]
            if "care_people" not in values and (
                values.get("spouse_name") is not None
                or values.get("spouse_birthday") is not None
            ):
                people = list(prefs.get("care_people") or [])
                spouse = next(
                    (item for item in people if item.get("relation") in {"配偶", "老婆", "妻子", "爱人"}),
                    None,
                )
                if spouse is None:
                    spouse = {
                        "person_id": f"CARE{len(people) + 1:04d}",
                        "name": prefs.get("spouse", "爱人"),
                        "relation": "配偶",
                        "birthday": prefs.get("spouse_birthday", ""),
                        "birthday_month_day": "",
                        "phone": "",
                        "wechat": "",
                        "alipay": "",
                        "bank_card": "",
                        "bank_card_tail": "",
                        "bank_card_masked": "",
                        "favorite_gift": [],
                        "avoid": [],
                    }
                    people.append(spouse)
                else:
                    spouse["name"] = prefs.get("spouse", spouse.get("name", "爱人"))
                    spouse["birthday"] = prefs.get("spouse_birthday", spouse.get("birthday", ""))
                prefs["care_people"] = people
            user.preferences = prefs
            user.source = "user:profile"
            user.updated_at = store.clock.iso()
            store.log("profile.updated", user_id=user.user_id)
            self._persist_session(record)
            return self.get_profile_settings(session_id)

    def update_goal_settings(
        self,
        session_id: str,
        goal_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Update user-editable goal metadata without changing locked funds."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            goal = store.goals.get(goal_id)
            if goal is None or goal.user_id != record.user_id:
                raise ValueError(f"资金目标不存在：{goal_id}")
            if values.get("name") is not None:
                name = str(values["name"]).strip()
                if not name:
                    raise ValueError("目标名称不能为空")
                goal.name = name[:60]
            if values.get("target_amount") is not None:
                try:
                    target_amount = float(values["target_amount"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("目标金额必须是数字") from exc
                if target_amount <= 0:
                    raise ValueError("目标金额必须大于 0")
                goal.target_amount = round(target_amount, 2)
            if values.get("deadline") is not None:
                goal.deadline = self._validate_date(values["deadline"], "目标日期")
            if values.get("status") is not None:
                status = str(values["status"]).strip()
                if status not in {"active", "achieved"}:
                    raise ValueError("资金目标状态只能是进行中或已完成")
                goal.status = status
            if values.get("note") is not None:
                goal.note = str(values["note"]).strip()[:200]
            goal.source = "user:profile"
            goal.is_demo = False
            store.touch(goal)
            store.log("goal.profile_updated", goal_id=goal.goal_id)
            self._persist_session(record)
            return asdict(goal)

    def create_goal_settings(
        self,
        session_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a profile goal without reserving account funds."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            name = str(values.get("name") or "").strip()
            if not name:
                raise ValueError("目标名称不能为空")
            try:
                target_amount = float(values.get("target_amount"))
            except (TypeError, ValueError) as exc:
                raise ValueError("目标金额必须是数字") from exc
            if target_amount <= 0:
                raise ValueError("目标金额必须大于 0")
            deadline = self._validate_date(values.get("deadline"), "目标日期")
            status = str(values.get("status") or "active").strip()
            if status not in {"active", "achieved"}:
                raise ValueError("资金目标状态只能是进行中或已完成")
            goal = store.add_goal(
                user_id=record.user_id,
                name=name[:60],
                target_amount=target_amount,
                deadline=deadline,
                scenario="profile",
                note=str(values.get("note") or "").strip()[:200],
            )
            goal.status = status
            goal.source = "user:profile"
            goal.is_demo = False
            store.touch(goal)
            store.log("goal.profile_created", goal_id=goal.goal_id)
            self._persist_session(record)
            return asdict(goal)

    def delete_goal_settings(self, session_id: str, goal_id: str) -> Dict[str, Any]:
        """Delete a profile goal and release any funds it still holds."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            goal = store.goals.get(goal_id)
            if goal is None or goal.user_id != record.user_id:
                raise ValueError(f"资金目标不存在：{goal_id}")
            released_amount = 0.0
            account = store.accounts.get(goal.account_id)
            if account is not None and account.user_id == record.user_id and goal.locked_amount > 0:
                released_amount = min(
                    round(float(goal.locked_amount), 2),
                    max(0.0, round(float(account.locked), 2)),
                )
                if released_amount:
                    account.locked = round(max(0.0, account.locked - released_amount), 2)
                    store.touch(account)
                    from finagent.data.card_ledger import sync_cards_for_account
                    sync_cards_for_account(store, account)
            del store.goals[goal_id]
            store.log(
                "goal.profile_deleted",
                goal_id=goal_id,
                released_amount=released_amount,
            )
            self._persist_session(record)
            return {
                "deleted_goal_id": goal_id,
                "released_amount": released_amount,
                "remaining_goals": len(store.goals_of(record.user_id)),
            }

    def create_card_settings(self, session_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            from finagent.data.card_ledger import (
                DEMO_CARD_PASSWORD,
                ensure_card_account,
                hash_card_password,
            )

            record = self._get_or_load(session_id)
            store = record.agent.store
            kind = str(values.get("kind") or "virtual")
            if kind not in {"debit", "credit", "virtual", "supplementary"}:
                raise ValueError("不支持的卡片类型")
            credit_limit = self._positive_number(values.get("credit_limit"), 0.0)
            if credit_limit > 200_000:
                raise ValueError("信用卡额度上限为 200,000 元")
            daily_limit = self._positive_number(values.get("daily_limit"), 20000.0)
            single_limit = self._positive_number(values.get("single_limit"), 5000.0)
            single_limit = min(single_limit, daily_limit)
            requested_status = str(
                values.get("status")
                or "active"
            ).strip()
            if requested_status not in {
                "active", "frozen", "lost", "cancelled", "pending", "rejected",
            }:
                raise ValueError("卡片状态无效")
            card = Card(
                card_id=store.next_id("cards"),
                user_id=record.user_id,
                kind=kind,
                brand=str(values.get("brand") or "演示银行"),
                tail=self._card_tail(values.get("tail")),
                status=requested_status,
                balance=0.0,
                credit_limit=credit_limit if kind == "credit" else 0.0,
                used=0.0,
                payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
                daily_limit=daily_limit,
                single_limit=single_limit,
                online_enabled=bool(values.get("online_enabled", True)),
                overseas_enabled=bool(values.get("overseas_enabled", False)),
                contactless_enabled=bool(values.get("contactless_enabled", True)),
                note=str(values.get("note") or "用户添加卡片")[:100],
                source="user:profile:test",
                is_demo=True,
            )
            store.cards[card.card_id] = card
            store.touch(card)
            ensure_card_account(
                store,
                card,
                account_kind="credit" if kind in {"credit", "supplementary"} else "current",
                account_name=f"{card.brand} {card.tail} 账户",
                allow_fallback_link=False,
            )
            store.log("card.profile_created", card_id=card.card_id)
            self._persist_session(record)
            return asdict(card)

    def update_card_settings(
        self,
        session_id: str,
        card_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._lock:
            from finagent.data.card_ledger import linked_account, verify_card_password

            record = self._get_or_load(session_id)
            store = record.agent.store
            card = store.cards.get(card_id)
            if card is None or card.user_id != record.user_id:
                raise ValueError(f"卡片不存在：{card_id}")
            if values.get("used") is not None:
                raise ValueError("已用额度由交易流水维护，不能在资料设置中手工修改")

            canonical: Dict[str, Any] = {}
            sensitive_changes: Dict[str, Any] = {}
            if values.get("brand") is not None:
                canonical["brand"] = str(values["brand"]).strip()[:100]
            if values.get("note") is not None:
                canonical["note"] = str(values["note"]).strip()[:100]

            requested_status = ""
            if values.get("status") is not None:
                requested_status = str(values["status"]).strip()
                allowed_statuses = {
                    "active", "frozen", "lost", "cancelled", "pending",
                }
                if requested_status not in allowed_statuses:
                    raise ValueError(
                        "卡片状态只能是 active、frozen、lost、cancelled 或 pending"
                    )
                if (
                    requested_status != card.status
                    and (card.status, requested_status) != ("active", "frozen")
                ):
                    raise ValueError(
                        "资料设置只能主动冻结卡片；解冻、激活、挂失恢复或注销"
                        "必须通过对话风控流程办理"
                    )
                if requested_status != card.status:
                    canonical["status"] = requested_status
                    sensitive_changes["status"] = requested_status

            for field, default in (
                ("credit_limit", card.credit_limit),
                ("daily_limit", card.daily_limit),
                ("single_limit", card.single_limit),
            ):
                if values.get(field) is not None:
                    if field == "credit_limit" and card.kind != "credit":
                        raise ValueError("只有信用卡可以调整信用额度")
                    if field in {"daily_limit", "single_limit"} and card.kind == "credit":
                        raise ValueError("信用卡不使用借记卡的单笔/日限额字段")
                    parsed = self._positive_number(values[field], default)
                    if field == "credit_limit" and parsed > 200_000:
                        raise ValueError("信用卡额度上限为 200,000 元")
                    if field == "credit_limit" and parsed < card.used:
                        raise ValueError(
                            f"信用额度不能低于当前已用额度 {card.used:,.2f} 元"
                        )
                    if abs(parsed - float(default)) > 0.009:
                        canonical[field] = parsed
                        sensitive_changes[field] = parsed

            daily_limit = float(canonical.get("daily_limit", card.daily_limit))
            single_limit = float(canonical.get("single_limit", card.single_limit))
            if single_limit > daily_limit:
                if values.get("single_limit") is not None:
                    raise ValueError("单笔限额不能高于日限额")
                canonical["single_limit"] = daily_limit
                sensitive_changes["single_limit"] = daily_limit
            for field in ("online_enabled", "overseas_enabled", "contactless_enabled"):
                if values.get(field) is not None:
                    enabled = self._strict_bool(values[field], field)
                    if enabled != bool(getattr(card, field)):
                        canonical[field] = enabled
                        sensitive_changes[field] = enabled

            if sensitive_changes and card.status != "active":
                raise ValueError("冻结、挂失或待激活的卡片不能直接修改敏感设置")

            if sensitive_changes:
                self._authorize_card_step_up(
                    record,
                    card,
                    canonical,
                    str(values.get("confirmation_token") or ""),
                    str(values.get("payment_password") or ""),
                    verify_card_password,
                )

            # Card type and tail identify the linked bank account and are immutable.
            for field in ("brand", "note"):
                if field in canonical:
                    setattr(card, field, canonical[field])
            if "status" in canonical:
                card.status = str(canonical["status"])
            for field in ("credit_limit", "daily_limit", "single_limit"):
                if field in canonical:
                    setattr(card, field, float(canonical[field]))
            for field in ("online_enabled", "overseas_enabled", "contactless_enabled"):
                if field in canonical:
                    setattr(card, field, bool(canonical[field]))
            card.source = "user:profile"
            card.is_demo = False
            account = linked_account(store, card)
            if account is not None:
                account.status = card.status
                if card.kind == "credit":
                    account.credit_limit = card.credit_limit
                    card.used = round(max(account.balance, 0.0), 2)
                else:
                    card.used = 0.0
                    card.credit_limit = 0.0
                store.touch(account)
            store.touch(card)
            store.log(
                "card.profile_updated",
                card_id=card.card_id,
                account_id=card.account_id,
            )
            self._persist_session(record)
            return asdict(card)

    def _authorize_card_step_up(
        self,
        record: SessionRecord,
        card: Card,
        changes: Dict[str, Any],
        confirmation_token: str,
        payment_password: str,
        verify_password: Callable[[Card, str], bool],
    ) -> None:
        """Require a single-use challenge plus the card PIN for risky settings."""
        now = time.monotonic()
        self._step_up_challenges = {
            token: challenge
            for token, challenge in self._step_up_challenges.items()
            if challenge.expires_at > now
        }
        digest = hashlib.sha256(
            json.dumps(
                changes,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not confirmation_token:
            token = secrets.token_urlsafe(24)
            self._step_up_challenges[token] = StepUpChallenge(
                token=token,
                session_id=record.session_id,
                card_id=card.card_id,
                digest=digest,
                expires_at=now + self._step_up_ttl_seconds,
            )
            raise StepUpRequired(
                "该修改涉及额度、限额、交易开关或卡片状态，"
                "需要输入该卡 6 位交易密码确认。",
                {
                    "confirmation_token": token,
                    "expires_in_seconds": self._step_up_ttl_seconds,
                    "preview": dict(changes),
                },
            )

        challenge = self._step_up_challenges.get(confirmation_token)
        if (
            challenge is None
            or challenge.expires_at <= now
            or challenge.session_id != record.session_id
            or challenge.card_id != card.card_id
            or not hmac.compare_digest(challenge.digest, digest)
        ):
            self._step_up_challenges.pop(confirmation_token, None)
            raise ValueError("二次确认已失效或修改内容发生变化，请重新保存")
        if not verify_password(card, payment_password):
            challenge.attempts += 1
            if challenge.attempts >= 5:
                self._step_up_challenges.pop(confirmation_token, None)
                raise ValueError("交易密码连续错误次数过多，请重新发起修改")
            raise ValueError(
                f"交易密码验证失败，还可尝试 {5 - challenge.attempts} 次"
            )
        self._step_up_challenges.pop(confirmation_token, None)

    @staticmethod
    def _strict_bool(value: Any, field: str) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{field} 必须是布尔值")

    def delete_card_settings(self, session_id: str, card_id: str) -> Dict[str, Any]:
        """Delete an empty demo card without creating orphan funds."""
        with self._lock:
            from finagent.data.card_ledger import linked_account

            record = self._get_or_load(session_id)
            store = record.agent.store
            card = store.cards.get(card_id)
            if card is None or card.user_id != record.user_id:
                raise ValueError(f"卡片不存在：{card_id}")
            account = linked_account(store, card)
            account_balance = max(float(account.balance if account else 0.0), 0.0)
            account_locked = max(float(account.locked if account else 0.0), 0.0)
            if (
                max(float(card.balance), 0.0) > 0.01
                or max(float(card.used), 0.0) > 0.01
                or max(float(card.locked), 0.0) > 0.01
                or account_balance > 0.01
                or account_locked > 0.01
            ):
                raise ValueError(
                    "该卡仍关联余额、已用额度或锁定资金，不能直接删除；"
                    "请先划转资金、结清欠款或释放锁定金额。"
                )
            del store.cards[card_id]
            store.log("card.profile_deleted", card_id=card_id)
            self._persist_session(record)
            return {
                "deleted_card_id": card_id,
                "remaining_cards": len(store.cards_of(record.user_id)),
            }

    @staticmethod
    def _validate_date(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return datetime.fromisoformat(text[:10]).date().isoformat()
        except ValueError as exc:
            raise ValueError(f"{label}格式应为 YYYY-MM-DD") from exc

    @classmethod
    def _validate_birthday(cls, value: Any, label: str, today: date) -> str:
        text = cls._validate_date(value, label)
        if text and text > today.isoformat():
            raise ValueError(f"{label}不能晚于今天")
        return text

    @staticmethod
    def _validate_month_day(value: Any, label: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.fullmatch(r"(\d{1,2})[-/月](\d{1,2})日?", text)
        if not match:
            raise ValueError(f"{label}格式应为 MM-DD，例如 11-20")
        month, day = int(match.group(1)), int(match.group(2))
        try:
            date(2000, month, day)
        except ValueError as exc:
            raise ValueError(f"{label}不是有效的月日") from exc
        return f"{month:02d}-{day:02d}"

    @staticmethod
    def _list_values(value: Any) -> list[str]:
        if isinstance(value, list):
            parts = value
        else:
            text = str(value or "").replace("，", ",").replace("、", ",")
            parts = text.split(",")
        return [str(item).strip()[:40] for item in parts if str(item).strip()]

    @staticmethod
    def _care_people_payload(value: Any) -> list[dict[str, Any]]:
        people = []
        for item in value:
            if not isinstance(item, dict):
                continue
            person = dict(item)
            for field in ("phone", "wechat", "alipay", "bank_name", "bank_card"):
                person.setdefault(field, "")
            bank_card = "".join(ch for ch in str(person.get("bank_card") or "") if ch.isdigit())
            tail = bank_card[-4:] or str(person.get("bank_card_tail") or "")[-4:]
            person["bank_card"] = ""
            person["bank_card_tail"] = tail
            person["bank_card_masked"] = f"**** **** **** {tail}" if tail else ""
            people.append(person)
        return people

    @staticmethod
    def _sanitize_care_people_storage(value: Any) -> list[dict[str, Any]]:
        people = []
        for item in value:
            if not isinstance(item, dict):
                continue
            person = dict(item)
            card = "".join(
                ch for ch in str(person.get("bank_card") or "")
                if ch.isdigit()
            )
            tail = card[-4:] or str(person.get("bank_card_tail") or "")[-4:]
            person["bank_card"] = ""
            person["bank_card_tail"] = tail
            person["bank_card_masked"] = (
                f"**** **** **** {tail}" if tail else ""
            )
            people.append(person)
        return people

    def _normalize_care_people(self, value: Any, today: date) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("关心的人必须是列表")
        from finagent.tools.card_tools import luhn_ok

        people = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            phone = "".join(ch for ch in str(item.get("phone") or "") if ch.isdigit())
            if phone and (len(phone) != 11 or not phone.startswith("1")):
                raise ValueError(f"{name}的手机号格式不正确")
            bank_name = str(item.get("bank_name") or "").strip()[:40]
            bank_card = "".join(ch for ch in str(item.get("bank_card") or "") if ch.isdigit())
            bank_card_tail = str(item.get("bank_card_tail") or "").strip()
            if bank_card:
                if not bank_name:
                    raise ValueError(
                        f"{name}已填写银行卡号，请同时填写开户银行。"
                        "银行卡号应为 16-19 位数字，可包含空格或短横线。"
                    )
                if not 16 <= len(bank_card) <= 19:
                    raise ValueError(
                        f"{name}的银行卡号格式不正确：应为 16-19 位数字，"
                        f"当前为 {len(bank_card)} 位。"
                    )
                if not bank_card.startswith("62"):
                    raise ValueError(
                        f"{name}的银行卡号格式不正确：境内银联储蓄卡通常以 62 开头，"
                        f"当前前两位为 {bank_card[:2]}。"
                    )
                if not luhn_ok(bank_card):
                    raise ValueError(
                        f"{name}的银行卡号未通过 Luhn 校验，通常是某一位数字输错。"
                        f"当前为 {len(bank_card)} 位且以 62 开头，请逐位核对卡号。"
                    )
                bank_card_tail = bank_card[-4:]
            if bank_card_tail and (
                len(bank_card_tail) != 4 or not bank_card_tail.isdigit()
            ):
                raise ValueError(f"{name}的银行卡后四位格式不正确")
            birthday = self._validate_birthday(
                item.get("birthday"),
                f"{name}的生日",
                today,
            )
            birthday_month_day = (
                ""
                if birthday
                else self._validate_month_day(
                    item.get("birthday_month_day"),
                    f"{name}的生日月日",
                )
            )
            people.append({
                "person_id": str(item.get("person_id") or f"CARE{index + 1:04d}")[:40],
                "name": name[:40],
                "relation": str(item.get("relation") or "关心的人").strip()[:20],
                "birthday": birthday,
                "birthday_month_day": birthday_month_day,
                "anniversary": self._validate_date(item.get("anniversary"), f"{name}的纪念日"),
                "phone": phone,
                "wechat": str(item.get("wechat") or "").strip()[:100],
                "alipay": str(item.get("alipay") or "").strip()[:100],
                "bank_name": bank_name,
                "bank_card": "",
                "bank_card_tail": bank_card_tail,
                "bank_card_masked": (
                    f"**** **** **** {bank_card_tail}" if bank_card_tail else ""
                ),
                "favorite_gift": self._list_values(item.get("favorite_gift")),
                "avoid": self._list_values(item.get("avoid")),
            })
        return people

    @staticmethod
    def _positive_number(value: Any, default: float) -> float:
        if value in (None, ""):
            return round(float(default), 2)
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("额度必须是数字") from exc
        if number < 0:
            raise ValueError("额度不能为负数")
        return round(number, 2)

    @staticmethod
    def _card_tail(value: Any) -> str:
        tail = str(value or "****").strip()[-4:]
        if tail != "****" and not tail.isdigit():
            raise ValueError("卡号后四位必须是数字")
        return tail

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
        agent = self._build_agent(store, str(data["user_id"]))
        agent.risk_answers = dict(data.get("risk_answers") or {})
        agent.risk_level = data.get("risk_level")
        agent.risk_assessed_at = str(data.get("risk_assessed_at") or "")
        agent.risk_inference_confirmed_at = str(
            data.get("risk_inference_confirmed_at") or ""
        )
        if agent.risk_level is not None:
            from finagent.risk.questionnaire import CUSTOMER_LABELS

            user = store.get_user(agent.user_id)
            if user is not None:
                prefs = dict(user.preferences or {})
                prefs.update({
                    "risk_profile_label": CUSTOMER_LABELS.get(agent.risk_level, ""),
                    "risk_score": agent.risk_level,
                    "risk_assessed_at": agent.risk_assessed_at,
                    "risk_assessment_method": prefs.get(
                        "risk_assessment_method", "legacy_session_restore"
                    ),
                })
                user.risk_profile = CUSTOMER_LABELS.get(agent.risk_level, user.risk_profile)
                user.risk_score = agent.risk_level
                user.preferences = prefs
                user.updated_at = store.clock.iso()
                store.touch(user)
        agent.restore_pending(data.get("pending"))
        agent.restore_paused_questionnaire(data.get("paused_questionnaire"))
        agent.session.history = list(data.get("history") or [])
        agent.session.transcript = list(data.get("transcript") or [])
        agent.session.turn = int(data.get("turn") or 0)
        agent.last_intent_name = str(data.get("last_intent_name") or "")
        last_reply = data.get("last_reply")
        if isinstance(last_reply, dict):
            agent.last_reply = AgentReply(**last_reply)
        agent.restore_reply_history(data.get("reply_history"))
        if (
            not agent.reply_history
            and agent.last_reply is not None
            and agent.last_reply.intent != "explain.why"
        ):
            previous = ""
            for item in reversed(agent.session.transcript):
                previous = str(item.get("user") or "")
                if previous:
                    break
            agent.remember_reply(previous, agent.last_reply)
        record = SessionRecord(
            session_id=session_id,
            user_id=str(data["user_id"]),
            channel=str(data["channel"]),
            created_at=str(data["created_at"]),
            agent=agent,
            version=int(data.get("version") or 0),
            session_token_hash=str(data.get("session_token_hash") or ""),
            processed_requests=dict(data.get("processed_requests") or {}),
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
        try:
            new_version = self.repository.save_session(
                session_id=record.session_id,
                user_id=record.user_id,
                channel=record.channel,
                created_at=record.created_at,
                expected_version=record.version,
                payload={
                    "store": agent.store.snapshot(),
                    "risk_answers": dict(agent.risk_answers),
                    "risk_level": agent.risk_level,
                    "risk_assessed_at": agent.risk_assessed_at,
                    "risk_inference_confirmed_at": agent.risk_inference_confirmed_at,
                    "session_token_hash": record.session_token_hash,
                    "processed_requests": dict(record.processed_requests),
                    "pending": agent.export_pending(),
                    "paused_questionnaire": agent.export_paused_questionnaire(),
                    "history": list(agent.session.history),
                    "transcript": list(agent.session.transcript),
                    "turn": agent.session.turn,
                    "last_intent_name": agent.last_intent_name,
                    "last_reply": asdict(agent.last_reply) if agent.last_reply is not None else None,
                    "reply_history": agent.export_reply_history(),
                },
            )
        except OptimisticConcurrencyError:
            with self._lock:
                if self._sessions.get(record.session_id) is record:
                    self._sessions.pop(record.session_id, None)
            raise
        record.version = (
            int(new_version)
            if new_version is not None else record.version + 1
        )

    @staticmethod
    def _session_payload(record: SessionRecord) -> Dict[str, Any]:
        return {
            "session_id": record.session_id,
            "user_id": record.user_id,
            "channel": record.channel,
            "created_at": record.created_at,
        }
