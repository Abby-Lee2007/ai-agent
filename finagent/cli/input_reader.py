"""命令行输入读取：把"整段粘贴"可靠地当成一条消息。

之前靠 msvcrt.kbhit() 探测控制台缓冲区，只在真正的 conhost 里有效；
stdin 被包装成管道时（例如某些嵌入式终端面板）会失效，结果又被逐行激活。

现在改成不依赖终端类型的方案，靠两个确定性的信号：

1. **完整性判断**：拿到的这一行如果"看起来不像一条完整指令"
   （例如行尾是金额、或者整行只是个时间戳），就认为后面可能还有内容，继续收；
2. **静默窗口**：后台线程持续读 stdin，主线程按 0.5 秒的静默窗口收行，
   安静下来即认为这一段粘贴结束。

这样无论控制台粘贴、终端面板粘贴，还是把内容用管道灌进来，表现都一致；
而手工逐行输入几乎不可能在 0.5 秒内敲出下一行，手感不变。

另外提供两个 100% 确定性的入口：
    /paste            进入块输入模式，单独输入一个 . 结束
    /import <文件>    直接从文件读（配合 samples/bill_template.txt 使用）
"""
from __future__ import annotations

import queue
import sys
import threading
from typing import Callable, List, Optional

SETTLE_SECONDS = 0.5
MAX_BLOCK_LINES = 800
BLOCK_END_MARKERS = {".", "END", "end", "结束", "'''", '"""'}

_TIMEOUT = object()
_EOF = object()


class _StdinPump:
    """后台线程持续读 stdin，把行放进队列，供主线程按超时取用。

    这样主线程既能"等一行"，也能"等一小段时间看还有没有更多"，
    而且对 TTY 与管道一视同仁。
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._queue: "queue.Queue" = queue.Queue()
        self._pushback: List[str] = []
        self._thread = threading.Thread(target=self._run, name="stdin-pump", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for line in self._stream:
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._queue.put(_EOF)

    def read_line(self, timeout: Optional[float]):
        if self._pushback:
            return self._pushback.pop(0)
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return _TIMEOUT

    def push_back(self, line: str) -> None:
        """把多读进来的一行放回去，供下一次读取使用。"""
        self._pushback.insert(0, line)


_pump: Optional[_StdinPump] = None
_pump_stream = None


def _get_pump() -> _StdinPump:
    global _pump, _pump_stream
    current = sys.stdin
    if _pump is None or _pump_stream is not current:
        _pump = _StdinPump(current)
        _pump_stream = current
    return _pump


def read_message(
    prompt: str = "",
    is_complete: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """读一条用户消息。返回 None 表示输入结束（EOF）。

    is_complete(line) 返回 True 表示"这一行本身就是完整指令"，不再等后续内容。
    """
    pump = _get_pump()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    first = pump.read_line(None)
    if first is _EOF or first is _TIMEOUT:
        return None
    text = str(first).rstrip("\r\n")
    if not text.strip():
        return ""
    if is_complete is None or is_complete(text):
        return text
    lines: List[str] = [text]
    while len(lines) < MAX_BLOCK_LINES:
        nxt = pump.read_line(SETTLE_SECONDS)
        if nxt is _TIMEOUT or nxt is _EOF:
            break
        nxt_text = str(nxt).rstrip("\r\n")
        if not nxt_text.strip():
            break
        if nxt_text.strip().startswith("/"):
            # 遇到 /命令 说明粘贴已经结束，把它放回去留给下一轮
            pump.push_back(nxt_text + "\n")
            break
        if is_complete is not None and is_complete(nxt_text):
            # 这一行本身已经是一条完整指令（例如"确认"），属于下一轮
            pump.push_back(nxt_text + "\n")
            break
        lines.append(nxt_text)
    return "\n".join(lines)


def read_paste_block(prompt: str = "", terminator: str = ".") -> Optional[str]:
    """显式块输入模式：一直读到只有终止符的一行（或空行）为止。"""
    print(prompt or f"请粘贴账单内容，粘贴完成后单独输入一个 {terminator} 并回车结束：")
    pump = _get_pump()
    lines: List[str] = []
    while len(lines) < MAX_BLOCK_LINES:
        sys.stdout.write("... ")
        sys.stdout.flush()
        raw = pump.read_line(None)
        if raw is _EOF or raw is _TIMEOUT:
            break
        line = str(raw).rstrip("\r\n")
        if line.strip() in {"", terminator} or line.strip() in BLOCK_END_MARKERS:
            break
        lines.append(line)
    return "\n".join(lines)
