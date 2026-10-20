"""简易命令行交互界面（初版 Demo）。

用法：
    python run.py                 # 进入对话
    python run.py --today 2026-11-08   # 指定"今天"，用于演示定时任务
    python run.py --script demo/xxx.txt # 批量回放一段对话

内置命令（以 / 开头）：
    /help     查看命令与能力
    /balance  账户总览
    /state    查看当前对话状态
    /trace    查看最近一次工具调用明细
    /task     查看待执行任务
    /advance N  时间快进 N 天，并执行到期的定时任务
    /audit    查看审计日志
    /reset    清空对话状态
    /quit     退出
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from config import APP_NAME, APP_SUBTITLE, DISCLAIMER
from finagent.agent.core import AgentReply, BankingAgent
from finagent.agent.dialogue import classify_control
from finagent.cli.input_reader import read_message, read_paste_block
from finagent.vision.receipt_parser import looks_like_bill_fragment


class Palette:
    """终端配色。--no-color 或非 TTY 时自动降级为纯文本。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def user(self, text: str) -> str:
        return self._wrap("36", text)

    def agent(self, text: str) -> str:
        return self._wrap("32", text)

    def warn(self, text: str) -> str:
        return self._wrap("33", text)

    def error(self, text: str) -> str:
        return self._wrap("31", text)

    def dim(self, text: str) -> str:
        return self._wrap("90", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


HELP_TEXT = """可用命令：
  /help        查看帮助
  /balance     账户总览
  /scan <路径>  识别账单截图/小票（图片或 txt/csv），识别后可确认导入
  /paste       进入粘贴模式：把微信/支付宝账单整段粘进来，单独输入 . 结束
  /import <文件> 从账单文件（txt/csv）导入，推荐配合 samples/bill_template.txt
  /state       查看当前对话状态（待补槽位 / 待确认）
  /trace       查看最近一次工具调用明细
  /task        查看待执行的定时任务
  /advance N   时间快进 N 天，并自动执行到期任务
  /audit       查看最近的审计日志
  /reset       清空对话状态
  /quit        退出

自然语言示例（六大核心场景）：
  转账   给老婆转2000块 / 下个月5号自动给房东转3800 / 昨晚聚餐960，我、王强、李思琪AA
  账单   分析我这个月的账单 / 帮我看看有没有异常交易 / 生成年度账单报告
        识别这张账单截图 samples/bill_sample.png （截图识别，离线 OCR 可用）
        也可以直接把微信/支付宝账单整段粘贴进来（粘完按一次回车即可）
        最稳的方式：把账单存成 txt，用 /import 你的文件路径
  理财   帮我做个风险测评 / 推荐几个稳健的理财产品 / 买1万块稳利债券A
  卡片   我有哪些卡 / 信用卡额度提到3万 / 关闭信用卡的境外交易 / 我的卡丢了帮我挂失
  订阅   有哪些自动续费 / 最近有哪些订阅要扣费 / 取消腾讯视频会员
  联动   我老婆快生日了帮我准备一下 / 今天有什么要注意的
"""


def ensure_utf8() -> None:
    """尽量让终端按 UTF-8 输出，避免中文在部分 Windows 控制台里变成乱码。

    如果终端本身不支持 UTF-8，可以在运行前执行 chcp 65001 手动切换。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if getattr(stream, "encoding", "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            continue


def banner(palette: Palette) -> str:
    lines = [
        "=" * 62,
        f"  {APP_NAME} · 智能银行代理系统",
        f"  {APP_SUBTITLE}",
        "=" * 62,
        palette.warn("  提示：" + DISCLAIMER),
        "  输入自然语言即可办理业务，输入 /help 查看命令，/quit 退出。",
    ]
    return "\n".join(lines)


def print_reply(reply: AgentReply, palette: Palette, show_trace: bool = True) -> None:
    print(palette.agent("Agent> ") + reply.text.replace("\n", "\n        "))
    if show_trace and reply.trace:
        print(palette.dim("        工具调用："))
        for item in reply.trace:
            args = ", ".join(f"{k}={v!r}" for k, v in item["args"].items())
            status = "OK " if item["ok"] else "FAIL"
            print(palette.dim(f"          [{status}] {item['tool']}({args}) -> {item['summary']}"))


def handle_command(
    command: str,
    agent: BankingAgent,
    palette: Palette,
    last_reply: Optional[AgentReply],
) -> "tuple[bool, Optional[AgentReply]]":
    """处理 / 开头的命令。返回 (是否继续, 新的 last_reply)。"""
    parts = command.strip().split()
    name = parts[0].lower()
    if name in {"/quit", "/exit", "/q"}:
        print(palette.dim("再见，期待下次为您服务。"))
        return False, last_reply
    if name == "/help":
        print(HELP_TEXT)
        return True, last_reply
    if name == "/balance":
        from finagent.agent.planner import Plan, PlanStep
        ctx = agent.context()
        reply = agent._execute(Plan(intent="account.balance", steps=[PlanStep("account.balance")]), ctx)
        print_reply(reply, palette, show_trace=False)
        return True, reply
    if name == "/state":
        pending = agent.session.pending
        if pending is None:
            print(palette.dim("当前没有等待补充或确认的操作。"))
        else:
            print(palette.dim(f"待处理类型：{pending.kind}；意图：{pending.intent}；"
                              f"已收集槽位：{pending.slots}；缺失：{pending.missing}"))
        print(palette.dim(f"对话轮次：{agent.session.turn}；最近意图：{agent.last_intent_name or '-'}；"
                          f"解析通道：{agent.parser.last_source}；"
                          f"当前时间：{agent.clock.iso()}"))
        return True, last_reply
    if name == "/trace":
        if last_reply is None or not last_reply.trace:
            print(palette.dim("最近一次回复没有工具调用。"))
        else:
            for item in last_reply.trace:
                print(palette.dim(f"  {item}"))
        return True, last_reply
    if name == "/task":
        tasks = agent.store.tasks_of(agent.user_id, status="pending")
        if not tasks:
            print(palette.dim("没有待执行任务。"))
        for task in tasks:
            print(palette.dim(f"  {task.run_date} [{task.kind}] {task.title}"))
        return True, last_reply
    if name == "/scan":
        path = command.strip()[len("/scan"):].strip().strip('"')
        if not path:
            print(palette.warn("用法：/scan <账单图片或账单文件路径>，例如 /scan samples/bill_sample.png"))
            return True, last_reply
        from finagent.agent.planner import Plan, PlanStep
        reply = agent._execute(
            Plan(intent="bill.scan_receipt", steps=[PlanStep("bill.scan_receipt", {"image": path})]),
            agent.context(),
        )
        print_reply(reply, palette)
        return True, reply
    if name == "/import":
        path = command.strip()[len("/import"):].strip().strip('"')
        if not path:
            print(palette.warn("用法：/import <账单文件路径>，例如 /import samples/bill_template.txt"))
            return True, last_reply
        from finagent.agent.planner import Plan, PlanStep
        reply = agent._execute(
            Plan(intent="bill.scan_receipt", steps=[PlanStep("bill.scan_receipt", {"image": path})]),
            agent.context(),
        )
        print_reply(reply, palette)
        return True, reply
    if name == "/paste":
        block = read_paste_block()
        if not block or not block.strip():
            print(palette.dim("没有收到任何内容。"))
            return True, last_reply
        print(palette.dim(f"已接收 {len([l for l in block.splitlines() if l.strip()])} 行文本，正在解析…"))
        reply = agent.handle(block)
        print_reply(reply, palette)
        return True, reply
    if name == "/advance":
        days = 1
        if len(parts) > 1:
            try:
                days = int(parts[1])
            except ValueError:
                days = 1
        agent.clock.advance(days=days)
        print(palette.dim(f"时间已快进到 {agent.clock.date_str()}。"))
        messages = agent.run_due_tasks()
        if messages:
            for message in messages:
                print(palette.agent("Agent> ") + message.replace("\n", "\n        "))
        else:
            print(palette.dim("没有到期任务需要执行。"))
        return True, last_reply
    if name == "/audit":
        for record in agent.store.audit[-12:]:
            print(palette.dim(f"  {record}"))
        return True, last_reply
    if name == "/reset":
        agent.reset()
        print(palette.dim("对话状态已重置。"))
        return True, last_reply
    print(palette.error(f"未知命令：{name}，输入 /help 查看可用命令。"))
    return True, last_reply


def run_repl(agent: BankingAgent, palette: Palette, show_trace: bool = True) -> None:
    print(banner(palette))
    last_reply: Optional[AgentReply] = None
    is_complete = make_completeness_check(agent)
    while True:
        try:
            raw = read_message(palette.user("用户> "), is_complete=is_complete)
        except KeyboardInterrupt:
            print()
            break
        if raw is None:
            print()
            break
        text = raw.strip()
        if not text:
            continue
        line_count = len([line for line in text.splitlines() if line.strip()])
        if line_count > 1:
            print(palette.dim(f"（收到 {line_count} 行输入，作为一整段处理）"))
        if text.startswith("/"):
            keep_going, last_reply = handle_command(text, agent, palette, last_reply)
            if not keep_going:
                break
            continue
        reply = agent.handle(text)
        print_reply(reply, palette, show_trace)
        last_reply = reply


def make_completeness_check(agent: BankingAgent):
    """判断"这一行是不是一条完整指令"。

    看起来像账单里的行（行尾是金额、或整行只是时间戳）时，认为后面还有内容，
    于是把后续内容一起收成一段，而不是逐行激活。
    """
    def is_complete(line: str) -> bool:
        text = (line or "").strip()
        if not text:
            return True
        if text.startswith("/"):
            return True
        if looks_like_bill_fragment(text):
            return False
        if classify_control(text):
            return True
        try:
            return not agent.parser.rule.parse(text).is_unknown()
        except Exception:
            return True
    return is_complete


def run_script(agent: BankingAgent, script_path: Path, palette: Palette) -> int:
    """按脚本回放一段对话，便于自动化演示与测试。"""
    if not script_path.exists():
        print(palette.error(f"找不到脚本文件：{script_path}"))
        return 1
    for line in script_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        print(palette.user("用户> ") + text)
        if text.startswith("/"):
            keep_going, _ = handle_command(text, agent, palette, None)
            if not keep_going:
                break
            continue
        print_reply(agent.handle(text), palette)
    return 0

