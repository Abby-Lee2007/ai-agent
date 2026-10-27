"""Session orchestration shared by HTTP, H5, and IM adapters."""
from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Optional

from finagent.agent.core import AgentReply, BankingAgent
from finagent.data.models import Card
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
                if self._is_replayable(cached):
                    cached["replayed"] = True
                    return cached
                self._idempotency.pop(cache_key, None)
            if cache_key and self.repository is not None:
                cached = self.repository.load_idempotency(session_id, request_id)
                if cached is not None and self._is_replayable(cached):
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

    @staticmethod
    def _is_replayable(payload: Dict[str, Any]) -> bool:
        """Only successful responses are safe to replay forever."""
        reply = payload.get("reply") or {}
        return bool(reply.get("ok", True)) and reply.get("kind") != "error"

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

    def get_profile_settings(self, session_id: str) -> Dict[str, Any]:
        """Return editable demo profile, surprise preferences, and cards."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            user = store.get_user(record.user_id)
            if user is None:
                raise ValueError(f"用户不存在：{record.user_id}")
            store.ensure_card_ledger()
            prefs = user.preferences or {}
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
                if len(phone) < 7:
                    raise ValueError("手机号格式不正确")
                user.phone = phone[:20]
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
                prefs["care_people"] = self._normalize_care_people(
                    values["care_people"],
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
                        "phone": "",
                        "wechat": "",
                        "alipay": "",
                        "bank_card": "",
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
            balance = self._positive_number(values.get("balance"), 0.0)
            credit_limit = self._positive_number(values.get("credit_limit"), 0.0)
            card = Card(
                card_id=store.next_id("cards"),
                user_id=record.user_id,
                kind=kind,
                brand=str(values.get("brand") or "演示银行"),
                tail=self._card_tail(values.get("tail")),
                status=str(values.get("status") or "active"),
                balance=0.0 if kind in {"credit", "supplementary"} else balance,
                credit_limit=credit_limit,
                used=self._positive_number(values.get("used"), 0.0),
                payment_password_hash=hash_card_password(DEMO_CARD_PASSWORD),
                daily_limit=self._positive_number(values.get("daily_limit"), 20000.0),
                single_limit=self._positive_number(values.get("single_limit"), 5000.0),
                online_enabled=bool(values.get("online_enabled", True)),
                overseas_enabled=bool(values.get("overseas_enabled", False)),
                contactless_enabled=bool(values.get("contactless_enabled", True)),
                note=str(values.get("note") or "用户添加卡片")[:100],
                source="user:profile",
                is_demo=False,
            )
            store.cards[card.card_id] = card
            store.touch(card)
            ensure_card_account(
                store,
                card,
                account_kind="credit" if kind in {"credit", "supplementary"} else "current",
                account_name=f"{card.brand} {card.tail} 账户",
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
            record = self._get_or_load(session_id)
            store = record.agent.store
            card = store.cards.get(card_id)
            if card is None or card.user_id != record.user_id:
                raise ValueError(f"卡片不存在：{card_id}")
            # Card type and tail identify the linked bank account and are immutable.
            for field in ("brand", "status", "note"):
                if values.get(field) is not None:
                    setattr(card, field, str(values[field]).strip()[:100])
            for field, default in (
                ("credit_limit", card.credit_limit),
                ("used", card.used),
                ("daily_limit", card.daily_limit),
                ("single_limit", card.single_limit),
            ):
                if values.get(field) is not None:
                    setattr(card, field, self._positive_number(values[field], default))
            for field in ("online_enabled", "overseas_enabled", "contactless_enabled"):
                if values.get(field) is not None:
                    setattr(card, field, bool(values[field]))
            card.source = "user:profile"
            card.is_demo = False
            store.touch(card)
            store.log("card.profile_updated", card_id=card.card_id)
            self._persist_session(record)
            return asdict(card)

    def delete_card_settings(self, session_id: str, card_id: str) -> Dict[str, Any]:
        """Delete a demo card without changing account balances."""
        with self._lock:
            record = self._get_or_load(session_id)
            store = record.agent.store
            card = store.cards.get(card_id)
            if card is None or card.user_id != record.user_id:
                raise ValueError(f"卡片不存在：{card_id}")
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
            person["bank_card_masked"] = (
                f"**** **** **** {bank_card[-4:]}" if bank_card else ""
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
            people.append({
                "person_id": str(item.get("person_id") or f"CARE{index + 1:04d}")[:40],
                "name": name[:40],
                "relation": str(item.get("relation") or "关心的人").strip()[:20],
                "birthday": self._validate_birthday(
                    item.get("birthday"),
                    f"{name}的生日",
                    today,
                ),
                "anniversary": self._validate_date(item.get("anniversary"), f"{name}的纪念日"),
                "phone": phone,
                "wechat": str(item.get("wechat") or "").strip()[:100],
                "alipay": str(item.get("alipay") or "").strip()[:100],
                "bank_name": bank_name,
                "bank_card": bank_card,
                "bank_card_masked": (
                    f"**** **** **** {bank_card[-4:]}" if bank_card else ""
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
        agent = BankingAgent(store, str(data["user_id"]), llm_mode=self.llm_mode)
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
                "paused_questionnaire": agent.export_paused_questionnaire(),
                "history": list(agent.session.history),
                "transcript": list(agent.session.transcript),
                "turn": agent.session.turn,
                "last_intent_name": agent.last_intent_name,
                "last_reply": asdict(agent.last_reply) if agent.last_reply is not None else None,
                "reply_history": agent.export_reply_history(),
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
