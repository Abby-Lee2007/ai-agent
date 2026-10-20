"""对话状态管理。

负责在"多轮对话"中记住三件事：
1. 上一轮缺了哪个槽位，等用户补充；
2. 已经规划好但还在等用户二次确认的操作；
3. 等待身份验证（模拟短信验证码）的操作。

这一层刻意与业务工具解耦：工具只管执行，状态机只管"什么时候该执行"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from finagent.agent.planner import Plan


@dataclass
class Pending:
    """一次"待完成"的交互。"""

    kind: str                       # slot / confirm / verify
    intent: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    plan: Optional[Plan] = None
    question: str = ""
    # 问卷流程：已答选项、待问题目、触发问卷的原始意图
    answers: Dict[str, str] = field(default_factory=dict)
    queue: List[str] = field(default_factory=list)
    origin: Any = None
    inferences: Dict[str, Any] = field(default_factory=dict)
    inference_confirmed_at: str = ""


CMD_WORDS = {
    "确认": {"确认", "确定", "好的", "好", "可以", "行", "是的", "是", "对", "没问题", "嗯",
             "同意", "执行", "ok", "OK", "okay", "yes", "y", "继续", "就这样"},
    "否认": {"取消", "不用", "不用了", "算了", "不要", "否", "不是", "不对", "no", "No", "别",
             "停止", "先不", "不", "否了"},
}


def classify_control(text: str) -> Optional[str]:
    """识别"确认/否认"这类控制词。只对短句生效，避免吞掉正常业务表达。"""
    cleaned = (text or "").strip().strip("。！!，,、 ")
    if not cleaned or len(cleaned) > 8:
        return None
    if cleaned in CMD_WORDS["确认"]:
        return "confirm"
    if cleaned in CMD_WORDS["否认"]:
        return "deny"
    return None


class DialogueSession:
    """一次会话的全部状态。"""

    def __init__(self, user_id: str, max_history: int = 40) -> None:
        self.user_id = user_id
        self.pending: Optional[Pending] = None
        self.history: List[Dict[str, str]] = []
        self.transcript: List[Dict[str, Any]] = []
        self.max_history = max_history
        self.turn: int = 0

    def remember_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})
        self._trim()

    def remember_assistant(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})
        self._trim()

    def _trim(self) -> None:
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def clear_pending(self) -> None:
        self.pending = None

    def reset(self) -> None:
        self.pending = None
        self.history.clear()
        self.transcript.clear()
        self.turn = 0

