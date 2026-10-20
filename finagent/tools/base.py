"""工具基类与通用输出格式化。

设计约定：
- 每个工具只做"一件事"，输入参数扁平，方便大模型生成；
- `run()` 返回 ToolResult，既是给用户看的话术，也是给上层编排的机器可读数据；
- 工具不做风控判断，风控统一由 RiskEngine 在调度前完成（职责单一）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from finagent.data.models import User
from finagent.data.store import Clock, DataStore
from finagent.risk.engine import RiskEngine


def format_money(value: float) -> str:
    """金额格式化：正数带 +，负数带 -，千分位。"""
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):,.2f}"


def render_table(headers: List[str], rows: List[List[Any]], indent: str = "  ") -> str:
    """把二维数据渲染成等宽对齐的文本表格（中文字符按 2 列宽计算）。"""
    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))

    def pad(text: str, target: int) -> str:
        return str(text) + " " * max(target - width(str(text)), 0)

    cols = [headers] + [[str(c) for c in row] for row in rows]
    widths = [max(width(r[i]) for r in cols) for i in range(len(headers))]
    lines = [indent + "  ".join(pad(h, widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append(indent + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(indent + "  ".join(pad(c, widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


@dataclass
class ToolParam:
    """工具参数声明。既是给人看的文档，也是给大模型的 schema。"""

    name: str
    type: str = "string"
    required: bool = False
    description: str = ""
    default: Any = None
    enum: Optional[List[str]] = None

    def to_schema(self) -> Dict[str, Any]:
        schema: Dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = self.enum
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""          # 一句话结论，供计划执行完毕后汇总

    def __post_init__(self) -> None:
        if not self.summary:
            self.summary = self.message.strip().splitlines()[0] if self.message else ""


@dataclass
class ToolContext:
    """工具运行上下文。"""

    store: DataStore
    clock: Clock
    risk: RiskEngine
    user_id: str
    session: Dict[str, Any] = field(default_factory=dict)

    @property
    def user(self) -> User:
        user = self.store.get_user(self.user_id)
        if user is None:  # pragma: no cover - 防御性分支
            raise ValueError(f"用户不存在: {self.user_id}")
        return user


class Tool(ABC):
    """所有银行业务工具的基类。"""

    name: str = ""
    category: str = ""
    description: str = ""
    params: List[ToolParam] = []
    # "二选一"型必填：这些参数里至少出现一个即可（例如 图片 或 粘贴文本）
    required_any: List[str] = []
    risk_level: str = "low"                 # low / medium / high
    requires_confirmation: bool = False
    examples: List[str] = []

    @abstractmethod
    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        """执行工具。子类实现。"""

    # ------------------------------------------------------------------
    def required_params(self) -> List[str]:
        return [p.name for p in self.params if p.required]

    def missing_params(self, args: Dict[str, Any]) -> List[str]:
        missing = []
        for param in self.params:
            if not param.required:
                continue
            value = args.get(param.name)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(param.name)
        return missing

    def param_description(self, name: str) -> str:
        for param in self.params:
            if param.name == name:
                return f"{self.describe_param(param.name)}（{param.description}）"
        return name

    def describe_param(self, name: str) -> str:
        labels = {
            "payee": "收款人",
            "amount": "金额",
            "note": "备注",
            "when": "时间",
            "repeat": "重复方式",
            "total": "总金额",
            "participants": "参与人",
            "period": "时间范围",
            "keyword": "关键词",
            "category": "类别",
            "risk_level": "风险等级",
            "horizon": "投资期限",
            "product": "产品",
            "card": "卡片",
            "merchant": "商户",
            "person": "对象",
            "budget": "预算",
            "date": "日期",
            "item": "物品",
            "new_limit": "新额度",
            "scope": "范围",
            "enable": "开关",
            "kind": "类型",
            "account": "账户",
        }
        return labels.get(name, name)

    def to_schema(self) -> Dict[str, Any]:
        """输出给大模型看的工具描述。"""
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "risk_level": self.risk_level,
            "requires_confirmation": self.requires_confirmation,
            "parameters": {p.name: p.to_schema() for p in self.params},
            "required": self.required_params(),
            "examples": self.examples,
        }

