"""Natural-language collection for the credit-risk model input.

The full UCI-compatible feature surface remains available for compatibility,
but the application model requires only the reproducible top-five features:
X6, X7, X8, X9, and X1.  Missing values are never imputed.  Sensitive optional
fields remain blocked from formal decision use.  The top-five artifact is a
separately trained reduced variant, not the missing original 23-feature model.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple


FEATURE_ORDER: Tuple[str, ...] = tuple(
    f"X{index}" for index in range(1, 24)
)
REQUIRED_FEATURE_ORDER: Tuple[str, ...] = (
    "X1",
    "X3",
    "X6",
    "X7",
    "X8",
    "X9",
    "X10",
    "X11",
    "X12",
    "X13",
    "X18",
    "X19",
    "X20",
    "X21",
    "X22",
    "X23",
)
REQUIRED_MODEL_FEATURES: Tuple[str, ...] = (
    "REPAYMENT_STATUS_6",
    "REPAYMENT_AMOUNT_6",
    "STATEMENT_AMOUNT_2",
    "X1",
    "X3",
)
REQUIRED_FEATURE_KEYS = frozenset(REQUIRED_FEATURE_ORDER)
TOP5_MODEL_NAME = "credit-risk-grouped-top5-random-forest"
TOP5_MODEL_VERSION = "5.0.0"
TOP5_FEATURE_VERSION = "uci_grouped_top5_mdi_v5"
FEATURE_GROUPS: Tuple[str, ...] = (
    "basic_profile",
    "repayment_status",
    "payment_amounts",
    "statement_amounts",
)
GROUP_LABELS = {
    "basic_profile": "基本资料",
    "repayment_status": "六个月还款状态",
    "statement_amounts": "六个月账单金额",
    "payment_amounts": "六个月还款金额",
}


@dataclass(frozen=True)
class ModelFeatureSpec:
    key: str
    label: str
    group: str
    kind: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    allowed_values: Tuple[float, ...] = ()
    required: bool = True
    core: bool = True
    sensitive: bool = False
    allowed_for_decision: bool = True
    user_collectable: bool = True
    explanation: str = ""

    def validate(self, value: Any) -> Tuple[bool, Optional[float], str]:
        if isinstance(value, bool):
            return False, None, "不接受布尔值"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False, None, "必须是数字"
        if not math.isfinite(number):
            return False, None, "必须是有限数字"
        if self.kind in {"integer", "category"} and not number.is_integer():
            return False, None, "必须是整数编码"
        if self.allowed_values and number not in self.allowed_values:
            allowed = "、".join(str(item) for item in self.allowed_values)
            return False, None, f"可选值为 {allowed}"
        if self.minimum is not None and number < self.minimum:
            return False, None, f"不能小于 {self.minimum:g}"
        if self.maximum is not None and number > self.maximum:
            return False, None, f"不能大于 {self.maximum:g}"
        return True, number, ""


def _spec(
    key: str,
    label: str,
    group: str,
    kind: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    allowed_values: Tuple[float, ...] = (),
    sensitive: bool = False,
    allowed_for_decision: bool = True,
    explanation: str = "",
) -> ModelFeatureSpec:
    return ModelFeatureSpec(
        key=key,
        label=label,
        group=group,
        kind=kind,
        minimum=minimum,
        maximum=maximum,
        allowed_values=allowed_values,
        sensitive=sensitive,
        allowed_for_decision=allowed_for_decision,
        explanation=explanation,
    )


FEATURE_SPECS: Dict[str, ModelFeatureSpec] = {
    "X1": _spec(
        "X1", "当前信用额度", "basic_profile", "amount",
        minimum=10_000, maximum=1_000_000,
        explanation="用于模型中的现有信用额度口径，不把本次期望额度当作当前额度。",
    ),
    "X2": _spec(
        "X2", "性别", "basic_profile", "category",
        allowed_values=(1, 2), sensitive=True, allowed_for_decision=False,
        explanation="历史模型特征，仅用于完整性检查和公平性审计，未经政策审查不进入正式授信决策。",
    ),
    "X3": _spec(
        "X3", "教育程度", "basic_profile", "category",
        allowed_values=(1, 2, 3, 4), sensitive=True, allowed_for_decision=False,
        explanation="历史模型特征，未经公平性审查不进入正式授信决策。",
    ),
    "X4": _spec(
        "X4", "婚姻状况", "basic_profile", "category",
        allowed_values=(1, 2, 3), sensitive=True, allowed_for_decision=False,
        explanation="历史模型特征，未经公平性审查不进入正式授信决策。",
    ),
    "X5": _spec(
        "X5", "年龄", "basic_profile", "integer",
        minimum=21, maximum=75,
        explanation="用于模型完整性检查。",
    ),
}
for _index in range(6, 12):
    FEATURE_SPECS[f"X{_index}"] = _spec(
        f"X{_index}",
        f"过去第 {_index - 5} 个月还款状态",
        "repayment_status",
        "category",
        minimum=-2,
        maximum=9,
        explanation="按模型原始还款状态编码采集；不得用普通消费流水代替。",
    )

_BILL_RANGES = {
    "X12": (-165_580, 964_511),
    "X13": (-69_777, 983_931),
    "X14": (-61_506, 855_086),
    "X15": (-81_334, 891_586),
    "X16": (-81_334, 927_171),
    "X17": (-339_603, 961_664),
}
for _index in range(12, 18):
    _key = f"X{_index}"
    _minimum, _maximum = _BILL_RANGES[_key]
    FEATURE_SPECS[_key] = _spec(
        _key,
        f"过去第 {_index - 11} 个月账单金额",
        "statement_amounts",
        "amount",
        minimum=_minimum,
        maximum=_maximum,
        explanation="使用历史模型账单金额口径；普通消费流水不能直接替代正式账单。",
    )

_PAYMENT_RANGES = {
    "X18": (0, 873_552),
    "X19": (0, 1_215_471),
    "X20": (0, 896_040),
    "X21": (0, 621_000),
    "X22": (0, 426_529),
    "X23": (0, 528_666),
}
for _index in range(18, 24):
    _key = f"X{_index}"
    _minimum, _maximum = _PAYMENT_RANGES[_key]
    FEATURE_SPECS[_key] = _spec(
        _key,
        f"过去第 {_index - 17} 个月还款金额",
        "payment_amounts",
        "amount",
        minimum=_minimum,
        maximum=_maximum,
        explanation="使用历史模型的实际还款金额口径。",
    )


@dataclass
class FeatureValue:
    status: str = "missing"
    value: Optional[float] = None
    source: str = ""
    message: str = ""
    decision_allowed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "source": self.source,
            "message": self.message,
            "decision_allowed": self.decision_allowed,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FeatureValue":
        if not isinstance(data, dict):
            return cls()
        status = str(data.get("status") or "missing")
        if status not in {"missing", "invalid", "declined", "collected", "verified"}:
            status = "missing"
        value = data.get("value")
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
                status = "invalid"
        decision_allowed = data.get("decision_allowed", True)
        if not isinstance(decision_allowed, bool):
            decision_allowed = str(decision_allowed).strip().lower() not in {
                "false",
                "0",
                "no",
                "off",
            }
        return cls(
            status=status,
            value=value,
            source=str(data.get("source") or ""),
            message=str(data.get("message") or "")[:200],
            decision_allowed=decision_allowed,
        )


@dataclass
class CreditRiskFeatureCollection:
    values: Dict[str, FeatureValue] = field(default_factory=dict)
    processed_groups: Tuple[str, ...] = ()
    last_errors: Tuple[str, ...] = ()

    @classmethod
    def from_context(
        cls,
        *,
        user: Any,
        cards: Sequence[Any] = (),
        accounts: Sequence[Any] = (),
        previous: Optional[Dict[str, Any]] = None,
    ) -> "CreditRiskFeatureCollection":
        previous = previous if isinstance(previous, dict) else {}
        raw_values = previous.get("values")
        raw_values = raw_values if isinstance(raw_values, dict) else {}
        values = {
            key: FeatureValue.from_dict(raw_values.get(key))
            for key in FEATURE_ORDER
        }
        processed = {
            str(group)
            for group in previous.get("processed_groups") or ()
            if str(group) in FEATURE_GROUPS
        }
        last_errors = tuple(
            str(item)[:200]
            for item in previous.get("last_errors") or ()
            if str(item).strip()
        )
        collection = cls(
            values=values,
            processed_groups=tuple(
                group for group in FEATURE_GROUPS if group in processed
            ),
            last_errors=last_errors,
        )
        for key in FEATURE_ORDER:
            item = collection.values[key]
            spec = FEATURE_SPECS[key]
            item.decision_allowed = spec.allowed_for_decision
            if item.status in {"collected", "verified"}:
                valid, number, message = spec.validate(item.value)
                if not valid or number is None:
                    collection.values[key] = FeatureValue(
                        status="invalid",
                        source=item.source,
                        message=message,
                        decision_allowed=spec.allowed_for_decision,
                    )
                else:
                    item.value = number
        collection._fill_existing_credit_limit(user, cards, accounts)
        collection._fill_age(user)
        collection._mark_completed_groups()
        return collection

    def _set_business_value(
        self,
        key: str,
        value: Any,
        source: str,
        *,
        verified: bool,
    ) -> None:
        current = self.values[key]
        if current.status in {"collected", "verified"}:
            return
        spec = FEATURE_SPECS[key]
        valid, number, _ = spec.validate(value)
        if valid and number is not None:
            self.values[key] = FeatureValue(
                status="verified" if verified else "collected",
                value=number,
                source=source,
                decision_allowed=spec.allowed_for_decision,
            )

    def _fill_existing_credit_limit(self, user, cards, accounts) -> None:
        active_credit_cards = [
            card for card in cards
            if card.user_id == user.user_id
            and card.kind == "credit"
            and card.status == "active"
        ]
        if len(active_credit_cards) != 1:
            return
        card = active_credit_cards[0]
        account = next(
            (
                item for item in accounts
                if item.account_id == card.account_id
                and item.user_id == user.user_id
                and item.kind == "credit"
            ),
            None,
        )
        if account is None:
            return
        self._set_business_value(
            "X1",
            account.credit_limit,
            "existing_credit_account",
            verified=True,
        )

    def _fill_sensitive_profile(self, user) -> None:
        gender = str(getattr(user, "gender", "") or "").strip().lower()
        code = {"male": 1, "m": 1, "男": 1, "female": 2, "f": 2, "女": 2}.get(gender)
        if code is not None:
            self._set_business_value("X2", code, "user_profile", verified=False)

    def _fill_age(self, user) -> None:
        try:
            age = int(getattr(user, "age", 0) or 0)
        except (TypeError, ValueError):
            age = 0
        if age:
            self._set_business_value("X5", age, "user_profile", verified=False)

    def _mark_completed_groups(self) -> None:
        completed = set(self.processed_groups)
        for group in FEATURE_GROUPS:
            keys = self._required_keys_for_group(group)
            if not keys:
                continue
            if all(
                self.values[key].status in {"collected", "verified"}
                for key in keys
            ):
                completed.add(group)
        self.processed_groups = tuple(
            group for group in FEATURE_GROUPS if group in completed
        )

    def next_group(self) -> Optional[str]:
        for group in FEATURE_GROUPS:
            keys = self._required_keys_for_group(group)
            if any(
                self.values[key].status not in {"collected", "verified"}
                for key in keys
            ):
                return group
        return None

    @staticmethod
    def _required_keys_for_group(group: str) -> Tuple[str, ...]:
        return tuple(
            key for key in REQUIRED_FEATURE_ORDER
            if FEATURE_SPECS[key].group == group
        )

    def apply_text(self, text: str) -> "CreditRiskFeatureCollection":
        group = self.next_group()
        raw = (text or "").strip()
        if group is None:
            basic_updates = self._parse_basic_profile(raw)
            explicit_updates = self._parse_explicit_history_update(raw)
            if basic_updates or explicit_updates:
                self.last_errors = tuple(self._apply_parsed_updates({
                    **basic_updates,
                    **explicit_updates,
                }))
            elif re.search(r"还款状态|账单金额|还款金额", raw):
                self.last_errors = (
                    "补充资料没有识别到完整输入，请检查字段数量和格式。",
                )
            else:
                self.last_errors = ()
            self._mark_completed_groups()
            return self
        basic_updates = self._parse_basic_profile(raw)
        if group == "basic_profile" and not basic_updates:
            bare_amount = re.fullmatch(
                r"(-?\d+(?:\.\d+)?)\s*(万|万元|元)?",
                raw,
            )
            if bare_amount:
                value = float(bare_amount.group(1))
                if bare_amount.group(2) and bare_amount.group(2).startswith("万"):
                    value *= 10_000
                basic_updates = {"X1": value}
        if re.fullmatch(
            r"(跳过本组|跳过|不提供|不清楚|不知道|暂时没有|无法提供)",
            raw,
        ):
            required_keys = self._required_keys_for_group(group)
            if required_keys:
                self.last_errors = (
                    "当前组包含模型必需资料，信用额度和最近四个月还款状态"
                    "不能跳过；如暂时无法提供，本次暂不能运行信用风险评估。",
                )
                return self
            for key, spec in FEATURE_SPECS.items():
                if (
                    spec.group == group
                    and self.values[key].status in {"missing", "invalid"}
                ):
                    self.values[key] = FeatureValue(status="declined", source="user")
            self.processed_groups = tuple(dict.fromkeys(
                (*self.processed_groups, group)
            ))
            self.last_errors = ()
            return self
        declined_keys = self._parse_declined_fields(raw)
        if declined_keys:
            self._apply_parsed_updates(basic_updates)
            required_declined = [
                key for key in declined_keys if key in REQUIRED_FEATURE_KEYS
            ]
            for key in declined_keys:
                if key in REQUIRED_FEATURE_KEYS:
                    continue
                spec = FEATURE_SPECS[key]
                self.values[key] = FeatureValue(
                    status="declined",
                    source="user",
                    message="用户暂不提供",
                    decision_allowed=spec.allowed_for_decision,
                )
            self.last_errors = (
                (
                    "信用额度和最近四个月还款状态属于模型必需资料，"
                    "不能标记为不提供。"
                )
                if required_declined
                else ()
            )
            self._mark_completed_groups()
            return self

        # Labeled basic-profile values are safe to update at any point.  This
        # lets a user correct an earlier answer without resubmitting the whole
        # application, even while a later feature group is being collected.
        explicit_updates = self._parse_explicit_history_update(raw)
        if group == "basic_profile":
            parsed = {**basic_updates, **explicit_updates}
        elif group == "repayment_status":
            parsed = explicit_updates or self._parse_repayment_status(raw)
        elif group == "statement_amounts":
            parsed = explicit_updates or self._parse_amount_group(
                raw,
                start=12,
                allowed_counts=(2, 6),
            )
        else:
            parsed = explicit_updates or self._parse_amount_group(raw, start=18)
        if group != "basic_profile" and basic_updates:
            parsed = {**basic_updates, **parsed}

        errors = self._apply_parsed_updates(parsed)
        if not parsed:
            errors.append(f"{GROUP_LABELS[group]}没有识别到完整输入")
            if group == "repayment_status" and "逾期" in raw:
                errors.append("“逾期还款”无法确定逾期月数，请写成“逾期1个月”或对应编码。")
        self.last_errors = tuple(errors)
        self._mark_completed_groups()
        return self

    def _apply_parsed_updates(self, parsed: Dict[str, Any]) -> list[str]:
        errors = []
        for key, value in parsed.items():
            if key not in FEATURE_SPECS:
                continue
            spec = FEATURE_SPECS[key]
            valid, number, message = spec.validate(value)
            if not valid or number is None:
                errors.append(f"{spec.label}：{message}")
                self.values[key] = FeatureValue(
                    status="invalid",
                    source="user",
                    message=message,
                    decision_allowed=spec.allowed_for_decision,
                )
                continue
            self.values[key] = FeatureValue(
                status="collected",
                value=number,
                source="user",
                decision_allowed=spec.allowed_for_decision,
            )
        return errors

    @staticmethod
    def _parse_explicit_history_update(text: str) -> Dict[str, Any]:
        """Parse a labeled correction for a historical feature group."""
        result: Dict[str, Any] = {}
        if "还款状态" in text:
            match = re.search(
                r"还款状态\s*[:：]?\s*(.*?)"
                r"(?=(?:账单金额|还款金额)\s*[:：]?|$)",
                text,
                re.S,
            )
            if match:
                result.update(
                    CreditRiskFeatureCollection._parse_repayment_status(
                        match.group(1)
                    )
                )
        if "账单金额" in text:
            match = re.search(
                r"账单金额\s*[:：]?\s*(.*?)"
                r"(?=(?:还款状态|还款金额)\s*[:：]?|$)",
                text,
                re.S,
            )
            if match:
                result.update(
                    CreditRiskFeatureCollection._parse_amount_group(
                        match.group(1),
                        start=12,
                        allowed_counts=(2, 6),
                    )
                )
        if "还款金额" in text:
            match = re.search(
                r"还款金额\s*[:：]?\s*(.*?)"
                r"(?=(?:还款状态|账单金额)\s*[:：]?|$)",
                text,
                re.S,
            )
            if match:
                result.update(
                    CreditRiskFeatureCollection._parse_amount_group(
                        match.group(1),
                        start=18,
                    )
                )
        return result

    @staticmethod
    def _parse_declined_fields(text: str) -> Tuple[str, ...]:
        """Recognize a field-level refusal without discarding other answers."""
        refusal = r"(?:不提供|跳过|不清楚|不知道|暂时没有|无法提供)"
        if not re.search(refusal, text):
            return ()
        labels = {
            "额度": ("X1",),
            "性别": ("X2",),
            "教育": ("X3",),
            "婚姻": ("X4",),
            "年龄": ("X5",),
            "还款状态": tuple(f"X{index}" for index in range(6, 12)),
            "账单金额": tuple(f"X{index}" for index in range(12, 18)),
            "还款金额": tuple(f"X{index}" for index in range(18, 24)),
        }
        result = []
        for label, keys in labels.items():
            before = re.search(
                refusal + r"[^，,；;。\n]{0,12}" + re.escape(label),
                text,
            )
            after = re.search(
                re.escape(label) + r"[^，,；;。\n]{0,8}" + refusal,
                text,
            )
            if before or after:
                result.extend(keys)
        return tuple(dict.fromkeys(result))

    def _parse_basic_profile(self, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        amount = re.search(
            r"(?:当前)?(?:信用|授信)?额度[^0-9-]{0,8}(-?[\d,]+(?:\.\d+)?)\s*(万)?",
            text,
        )
        if amount:
            value = float(amount.group(1).replace(",", ""))
            result["X1"] = value * 10_000 if amount.group(2) else value
        if re.search(r"性别?\s*[:：]?\s*(男|男性)", text):
            result["X2"] = 1
        elif re.search(r"性别?\s*[:：]?\s*(女|女性)", text):
            result["X2"] = 2
        education = {
            "研究生": 1, "硕士": 1, "博士": 1,
            "本科": 2, "大学": 2, "大专": 2,
            "高中": 3, "中专": 3, "初中": 3, "其他教育": 4,
        }
        for label, value in education.items():
            if label in text and "教育" in text:
                result["X3"] = value
                break
        if re.search(r"(?:已婚|结婚)", text) and "未婚" not in text and "离婚" not in text:
            result["X4"] = 1
        elif "未婚" in text or "单身" in text:
            result["X4"] = 2
        elif "离异" in text or "离婚" in text or "其他婚姻" in text:
            result["X4"] = 3
        age_labeled = re.search(r"年龄\s*[:：]?\s*(\d{2})", text)
        age_suffix = re.search(r"(\d{2})\s*岁", text)
        if age_labeled:
            result["X5"] = int(age_labeled.group(1))
        elif age_suffix:
            result["X5"] = int(age_suffix.group(1))
        return result

    @staticmethod
    def _parse_repayment_status(text: str) -> Dict[str, Any]:
        tokens = [
            token.strip()
            for token in re.split(r"[,，、;；/\s]+", text)
            if token.strip()
        ]
        if len(tokens) != 6:
            return {}
        values = {}
        invalid = False
        for index, token in enumerate(tokens, start=6):
            if token in {"无消费", "无账单", "没有账单"}:
                values[f"X{index}"] = -2
            elif token in {"按时还清", "已还清", "正常还清", "按时还款"}:
                values[f"X{index}"] = -1
            elif token in {"循环使用", "最低还款", "未还清", "正常"}:
                values[f"X{index}"] = 0
            else:
                overdue = re.search(r"逾期\s*(\d)\s*个?月", token)
                if overdue:
                    values[f"X{index}"] = int(overdue.group(1))
                else:
                    try:
                        values[f"X{index}"] = int(token)
                    except ValueError:
                        invalid = True
        if invalid:
            return {}
        return values

    @staticmethod
    def _parse_amount_group(
        text: str,
        *,
        start: int,
        allowed_counts: Tuple[int, ...] = (6,),
    ) -> Dict[str, Any]:
        # Commas are the public list separator here.  Treating them as
        # in-number thousands separators greedily consumes all six values
        # into one token, so split the list first and reject ambiguous input.
        tokens = [
            token.strip()
            for token in re.split(r"[,，、;；/\s]+", text.strip())
            if token.strip()
        ]
        if len(tokens) not in allowed_counts:
            return {}
        result = {}
        for offset, token in enumerate(tokens):
            is_wan = "万" in token
            normalized = token.replace("万", "").replace("元", "").strip()
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
                return {}
            try:
                number = float(normalized)
            except ValueError:
                return {}
            result[f"X{start + offset}"] = number * 10_000 if is_wan else number
        return result

    def _group_missing_keys(self, group: str) -> Tuple[str, ...]:
        required = self._required_keys_for_group(group)
        return tuple(
            key for key in required
            if self.values[key].status not in {"collected", "verified"}
        )

    def _model_slot_complete(self, slot: str) -> bool:
        if slot == "REPAYMENT_STATUS_6":
            keys = tuple(f"X{index}" for index in range(6, 12))
        elif slot == "REPAYMENT_AMOUNT_6":
            keys = tuple(f"X{index}" for index in range(18, 24))
        elif slot == "STATEMENT_AMOUNT_2":
            keys = ("X12", "X13")
        else:
            keys = (slot,)
        return all(
            self.values[key].status in {"collected", "verified"}
            for key in keys
        )

    def next_question(self) -> str:
        group = self.next_group()
        if group is None:
            return ""
        progress = self.progress_payload()
        missing_keys = self._group_missing_keys(group)
        missing_labels = "、".join(FEATURE_SPECS[key].label for key in missing_keys)
        prefix = ""
        if self.last_errors:
            prefix = "刚才的信息需要修正：\n" + "\n".join(
                f"  · {item}" for item in self.last_errors
            ) + "\n"
        if group == "basic_profile":
            detail = (
                f"请提供当前信用额度和教育程度（{missing_labels}）。"
                "例如：「当前额度5万；教育本科」。"
                "教育程度仅用于研究模型，不进入正式授信决策；"
                "如果目前没有信用卡，请直接说明，系统不会虚构额度。"
            )
        elif group == "repayment_status":
            detail = (
                "请按“最近一个月到最早一个月”的顺序提供最近 6 个月还款状态，"
                "用逗号分隔。可用：按时还清、无消费、循环使用、逾期N个月，"
                "也可以填写模型编码 -2、-1、0 到 9。"
                "这 6 个月会作为一个还款状态组参与模型。"
                "如方便，还可以同时补充教育、婚姻、六个月账单金额和还款金额；"
                "这些是可选资料，不会阻塞本次模型评估。"
            )
        elif group == "payment_amounts":
            detail = (
                "请按“最近一个月到最早一个月”的顺序提供 6 个实际还款金额，"
                "用逗号分隔，金额不能为负，支持“万”单位。"
                "这 6 个月会作为一个还款金额组参与模型。"
            )
        else:
            detail = (
                "请提供最近一个月和最近第二个月的正式账单金额，"
                "用逗号分隔；金额可为负，支持“万”单位。"
                "这两个月会合并为一个账单金额组参与模型。"
            )
        detail += (
            f"\n模型必需资料 {progress['required_collected_features']}/"
            f"{progress['required_total_features']} 项，"
            f"剩余必需 {progress['required_missing_features']} 项。"
            "\n其余历史资料可稍后补充，但不影响完成本次模型评估。"
        )
        return prefix + detail

    def progress_payload(self) -> Dict[str, Any]:
        collected = [
            key for key in FEATURE_ORDER
            if self.values[key].status in {"collected", "verified"}
        ]
        collected_values = [
            {
                "key": key,
                "label": FEATURE_SPECS[key].label,
                "display_value": self._display_value(key),
                "source": self.values[key].source,
            }
            for key in collected
        ]
        missing = [
            key for key in FEATURE_ORDER
            if self.values[key].status == "missing"
        ]
        invalid = [
            key for key in FEATURE_ORDER
            if self.values[key].status == "invalid"
        ]
        declined = [
            key for key in FEATURE_ORDER
            if self.values[key].status == "declined"
        ]
        required_collected = [
            slot for slot in REQUIRED_MODEL_FEATURES
            if self._model_slot_complete(slot)
        ]
        required_missing = [
            slot for slot in REQUIRED_MODEL_FEATURES
            if slot not in required_collected
        ]
        required_complete = not required_missing
        policy_allowed = all(
            self.values[key].decision_allowed
            for key in REQUIRED_FEATURE_ORDER
        )
        total = len(FEATURE_ORDER)
        return {
            "total_features": total,
            "collected_features": len(collected),
            "missing_features": len(missing) + len(invalid),
            "invalid_features": len(invalid),
            "declined_features": len(declined),
            "required_total_features": len(REQUIRED_MODEL_FEATURES),
            "required_collected_features": len(required_collected),
            "required_missing_features": len(required_missing),
            "required_feature_keys": list(REQUIRED_MODEL_FEATURES),
            "required_collected_keys": required_collected,
            "required_missing_feature_keys": required_missing,
            "optional_collected_keys": [
                key for key in collected if key not in REQUIRED_FEATURE_KEYS
            ],
            "required_complete": required_complete,
            "core_complete": required_complete,
            "ready_for_prediction": required_complete,
            "coverage_ratio": round(len(collected) / total, 4),
            "core_coverage_ratio": round(
                len(required_collected) / len(REQUIRED_MODEL_FEATURES),
                4,
            ),
            "collected_feature_keys": collected,
            "collected_values": collected_values,
            "missing_feature_keys": missing,
            "invalid_feature_keys": invalid,
            "declined_feature_keys": declined,
            "processed_groups": list(self.processed_groups),
            "next_group": self.next_group(),
            "policy_allowed_for_decision": policy_allowed,
            "model_name": TOP5_MODEL_NAME,
            "model_version": TOP5_MODEL_VERSION,
            "feature_version": TOP5_FEATURE_VERSION,
            "model_input_order": [
                *[f"X{index}" for index in range(6, 12)],
                *[f"X{index}" for index in range(18, 24)],
                "X12", "X13", "X1", "X3",
            ],
            "model_importance_order": list(REQUIRED_MODEL_FEATURES),
            "model_status": "ready" if required_complete else "collecting",
            "model_status_text": (
                "Top-5 核心资料已满足，确认后将调用风险评估模型。"
                if required_complete
                else "仍在收集还款状态、还款金额、账单金额、额度和教育程度。"
            ),
            "blocking_reasons": required_missing,
        }

    def _display_value(self, key: str) -> str:
        item = self.values[key]
        if item.value is None:
            return ""
        number = item.value
        if key == "X2":
            return {1: "男", 2: "女"}.get(int(number), str(number))
        if key == "X3":
            return {
                1: "研究生及以上",
                2: "本科/大专",
                3: "高中/中专及以下",
                4: "其他",
            }.get(int(number), str(number))
        if key == "X4":
            return {
                1: "已婚",
                2: "未婚",
                3: "其他",
            }.get(int(number), str(number))
        if key == "X5":
            return f"{int(number)} 岁"
        if 6 <= int(key[1:]) <= 11:
            if number == -2:
                return "无账单"
            if number == -1:
                return "按时还清"
            if number == 0:
                return "循环使用/最低还款"
            if number > 0:
                return f"逾期 {int(number)} 个月"
        return f"{number:,.2f} 元"

    def model_input_payload(self) -> Dict[str, Any]:
        missing = [
            key for key in REQUIRED_FEATURE_ORDER
            if self.values[key].status not in {"collected", "verified"}
            or self.values[key].value is None
        ]
        if missing:
            return {
                "ready": False,
                "missing_features": missing,
                "features": {},
                "feature_statuses": {},
                "feature_sources": {},
            }
        return {
            "ready": True,
            "missing_features": [],
            "features": {
                key: float(self.values[key].value)
                for key in REQUIRED_FEATURE_ORDER
            },
            "feature_statuses": {
                key: self.values[key].status
                for key in REQUIRED_FEATURE_ORDER
            },
            "feature_sources": {
                key: self.values[key].source
                for key in REQUIRED_FEATURE_ORDER
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "values": {
                key: value.to_dict()
                for key, value in self.values.items()
            },
            "processed_groups": list(self.processed_groups),
            "last_errors": list(self.last_errors),
        }


def feature_contract_payload() -> Dict[str, Any]:
    return {
        "model_name": TOP5_MODEL_NAME,
        "model_version": TOP5_MODEL_VERSION,
        "feature_version": TOP5_FEATURE_VERSION,
        "feature_order": [
            *[f"X{index}" for index in range(6, 12)],
            *[f"X{index}" for index in range(18, 24)],
            "X12", "X13", "X1", "X3",
        ],
        "required_feature_order": list(REQUIRED_FEATURE_ORDER),
        "feature_importance_order": list(REQUIRED_MODEL_FEATURES),
        "semantic_group_order": list(REQUIRED_MODEL_FEATURES),
        "all_supported_feature_order": list(FEATURE_ORDER),
        "optional_feature_order": [
            key for key in FEATURE_ORDER if key not in REQUIRED_FEATURE_KEYS
        ],
        "groups": [
            {"id": group, "label": GROUP_LABELS[group]}
            for group in FEATURE_GROUPS
        ],
        "preprocessing": {
            "numeric": [
                *[f"X{index}" for index in range(18, 24)],
                "X1", "X12", "X13",
            ],
            "categorical": [
                *[f"X{index}" for index in range(6, 12)],
                "X3",
            ],
            "numeric_scaler": "StandardScaler",
            "categorical_encoder": "OneHotEncoder(handle_unknown='ignore')",
        },
        "artifact_status": "available",
        "prediction_available": True,
        "artifact_origin": "reproduced_top5_variant",
        "top5_evidence": "grouped MDI from the full 23-feature Random Forest",
    }


__all__ = [
    "CreditRiskFeatureCollection",
    "FEATURE_GROUPS",
    "FEATURE_ORDER",
    "FEATURE_SPECS",
    "FeatureValue",
    "ModelFeatureSpec",
    "REQUIRED_FEATURE_KEYS",
    "REQUIRED_FEATURE_ORDER",
    "REQUIRED_MODEL_FEATURES",
    "TOP5_FEATURE_VERSION",
    "TOP5_MODEL_NAME",
    "TOP5_MODEL_VERSION",
    "feature_contract_payload",
]
