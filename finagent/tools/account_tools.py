"""账户与助手类工具：余额查询、客户画像、能力清单。"""
from __future__ import annotations

from typing import Dict, List

from config import LLMConfig
from finagent.tools.base import Tool, ToolContext, ToolResult, render_table

KIND_LABEL = {
    "current": "活期账户",
    "savings": "定期存款",
    "credit": "信用卡",
    "wallet": "电子钱包",
}


class BalanceTool(Tool):
    """账户总览。"""

    name = "account.balance"
    category = "账户服务"
    description = "查看名下账户余额、可用额度与总资产"
    risk_level = "low"
    params = []
    examples = ["我还有多少钱", "查一下余额"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        accounts = ctx.store.accounts_of(ctx.user_id)
        rows = []
        total = 0.0
        for acc in accounts:
            if acc.kind == "credit":
                rows.append([KIND_LABEL.get(acc.kind, acc.kind), f"尾号{acc.tail}",
                             f"已用 {acc.balance:,.2f}", f"可用 {acc.available:,.2f}"])
            else:
                total += acc.balance
                locked = f"（锁定 {acc.locked:,.2f}）" if acc.locked else ""
                rows.append([KIND_LABEL.get(acc.kind, acc.kind), f"尾号{acc.tail}",
                             f"{acc.balance:,.2f}{locked}", f"可用 {acc.available:,.2f}"])
        lines = [
            f"总资产 {total:,.2f} 元：",
            render_table(["账户", "卡号", "余额/已用", "可用"], rows),
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"total": round(total, 2)},
            summary=f"总资产 {total:,.2f} 元",
        )


class ProfileTool(Tool):
    """客户画像。"""

    name = "profile.show"
    category = "账户服务"
    description = "查看客户画像、风险偏好、重要日期与消费偏好"
    risk_level = "low"
    params = []
    examples = ["我的资料", "我的风险等级是什么"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        user = ctx.user
        prefs = user.preferences or {}
        lines = [
            f"客户档案：{user.name}（{user.user_id}）",
            f"  手机号：{user.phone[:3]}****{user.phone[-4:] if len(user.phone) >= 4 else ''}",
            f"  城市：{user.city}   职业：{user.occupation}   年龄：{user.age}",
            f"  月收入：{user.monthly_income:,.0f} 元   风险偏好：{user.risk_profile}（{user.risk_score}/5）",
            f"  实名认证：{'已完成' if user.verified else '未完成'}",
        ]
        if prefs.get("spouse"):
            lines.append(f"  家人：{prefs['spouse']}" + (f"（生日 {prefs.get('spouse_birthday')}）"
                                                        if prefs.get("spouse_birthday") else ""))
        if prefs.get("budget_categories"):
            lines.append(f"  重点管控类别：{'、'.join(prefs['budget_categories'])}")
        if prefs.get("notify_channel"):
            lines.append(f"  通知渠道：{prefs['notify_channel']}")
        return ToolResult(True, "\n".join(lines), data={"name": user.name}, summary=f"{user.name} 的客户画像")


class HelpTool(Tool):
    """能力清单。"""

    name = "help"
    category = "账户服务"
    description = "列出 Agent 当前支持的全部能力"
    risk_level = "low"
    params = []
    examples = ["你能做什么", "帮助"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        registry = ctx.session.get("registry")
        lines = ["我可以帮您处理以下银行业务（直接说自然语言即可）："]
        if registry is not None:
            grouped: Dict[str, List[Any]] = {}
            for tool in registry.all():
                if tool.name == "help":
                    continue
                grouped.setdefault(tool.category, []).append(tool)
            for category, items in grouped.items():
                lines.append(f"\n【{category}】")
                for tool in items:
                    example = tool.examples[0] if getattr(tool, "examples", None) else ""
                    suffix = f"   例：{example}" if example else ""
                    lines.append(f"  · {tool.description}{suffix}")
        lines.append("\n提示：涉及资金变动、卡片冻结、取消订阅等操作时，我会先跟您确认再执行。")
        return ToolResult(True, "\n".join(lines), summary="已列出全部能力",
                          data={"categories": len(ctx.session.get("registry").all()) if registry else 0})


class AboutTool(Tool):
    """自我说明：回答"你用的什么大模型""你是怎么实现的"这类问题。"""

    name = "agent.about"
    category = "账户服务"
    description = "说明本 Agent 的技术实现、当前使用的语言理解通道与大模型配置"
    risk_level = "low"
    params = []
    examples = ["你用的什么大模型", "你是谁", "你是怎么实现的"]

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        session = ctx.session
        llm = session.get("llm")
        mode = str(session.get("llm_mode") or LLMConfig.MODE)
        source = str(session.get("parser_source") or "rule")
        registry = session.get("registry")
        tool_count = len(registry.all()) if registry is not None else 0
        key_state = "已配置" if (llm is not None and getattr(llm, "available", False)) else "未配置"
        channel = "大模型 + 规则兜底" if key_state == "已配置" else "内置规则解析器（离线）"
        model = getattr(llm, "model", LLMConfig.MODEL) if llm is not None else LLMConfig.MODEL
        base_url = getattr(llm, "base_url", LLMConfig.BASE_URL) if llm is not None else LLMConfig.BASE_URL

        lines = [
            "我是「AI Banking Agent 智能银行代理系统」，一个面向银行业的任务型智能体原型。",
            "",
            "一、自然语言理解用什么",
            f"  当前通道：{channel}（本次解析来自：{source}）",
            f"  模型配置：{model}",
            f"  接口地址：{base_url}",
            f"  API Key：{key_state}",
            "  策略：配置了 Key 就优先用大模型做语义理解，失败或没配就自动回退到内置规则；",
            "        金额、日期这类硬信息始终由正则兜底，所以断网也能完整跑通全部场景。",
            "",
            "二、一次请求的处理链路",
            "  自然语言 → 意图识别 → 槽位补全 → 任务规划 → 风控校验 → 工具执行 → 自然语言应答",
            "",
            "三、装了哪些能力",
            f"  {tool_count} 个银行业务工具，覆盖智能转账、账单分析、理财操作、卡片管理、订阅代扣、跨场景联动",
            "  账单截图识别走 Windows 内置 OCR，不需要联网，也不需要安装第三方库",
            "",
            "四、想换模型",
            "  任何兼容 OpenAI 协议的接口都能接，设置这两个环境变量即可：",
            "    FINAGENT_LLM_BASE_URL / FINAGENT_LLM_MODEL（以及 FINAGENT_LLM_API_KEY）",
            "  也可以把 FINAGENT_LLM_MODE 设成 off 强制走离线规则解析。",
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"mode": mode, "model": model, "api_key_configured": key_state == "已配置",
                  "parser_source": source, "tool_count": tool_count},
            summary=f"当前语言理解通道：{channel}",
        )

