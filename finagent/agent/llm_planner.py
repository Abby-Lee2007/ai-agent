"""LLM tool-calling planner with deterministic argument validation.

The model may choose a registered tool, but it cannot invent tool names or
override arguments already extracted by deterministic rules. Any unsupported
response falls back to the built-in planner.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from config import LLMConfig
from finagent.agent.registry import ToolRegistry
from finagent.nlu.llm_client import LLMClient


class LLMToolPlanner:
    def __init__(self, registry: ToolRegistry, llm: Optional[LLMClient]) -> None:
        self.registry = registry
        self.llm = llm
        self.last_error = ""
        self.last_tool = ""

    @property
    def available(self) -> bool:
        return bool(
            LLMConfig.TOOL_CALLING
            and self.llm is not None
            and self.llm.available
        )

    @staticmethod
    def _function_name(tool_name: str) -> str:
        return tool_name.replace(".", "__")

    def _tool_schemas(
        self,
        candidates: List[Tuple[str, Dict[str, str]]],
    ) -> List[Dict[str, Any]]:
        result = []
        seen = set()
        for tool_name, _ in candidates:
            if tool_name in seen:
                continue
            seen.add(tool_name)
            tool = self.registry.get(tool_name)
            if tool is None:
                continue
            properties = {}
            required = []
            for param in tool.params:
                schema = {
                    "type": param.type,
                    "description": param.description,
                }
                if param.enum:
                    schema["enum"] = param.enum
                properties[param.name] = schema
                if param.required:
                    required.append(param.name)
            result.append({
                "type": "function",
                "function": {
                    "name": self._function_name(tool_name),
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            })
        return result

    def propose(
        self,
        intent: str,
        candidates: List[Tuple[str, Dict[str, str]]],
        slots: Dict[str, Any],
        raw_text: str,
    ) -> Optional[Dict[str, Any]]:
        if not self.available or not candidates:
            return None
        tools = self._tool_schemas(candidates)
        if not tools:
            return None
        mapping_by_name = {
            self._function_name(tool_name): (tool_name, mapping)
            for tool_name, mapping in candidates
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是银行 Agent 的工具选择器。根据用户原句和已抽取槽位，"
                    "只选择提供的工具之一。不得执行资金动作，不得创造工具。"
                    f"\n意图：{intent}\n已抽取槽位："
                    + json.dumps(slots, ensure_ascii=False)
                ),
            },
            {"role": "user", "content": raw_text},
        ]
        try:
            call = self.llm.chat_with_tools(messages, tools, tool_choice="required")
        except Exception as exc:
            self.last_error = f"工具调用失败：{exc}"
            return None
        if not call:
            self.last_error = self.llm.last_error or "模型未返回工具调用"
            return None
        selected = mapping_by_name.get(str(call.get("name") or ""))
        if selected is None:
            self.last_error = "模型选择了未授权工具"
            return None
        tool_name, mapping = selected
        model_args = call.get("arguments") or {}
        if not isinstance(model_args, dict):
            self.last_error = "模型工具参数不是对象"
            return None
        args: Dict[str, Any] = {}
        for param, slot in mapping.items():
            if slots.get(slot) not in (None, ""):
                args[param] = slots[slot]
                continue
            # Model-proposed free-form values are accepted only for optional
            # parameters and only when the deterministic parser left them blank.
            tool = self.registry.get(tool_name)
            param_spec = next(
                (item for item in (tool.params if tool else []) if item.name == param),
                None,
            )
            if param_spec is not None and not param_spec.required:
                value = model_args.get(param)
                if isinstance(value, (str, int, float, bool)):
                    args[param] = value
        required = self.registry.get(tool_name).required_params() if self.registry.get(tool_name) else []
        if any(name not in args for name in required):
            self.last_error = "模型工具调用缺少必填参数"
            return None
        self.last_tool = tool_name
        return {"tool": tool_name, "args": args}
