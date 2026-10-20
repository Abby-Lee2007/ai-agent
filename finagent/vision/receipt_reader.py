"""账单识别调度器：按可用性自动选择识别后端。

后端优先级：
1. `vision_llm`  —— 多模态大模型，识别最准，还能直接给出消费分类；
2. `windows_ocr` —— Windows 内置 OCR（Windows.Media.Ocr），完全离线、零依赖；
3. `text_file`   —— .txt/.csv/.md 等文本账单直接解析。

任何一个后端不可用都会自动降级，并把原因写进 message，方便排查。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional

from finagent.nlu.llm_client import LLMClient
from finagent.vision.receipt_parser import (
    ParsedTransaction,
    guess_category,
    looks_like_pasted_bill,
    parse_receipt_text,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
TEXT_SUFFIXES = {".txt", ".csv", ".md", ".log", ".tsv"}
CATEGORY_CHOICES = ["餐饮", "交通", "购物", "订阅服务", "旅行", "生活缴费", "医疗", "娱乐", "其他"]

VISION_PROMPT = (
    "你是一个银行账单识别助手。请识别这张账单/小票/支付截图里的每一笔交易，"
    "只输出 JSON，不要任何解释。\n"
    '格式：{"records":[{"date":"YYYY-MM-DD","time":"HH:MM","merchant":"商户名",'
    '"amount":-123.45,"category":"餐饮"}],"total":-123.45}\n'
    "规则：\n"
    "1. 金额为负数表示支出，正数表示收入；\n"
    "2. 日期统一成 YYYY-MM-DD，缺少年份时按账单年份推断；\n"
    "3. merchant 使用原始商户名，不要翻译、不要补全；\n"
    "4. category 只能取以下之一：" + "/".join(CATEGORY_CHOICES) + "；\n"
    "5. 忽略页眉、页脚、卡号、合计、应还金额等非交易行。"
)


@dataclass
class ReceiptResult:
    """一次账单识别的结果。"""

    ok: bool
    backend: str = "none"
    backend_label: str = ""
    records: List[ParsedTransaction] = field(default_factory=list)
    raw_text: str = ""
    message: str = ""
    image_path: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def total_expense(self) -> float:
        return round(sum(abs(r.amount) for r in self.records if r.amount < 0), 2)


class ReceiptReader:
    """账单图片 / 文本识别器。"""

    def __init__(self, llm: Optional[LLMClient] = None, ocr_timeout: float = 60.0) -> None:
        self.llm = llm if llm is not None else LLMClient()
        self.ocr_timeout = ocr_timeout
        self.last_error: str = ""
        self._powershell: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def vision_available(self) -> bool:
        return self.llm is not None and self.llm.available

    def powershell_path(self) -> Optional[str]:
        """找到 Windows PowerShell 5.1（WinRT 只在 5.1 里可用）。"""
        if self._powershell is not None:
            return self._powershell
        found = shutil.which("powershell.exe") or shutil.which("powershell")
        if not found:
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            found = str(candidate) if candidate.exists() else None
        self._powershell = found
        return found

    def backends(self) -> dict:
        """当前环境可用的识别后端（给 /help 和诊断用）。"""
        return {
            "vision_llm": self.vision_available,
            "windows_ocr": bool(self.powershell_path() and self._ocr_script().exists()),
            "text_file": True,
        }

    def _ocr_script(self) -> Path:
        return Path(__file__).with_name("win_ocr.ps1")

    # ------------------------------------------------------------------
    def read(self, path: str, today: Optional[date] = None) -> ReceiptResult:
        """识别一份账单。"""
        target = Path(str(path).strip().strip('"').strip("'"))
        if not target.exists():
            return ReceiptResult(
                ok=False,
                message=f"找不到文件：{target}\n请把账单截图放到项目目录下，然后给出完整路径，"
                        f"例如 samples/bill_sample.png",
                image_path=str(target),
            )

        suffix = target.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            return self._from_text(target, today)

        if suffix not in IMAGE_SUFFIXES:
            return ReceiptResult(
                ok=False,
                message=f"暂不支持的文件类型：{suffix or '无扩展名'}。支持图片（png/jpg/bmp/webp）"
                        f"与文本账单（txt/csv/md）。",
                image_path=str(target),
            )

        errors: List[str] = []
        if self.vision_available:
            result = self._from_vision(target, today)
            if result.ok and result.records:
                return result
            errors.append("多模态识别未返回可用结果：" + (result.message or "空结果"))

        result = self._from_windows_ocr(target, today)
        result.errors.extend(errors)
        if result.ok and result.records:
            if errors:
                result.message += "\n（多模态通道不可用，已自动改用 Windows 离线 OCR）"
            return result

        errors.append(result.message)
        hint = "；".join(filter(None, errors)) or "没有可用的识别通道"
        return ReceiptResult(
            ok=False,
            message=(
                "这张图片没能识别出交易明细。\n"
                f"  识别通道情况：{hint}\n"
                "  可选做法：\n"
                "    1) 配置多模态大模型（FINAGENT_LLM_API_KEY + 支持视觉的模型）后重试；\n"
                "    2) 确认账单截图足够清晰、文字不过小；\n"
                "    3) 直接把账单导出成 CSV/TXT 后交给我解析。"
            ),
            image_path=str(target),
            errors=errors,
        )

    # ------------------------------------------------------------------
    def _from_text(self, target: Path, today: Optional[date]) -> ReceiptResult:
        raw = target.read_text(encoding="utf-8", errors="replace")
        records = parse_receipt_text(raw, today)
        return ReceiptResult(
            ok=bool(records),
            backend="text_file",
            backend_label="文本账单解析",
            records=records,
            raw_text=raw,
            image_path=str(target),
            message="" if records else "文本里没有解析出交易记录。",
        )

    def _from_vision(self, target: Path, today: Optional[date]) -> ReceiptResult:
        assert self.llm is not None
        payload = self.llm.chat_vision_json(str(target), VISION_PROMPT)
        if not payload:
            return ReceiptResult(False, "vision_llm", "多模态大模型",
                                 message=self.llm.last_error or "调用失败",
                                 image_path=str(target))
        records = self._records_from_payload(payload, today)
        return ReceiptResult(
            ok=bool(records),
            backend="vision_llm",
            backend_label=f"多模态大模型（{self.llm.model}）",
            records=records,
            raw_text=json.dumps(payload, ensure_ascii=False),
            image_path=str(target),
            message="" if records else "模型返回的 JSON 里没有交易记录。",
        )

    def _records_from_payload(self, payload: dict, today: Optional[date]) -> List[ParsedTransaction]:
        records: List[ParsedTransaction] = []
        fallback_date = (today or date.today()).isoformat()
        for item in payload.get("records") or []:
            if not isinstance(item, dict):
                continue
            try:
                amount = float(item.get("amount", 0))
            except (TypeError, ValueError):
                continue
            if abs(amount) < 0.01:
                continue
            merchant = str(item.get("merchant") or "未知商户").strip()
            category = str(item.get("category") or "").strip()
            if category not in CATEGORY_CHOICES:
                category = guess_category(merchant)
            records.append(
                ParsedTransaction(
                    date=str(item.get("date") or fallback_date)[:10],
                    time=str(item.get("time") or "")[:5],
                    merchant=merchant,
                    amount=round(amount, 2),
                    category=category,
                    confidence=0.95,
                )
            )
        return records

    def _from_windows_ocr(self, target: Path, today: Optional[date]) -> ReceiptResult:
        powershell = self.powershell_path()
        script = self._ocr_script()
        if not powershell:
            return ReceiptResult(False, "windows_ocr", "Windows 内置 OCR",
                                 message="未找到 powershell.exe", image_path=str(target))
        if not script.exists():
            return ReceiptResult(False, "windows_ocr", "Windows 内置 OCR",
                                 message=f"缺少 OCR 脚本 {script.name}", image_path=str(target))
        with tempfile.TemporaryDirectory(prefix="finagent_ocr_") as tmp:
            out_file = Path(tmp) / "ocr.txt"
            command = [
                powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
                "-ImagePath", str(target.resolve()),
                "-OutFile", str(out_file),
                "-Language", "zh-Hans-CN",
            ]
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, errors="ignore",
                    timeout=self.ocr_timeout,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.last_error = f"调用 OCR 失败：{exc}"
                return ReceiptResult(False, "windows_ocr", "Windows 内置 OCR",
                                     message=self.last_error, image_path=str(target))
            if completed.returncode != 0 or not out_file.exists():
                detail = (completed.stderr or completed.stdout or "").strip()[:200]
                self.last_error = f"OCR 引擎返回错误：{detail or '未知错误'}"
                return ReceiptResult(False, "windows_ocr", "Windows 内置 OCR",
                                     message=self.last_error, image_path=str(target))
            raw_text = out_file.read_text(encoding="utf-8", errors="replace")
        records = parse_receipt_text(raw_text, today)
        if not records:
            return ReceiptResult(
                ok=False, backend="windows_ocr", backend_label="Windows 内置 OCR",
                raw_text=raw_text, image_path=str(target),
                message="OCR 有文字输出，但没有解析出规范的交易记录。",
            )
        return ReceiptResult(
            ok=True,
            backend="windows_ocr",
            backend_label="Windows 内置 OCR（离线）",
            records=records,
            raw_text=raw_text,
            image_path=str(target),
        )

    # ------------------------------------------------------------------
    def read_text(self, text: str, today: Optional[date] = None) -> ReceiptResult:
        """直接解析用户粘贴进来的账单文本（微信/支付宝账单导出常见格式）。"""
        records = parse_receipt_text(text or "", today)
        if not records:
            return ReceiptResult(
                ok=False,
                backend="pasted_text",
                backend_label="粘贴文本解析",
                raw_text=text or "",
                message="这段文本里没有解析出交易记录（需要能看出金额，最好还带日期）。",
            )
        return ReceiptResult(
            ok=True,
            backend="pasted_text",
            backend_label="粘贴文本解析",
            records=records,
            raw_text=text or "",
        )

    @staticmethod
    def looks_like_bill(text: str) -> bool:
        return looks_like_pasted_bill(text)
