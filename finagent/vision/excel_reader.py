"""Read transaction and summary workbooks from .xlsx without external packages."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

from finagent.utils import parse_date
from finagent.vision.text_codec import decode_text_bytes
from finagent.vision.receipt_parser import (
    BillSummary,
    CategorySpend,
    MonthlySpend,
    ParsedTransaction,
    guess_category,
)

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
MONTH_RE = re.compile(r"(20\d{2})\D{0,3}(\d{1,2})")
MONTH_YEAR_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
MONTH_ONLY_RE = re.compile(r"(\d{1,2})\s*月")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::\d{2})?")
CATEGORY_CHOICES = {"餐饮", "交通", "购物", "订阅服务", "旅行", "生活缴费",
                    "医疗", "娱乐", "其他", "转账", "住房", "教育", "人情往来"}


class ExcelReadError(ValueError):
    """Raised when an XLSX package cannot be understood."""


@dataclass
class ExcelSheet:
    name: str
    rows: List[List[Any]] = field(default_factory=list)


@dataclass
class ExcelReadResult:
    records: List[ParsedTransaction] = field(default_factory=list)
    summary: Optional[BillSummary] = None
    sheets: List[ExcelSheet] = field(default_factory=list)
    raw_text: str = ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, name: str) -> Iterable[ET.Element]:
    return (item for item in list(node) if _local_name(item.tag) == name)


def _text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return "".join(item.text or "" for item in node.iter() if _local_name(item.tag) == "t")


def _column_index(reference: str) -> int:
    match = CELL_REF_RE.match(reference or "")
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [_text(item) for item in _children(root, "si")]


def _cell_value(cell: ET.Element, shared: List[str]) -> Any:
    kind = cell.attrib.get("t", "")
    value_node = next(_children(cell, "v"), None)
    raw = value_node.text if value_node is not None else ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "inlineStr":
        return _text(next(_children(cell, "is"), None))
    if kind == "b":
        return raw == "1"
    if kind in {"str", "e"}:
        return raw
    if raw == "":
        return ""
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def _read_sheet(xml: bytes, shared: List[str]) -> List[List[Any]]:
    root = ET.fromstring(xml)
    rows: List[List[Any]] = []
    data = next(_children(root, "sheetData"), None)
    if data is None:
        return rows
    for row_node in _children(data, "row"):
        row: List[Any] = []
        for cell in _children(row_node, "c"):
            index = _column_index(cell.attrib.get("r", ""))
            while len(row) <= index:
                row.append("")
            row[index] = _cell_value(cell, shared)
        if any(str(value).strip() for value in row):
            rows.append(row)
    return rows


def read_xlsx_sheets(path: Path | str) -> List[ExcelSheet]:
    target = Path(path)
    try:
        with zipfile.ZipFile(target) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationships = {
                item.attrib.get("Id", ""): item.attrib.get("Target", "")
                for item in rels_root
            }
            shared = _shared_strings(archive)
            sheets: List[ExcelSheet] = []
            sheets_node = next(_children(workbook, "sheets"), None)
            if sheets_node is None:
                raise ExcelReadError("Excel 文件没有工作表")
            for sheet in _children(sheets_node, "sheet"):
                name = sheet.attrib.get("name", "Sheet")
                rel_id = next(
                    (value for key, value in sheet.attrib.items() if _local_name(key) == "id"),
                    "",
                )
                target_name = relationships.get(rel_id, "")
                if not target_name:
                    continue
                if target_name.startswith("/"):
                    sheet_path = target_name.lstrip("/")
                elif target_name.startswith("xl/"):
                    sheet_path = target_name
                else:
                    sheet_path = "xl/" + target_name.lstrip("./")
                if sheet_path not in archive.namelist():
                    continue
                sheets.append(ExcelSheet(name, _read_sheet(archive.read(sheet_path), shared)))
            return sheets
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ExcelReadError(f"无法读取 Excel 文件：{exc}") from exc


def _text_value(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = _text_value(value).replace(",", "").replace("，", "").replace("¥", "").replace("￥", "")
    match = NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return round(float(match.group(0)), 2)
    except ValueError:
        return None


def _excel_date(value: Any, today: date) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and 20_000 <= float(value) <= 80_000:
        return (date(1899, 12, 30) + timedelta(days=float(value))).isoformat()
    parsed = parse_date(_text_value(value), today)
    return parsed.isoformat() if parsed else None


def _excel_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, (int, float)) and 20_000 <= float(value) <= 80_000:
        seconds = int(round((float(value) % 1) * 86400))
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"
    match = TIME_RE.search(_text_value(value))
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return ""


def _header_map(row: List[Any]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for index, value in enumerate(row):
        text = re.sub(r"\s+", "", _text_value(value).lower())
        if not text:
            continue
        groups = {
            "date": ("日期", "交易日期", "账单日期", "交易时间", "时间", "date"),
            "merchant": ("商户", "商户名称", "交易对方", "收款方", "对方", "merchant"),
            "description": ("商品说明", "商品", "用途", "备注", "摘要", "说明", "description", "product"),
            "transaction_type": ("交易分类", "交易类型", "业务类型", "transactiontype"),
            "amount": ("金额", "交易金额", "支出金额", "消费金额", "支出", "收入", "amount"),
            "category": ("类别", "分类", "消费类别", "category"),
            "direction": ("收/支", "收支", "收入/支出", "交易方向"),
            "month": ("月份", "月度", "年月", "month"),
            "ratio": ("占比", "比例", "百分比", "ratio"),
        }
        for key, aliases in groups.items():
            if key not in mapping and any(alias in text for alias in aliases):
                mapping[key] = index
    return mapping


def _transactions_from_sheet(sheet: ExcelSheet, today: date) -> List[ParsedTransaction]:
    rows = sheet.rows
    for header_index, header in enumerate(rows):
        columns = _header_map(header)
        if "date" not in columns or "amount" not in columns:
            continue
        if "merchant" not in columns and "description" not in columns:
            continue
        records: List[ParsedTransaction] = []
        for row in rows[header_index + 1:]:
            date_value = row[columns["date"]] if columns["date"] < len(row) else ""
            amount_value = row[columns["amount"]] if columns["amount"] < len(row) else ""
            merchant_index = columns.get("merchant")
            description_index = columns.get("description")
            type_index = columns.get("transaction_type") or columns.get("category")
            merchant_value = (
                row[merchant_index]
                if merchant_index is not None and merchant_index < len(row)
                else ""
            )
            description_value = (
                row[description_index]
                if description_index is not None and description_index < len(row)
                else ""
            )
            transaction_type = (
                row[type_index]
                if type_index is not None and type_index < len(row)
                else ""
            )
            parsed_date = _excel_date(date_value, today)
            amount = _number(amount_value)
            merchant = _merchant_label(merchant_value, description_value, transaction_type)
            if not parsed_date or amount is None or not merchant:
                continue
            direction_index = columns.get("direction")
            direction = (
                _text_value(row[direction_index])
                if direction_index is not None and direction_index < len(row)
                else ""
            )
            if direction == "收入":
                amount = abs(amount)
            elif direction in {"支出", "消费"}:
                amount = -abs(amount)
            elif direction in {"不计收支", "中性交易"}:
                amount = 0.0
            elif amount > 0 and "收入" not in _text_value(header[columns["amount"]]):
                amount = -amount
            category_index = columns.get("category")
            category = (
                _text_value(row[category_index])
                if category_index is not None and category_index < len(row)
                else ""
            )
            records.append(ParsedTransaction(
                date=parsed_date,
                time=_excel_time(date_value),
                merchant=merchant,
                amount=round(amount, 2),
                category=category if category in CATEGORY_CHOICES else guess_category(merchant),
                confidence=0.95,
            ))
        if records:
            return records
    return []


def _merchant_label(counterparty: Any, description: Any, transaction_type: Any) -> str:
    """Use description/type when a CSV exports '??' as the counterparty."""
    raw_counterparty = _text_value(counterparty)
    cleaned_counterparty = re.sub(r"[?？]+", "", raw_counterparty).strip()
    description_text = _text_value(description)
    type_text = _text_value(transaction_type)
    if cleaned_counterparty:
        return cleaned_counterparty
    if "转账" in f"{description_text}{type_text}":
        return "转账"
    if description_text:
        return description_text
    if type_text:
        return type_text
    return "未知商户"


def _month_key(value: Any, default_year: int) -> Optional[str]:
    text = _text_value(value)
    match = MONTH_YEAR_RE.search(text)
    if not match:
        match = MONTH_RE.search(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    match = MONTH_ONLY_RE.search(text)
    if match:
        month = int(match.group(1))
        if 1 <= month <= 12:
            return f"{default_year:04d}-{month:02d}"
    return None


def _summary_from_sheet(sheet: ExcelSheet, today: date) -> Optional[BillSummary]:
    rows = sheet.rows
    months: Dict[str, MonthlySpend] = {}
    categories: List[CategorySpend] = []
    expected_period = ""
    total = 0.0
    for row_index, row in enumerate(rows):
        columns = _header_map(row)
        if "month" in columns and "amount" in columns:
            for data_row in rows[row_index + 1:]:
                next_header = _header_map(data_row)
                if "month" in next_header and "amount" in next_header:
                    break
                month_value = data_row[columns["month"]] if columns["month"] < len(data_row) else ""
                amount = _number(data_row[columns["amount"]] if columns["amount"] < len(data_row) else "")
                month = _month_key(month_value, today.year)
                if month and amount is not None and amount > 0:
                    months[month] = MonthlySpend(month, amount)
                    expected_period = month
        if "category" in columns and "amount" in columns:
            for data_row in rows[row_index + 1:]:
                next_header = _header_map(data_row)
                if "category" in next_header and "amount" in next_header:
                    break
                category_text = _text_value(
                    data_row[columns["category"]] if columns["category"] < len(data_row) else ""
                )
                amount = _number(data_row[columns["amount"]] if columns["amount"] < len(data_row) else "")
                if not category_text or amount is None or amount <= 0:
                    continue
                ratio = None
                ratio_index = columns.get("ratio")
                if ratio_index is not None and ratio_index < len(data_row):
                    ratio_text = _text_value(data_row[ratio_index])
                    ratio = _number(ratio_text.rstrip("%"))
                category = guess_category(category_text)
                if category == "其他" and category_text not in {"其他", "其他消费", "未分类"}:
                    category = category_text
                categories.append(CategorySpend(category, amount, ratio))
        for cell in row:
            text = _text_value(cell)
            if any(word in text for word in ("总支出", "本月支出", "消费合计", "合计")):
                for other in row:
                    candidate = _number(other)
                    if candidate is not None and candidate > 0:
                        total = candidate
                        break
    if not months and not categories:
        return None
    if total <= 0:
        total = max((item.amount for item in months.values()), default=0.0)
    if total <= 0:
        total = sum(item.amount for item in categories)
    if not expected_period and months:
        expected_period = max(months)
    return BillSummary(
        period=expected_period,
        total_expense=round(total, 2),
        categories=sorted(categories, key=lambda item: item.amount, reverse=True),
        months=sorted(months.values(), key=lambda item: item.month),
        raw_text="\n".join("\t".join(_text_value(cell) for cell in row) for row in rows),
    )


def read_excel(path: Path | str, today: Optional[date] = None) -> ExcelReadResult:
    sheets = read_xlsx_sheets(path)
    today = today or date.today()
    all_records: List[ParsedTransaction] = []
    summary: Optional[BillSummary] = None
    for sheet in sheets:
        records = _transactions_from_sheet(sheet, today)
        if records:
            all_records.extend(records)
            continue
        candidate = _summary_from_sheet(sheet, today)
        if candidate and summary is None:
            summary = candidate
    raw_text = "\n\n".join(
        f"# {sheet.name}\n" + "\n".join("\t".join(_text_value(cell) for cell in row) for row in sheet.rows)
        for sheet in sheets
    )
    return ExcelReadResult(records=all_records, summary=summary, sheets=sheets, raw_text=raw_text)


def read_csv(path: Path | str, today: Optional[date] = None) -> ExcelReadResult:
    """Parse a CSV export with the same header mapping as XLSX."""
    target = Path(path)
    raw = decode_text_bytes(target.read_bytes())
    try:
        dialect = csv.Sniffer().sniff(raw[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [list(row) for row in csv.reader(io.StringIO(raw), dialect)]
    sheet = ExcelSheet("CSV", rows)
    today = today or date.today()
    records = _transactions_from_sheet(sheet, today)
    summary = None if records else _summary_from_sheet(sheet, today)
    return ExcelReadResult(
        records=records,
        summary=summary,
        sheets=[sheet],
        raw_text=raw,
    )
