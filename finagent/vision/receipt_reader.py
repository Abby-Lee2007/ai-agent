"""账单识别调度器：按可用性自动选择识别后端。

后端优先级：
1. `vision_llm`  —— 多模态大模型，识别最准，还能直接给出消费分类；
2. `windows_ocr` —— Windows 内置 OCR（Windows.Media.Ocr），完全离线、零依赖；
3. `text_file`   —— .txt/.csv/.md 等文本账单直接解析。

任何一个后端不可用都会自动降级，并把原因写进 message，方便排查。
"""
from __future__ import annotations

import csv
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
from finagent.vision.excel_reader import ExcelReadError, read_csv, read_excel
from finagent.vision.text_codec import decode_text_bytes
from finagent.vision.receipt_parser import (
    BillSummary,
    ParsedTransaction,
    guess_category,
    looks_like_summary_page,
    looks_like_pasted_bill,
    parse_bill_summary,
    parse_receipt_text,
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"}
TEXT_SUFFIXES = {".txt", ".csv", ".md", ".log", ".tsv"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
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
    summary: Optional[BillSummary] = None

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
            "excel_file": True,
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
        if suffix == ".csv":
            return self._from_csv(target, today)
        if suffix in TEXT_SUFFIXES:
            return self._from_text(target, today)
        if suffix in EXCEL_SUFFIXES:
            return self._from_excel(target, today)

        if suffix not in IMAGE_SUFFIXES:
            return ReceiptResult(
                ok=False,
                message=f"暂不支持的文件类型：{suffix or '无扩展名'}。支持图片（png/jpg/bmp/webp）、"
                        f"文本账单（txt/csv/md）和 Excel（xlsx/xlsm）。",
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
        if "汇总页" in (result.message or ""):
            return result          # 汇总页要给出明确指引，不要再套通用失败文案
        if result.ok and (result.records or result.summary):
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
        raw = decode_text_bytes(target.read_bytes())
        summary = (
            parse_bill_summary(raw, today)
            if looks_like_summary_page(raw)
            else None
        )
        if summary is not None:
            return ReceiptResult(
                ok=True,
                backend="text_file",
                backend_label="文本账单汇总分析",
                raw_text=raw,
                image_path=str(target),
                summary=summary,
            )
        records = parse_receipt_text(raw, today)
        return ReceiptResult(
            ok=bool(records),
            backend="text_file",
            backend_label="文本账单解析",
            records=records,
            raw_text=raw,
            image_path=str(target),
            message="" if records else "文本里没有解析出交易记录，也没有识别到可分析的汇总结构。",
        )

    def _from_excel(self, target: Path, today: Optional[date]) -> ReceiptResult:
        try:
            excel = read_excel(target, today)
        except ExcelReadError as exc:
            return ReceiptResult(
                ok=False,
                backend="excel_file",
                backend_label="Excel 账单解析",
                image_path=str(target),
                message=str(exc),
            )
        if excel.summary is not None:
            return ReceiptResult(
                ok=True,
                backend="excel_file",
                backend_label="Excel 汇总分析",
                raw_text=excel.raw_text,
                image_path=str(target),
                summary=excel.summary,
            )
        if excel.records:
            return ReceiptResult(
                ok=True,
                backend="excel_file",
                backend_label="Excel 交易明细解析",
                records=excel.records,
                raw_text=excel.raw_text,
                image_path=str(target),
            )
        return ReceiptResult(
            ok=False,
            backend="excel_file",
            backend_label="Excel 账单解析",
            raw_text=excel.raw_text,
            image_path=str(target),
            message=("Excel 已读取，但没有找到可识别的交易列（日期/商户/金额）"
                     "或汇总列（月份/类别/支出）。"),
        )

    def _from_csv(self, target: Path, today: Optional[date]) -> ReceiptResult:
        try:
            table = read_csv(target, today)
        except (ExcelReadError, csv.Error) as exc:
            return ReceiptResult(
                ok=False,
                backend="csv_file",
                backend_label="CSV 账单解析",
                image_path=str(target),
                message=f"无法读取 CSV 文件：{exc}",
            )
        if table.summary is not None:
            return ReceiptResult(
                ok=True,
                backend="csv_file",
                backend_label="CSV 汇总分析",
                raw_text=table.raw_text,
                image_path=str(target),
                summary=table.summary,
            )
        if table.records:
            return ReceiptResult(
                ok=True,
                backend="csv_file",
                backend_label="CSV 交易明细解析",
                records=table.records,
                raw_text=table.raw_text,
                image_path=str(target),
            )
        return ReceiptResult(
            ok=False,
            backend="csv_file",
            backend_label="CSV 账单解析",
            raw_text=table.raw_text,
            image_path=str(target),
            message="CSV 已读取，但没有找到日期/商户/金额或月份/类别/支出列。",
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
        summary = (
            parse_bill_summary(raw_text, today)
            if looks_like_summary_page(raw_text)
            else None
        )
        if summary is not None:
            return ReceiptResult(
                ok=True,
                backend="windows_ocr",
                backend_label="Windows 内置 OCR（汇总分析）",
                raw_text=raw_text,
                image_path=str(target),
                summary=summary,
            )
        records = parse_receipt_text(raw_text, today)
        if looks_like_summary_page(raw_text):
            return ReceiptResult(
                ok=False, backend="windows_ocr", backend_label="Windows 内置 OCR",
                raw_text=raw_text, image_path=str(target),
                message=("这张是账单「汇总页」，但没有提取到足够的支出构成或月度趋势。"
                         "请截出包含分类金额、合计或每月支出的区域再上传。"),
            )
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
        summary = (
            parse_bill_summary(text or "", today)
            if looks_like_summary_page(text or "")
            else None
        )
        if summary is not None:
            return ReceiptResult(
                ok=True,
                backend="pasted_text",
                backend_label="粘贴文本汇总分析",
                raw_text=text or "",
                summary=summary,
            )
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
