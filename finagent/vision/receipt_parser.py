"""把小票/账单文本解析成结构化交易。

OCR 的输出永远是"脏"的，这个模块专门处理真实 OCR 的几类典型噪声：
1. 中文之间被插入了空格（"携 程 旅 行 网"）；
2. 连字符被识别成汉字"一"（2026 一 10 一 12），负号同理；
3. 全角标点（：．，）与全角数字；
4. 小数点被吞掉，金额变成 "2480 00"；
5. 一行被拆成多行（某些引擎会按列重排）。

解析策略：优先"按行解析"，失败时退化为"按列顺序对齐"（日期列 / 金额列）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

FULLWIDTH_MAP = str.maketrans({
    "：": ":", "．": ".", "，": ",", "；": ";", "（": "(", "）": ")",
    "％": "%", "￥": "¥", "－": "-", "–": "-", "—": "-", "～": "~",
    "\u3000": " ", "\u2003": " ", "\u2002": " ", "\u2009": " ", "\u00a0": " ",
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})
HEADER_WORDS = ("账单", "周期", "卡号", "户名", "合计", "小计", "总计", "汇总", "应还",
                "总额", "余额", "打印", "页码", "备注", "说明", "交易明细")
SUMMARY_WORDS = ("支出¥", "收入¥", "全部账单", "查找交易", "收支统计", "本期",
                 "合计", "小计", "总计", "汇总", "应还", "总额")
DATE_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
# 中文日期：10月14日13:11 / 10月14日 13:11
CN_DATETIME_RE = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{1,2})?")
YEAR_RE = re.compile(r"(20\d{2})\s*年")
# 行尾金额：微信/支付宝账单导出的常见排版是"商户  金额"，金额在行末
TRAILING_AMOUNT_RE = re.compile(r"([-+一])?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*$")
# 负号可能被 OCR 识别成汉字"一"，所以符号位要把"一"也算进来
MONEY_RE = re.compile(r"([-+一])?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
INCOME_WORDS = ("收入", "退款", "工资", "转入", "存入", "还款入账", "退货")
TIME_RE = re.compile(r"(\d{1,2})\s*:\s*(\d{2})")
CATEGORY_RULES = [
    ("餐饮", ("咖啡", "星巴克", "瑞幸", "茶", "餐", "烧烤", "火锅", "外卖", "美团", "饿了么",
              "肯德基", "麦当劳", "面馆", "饭店", "食堂")),
    ("交通", ("滴滴", "出行", "地铁", "公交", "打车", "加油", "中石化", "停车", "高速", "铁路", "航空")),
    ("购物", ("京东", "淘宝", "天猫", "商城", "优衣库", "山姆", "超市", "便利店", "名创", "数码")),
    ("订阅服务", ("视频", "音乐", "云存储", "网盘", "会员", "keep", "icloud", "订阅", "续费")),
    ("旅行", ("携程", "机票", "酒店", "旅行", "民宿", "飞猪", "去哪儿")),
    ("生活缴费", ("话费", "移动", "联通", "电信", "燃气", "电网", "电力", "物业", "水费")),
    ("医疗", ("医院", "药房", "医疗", "诊所", "体检")),
    ("娱乐", ("电影", "游戏", "ktv", "健身", "影院", "猫眼")),
    ("人情往来", ("红包", "群收款", "礼金")),
    ("转账", ("转账", "转给", "退款")),
]


@dataclass
class ParsedTransaction:
    """从账单里解析出来的一笔交易。"""

    date: str
    merchant: str
    amount: float
    time: str = ""
    category: str = "其他"
    raw_line: str = ""
    confidence: float = 1.0

    def to_payload(self) -> Dict[str, object]:
        return {
            "date": self.date,
            "time": self.time,
            "merchant": self.merchant,
            "amount": self.amount,
            "category": self.category,
        }


def normalize_line(line: str) -> str:
    """单行规范化：全角转半角、修补被识别成汉字的连字符、去掉中文之间的空格。"""
    text = (line or "").strip().translate(FULLWIDTH_MAP)
    if not text:
        return ""
    # 时间里的冒号常被识别成 "10 : 05"，先把空格收掉，避免它被当成小数
    text = re.sub(r"(?<=\d)\s*:\s*(?=\d)", ":", text)
    # "2026 一 10 一 12" -> "2026-10-12"
    text = re.sub(r"(?<=\d)\s*[一-]\s*(?=\d)", "-", text)
    # 金额小数点被吞："2480 00" / "38 00" -> "2480.00" / "38.00"
    text = re.sub(r"(?<!\d)(\d{1,5})\s+(\d{2})(?!\d)(?=\s*(?:元|$))", r"\1.\2", text)
    # 千分位被空格拆开："12 345" -> "12,345"
    text = re.sub(r"(?<!\d)(\d{1,3})\s+(\d{3})(?!\d)", r"\1,\2", text)
    text = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=\d)", "", text)
    text = re.sub(r"(?<=\d)\s+(?=[\u4e00-\u9fa5])", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def normalize_text(text: str) -> str:
    """逐行规范化后重新拼接。"""
    return "\n".join(filter(None, (normalize_line(line) for line in (text or "").splitlines())))


def guess_category(merchant: str) -> str:
    """按商户关键字猜消费类别，用于没有分类信息的账单截图。"""
    name = (merchant or "").lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword in name for keyword in keywords):
            return category
    return "其他"


def _looks_like_header(line: str) -> bool:
    return any(word in line for word in HEADER_WORDS)


def _looks_like_summary(line: str) -> bool:
    """账单汇总行：'支出¥7283.34 收入¥9577.09'、'本期应还' 这类不当作交易。"""
    if any(word in line for word in SUMMARY_WORDS):
        return True
    return "支出" in line and "收入" in line


def is_datetime_only(line: str) -> bool:
    """整行是否只是一个时间戳，例如 '10月14日13:11' 或 '10月13日23:33  对方已退还'。"""
    stripped = line.strip()
    if not stripped:
        return False
    match = CN_DATETIME_RE.search(stripped)
    if match and not stripped[:match.start()].strip():
        tail = stripped[match.end():].strip()
        return not TRAILING_AMOUNT_RE.search(tail)
    match = DATE_RE.search(stripped)
    if match and not stripped[:match.start()].strip():
        tail = stripped[match.end():].strip()
        time_match = TIME_RE.match(tail)
        rest = tail[time_match.end():].strip() if time_match else tail
        return not TRAILING_AMOUNT_RE.search(rest)
    return False


def _trailing_amount(line: str):
    """取行尾金额。返回 (值, 起始下标, 符号) 或 None。"""
    match = TRAILING_AMOUNT_RE.search(line)
    if not match:
        return None
    try:
        value = float(match.group(2).replace(",", ""))
    except ValueError:
        return None
    if abs(value) < 0.01:
        return None
    return value, match.start(), (match.group(1) or "")


def _parse_cn_datetime(text: str, default_year: int, today: date):
    """解析 '10月14日13:11' / '2026-10-14 13:11'，返回 (date, 'HH:MM') 或 None。"""
    match = CN_DATETIME_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        hour = int(match.group(3))
        minute = int(match.group(4) or 0)
        try:
            parsed = date(default_year, month, day)
        except ValueError:
            return None
        # 没写年份且日期落在未来较多时，按上一年的账单处理
        if (parsed - today).days > 31:
            try:
                parsed = date(default_year - 1, month, day)
            except ValueError:
                pass
        return parsed, f"{hour:02d}:{minute:02d}"
    match = DATE_RE.search(text)
    if match:
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        time_match = TIME_RE.search(text[match.end():])
        clock = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else ""
        return parsed, clock
    return None


def _clean_merchant(text: str) -> str:
    cleaned = re.sub(r"[|~¥$*#:;,\-]+", " ", text or "")
    cleaned = re.sub(r"\b\d{4,}\b", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" \t")
    return cleaned[:30]


def _parse_line(line: str, assume_expense: bool = True) -> Optional[ParsedTransaction]:
    """尝试把一行解析成一笔交易：必须同时出现日期和金额。"""
    if not line or _looks_like_header(line) or _looks_like_summary(line):
        return None
    date_match = DATE_RE.search(line)
    if not date_match:
        return None
    try:
        day = date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
    except ValueError:
        return None
    tail = line[date_match.end():]
    money_matches = [m for m in MONEY_RE.finditer(tail) if m.group(2).strip(",.0") != ""]
    if not money_matches:
        return None
    money = money_matches[-1]
    try:
        amount = float(money.group(2).replace(",", ""))
    except ValueError:
        return None
    if money.group(1) in {"-", "一"}:
        amount = -amount
    elif assume_expense and not money.group(1) == "+" and not any(w in line for w in INCOME_WORDS):
        # 账单/小票里不带符号的数字几乎都是支出
        amount = -abs(amount)
    middle = tail[:money.start()]
    time_match = TIME_RE.search(middle)
    time_text = ""
    if time_match:
        time_text = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        middle = middle[:time_match.start()] + middle[time_match.end():]
    merchant = _clean_merchant(middle)
    if not merchant:
        return None
    return ParsedTransaction(
        date=day.isoformat(),
        time=time_text,
        merchant=merchant,
        amount=amount,
        category=guess_category(merchant),
        raw_line=line,
    )


def _column_fallback(lines: List[str], assume_expense: bool = True) -> List[ParsedTransaction]:
    """按行解析失败时，退化为"日期列 / 金额列 / 商户列"顺序对齐。

    有些 OCR 引擎会按列切分文本（先输出所有日期，再输出商户和金额），
    只要各列内部的相对顺序不变，就能按序号重新拼回记录。
    """
    dates: List[str] = []
    amounts: List[float] = []
    merchants: List[str] = []
    for line in lines:
        if _looks_like_header(line):
            continue
        remainder = line
        date_match = DATE_RE.search(line)
        if date_match:
            try:
                dates.append(date(int(date_match.group(1)), int(date_match.group(2)),
                                  int(date_match.group(3))).isoformat())
            except ValueError:
                pass
            remainder = line[:date_match.start()] + line[date_match.end():]
        rest = remainder
        for money in reversed(list(MONEY_RE.finditer(remainder))):
            try:
                value = float(money.group(2).replace(",", ""))
            except ValueError:
                continue
            if money.group(1) in {"-", "一"}:
                value = -value
            elif assume_expense and money.group(1) != "+":
                value = -abs(value)
            if abs(value) >= 0.01:
                amounts.append(value)
                rest = rest[:money.start()] + rest[money.end():]
                break
        candidate = _clean_merchant(rest)
        if candidate and not re.search(r"\d", candidate):
            merchants.append(candidate)
    if not dates or not amounts:
        return []
    records: List[ParsedTransaction] = []
    for index in range(min(len(dates), len(amounts))):
        merchant = merchants[index] if index < len(merchants) else "未知商户"
        records.append(
            ParsedTransaction(
                date=dates[index],
                merchant=merchant,
                amount=amounts[index],
                category=guess_category(merchant),
                confidence=0.6,
            )
        )
    return records


def _parse_paired(lines: List[str], default_year: int, today: date,
                  assume_expense: bool = True) -> List[ParsedTransaction]:
    """处理"商户+金额"一行、"时间"在相邻一行 的排版。

    微信/支付宝导出的账单常见格式：
        抖音电商商家        -148.72
        10月14日13:11
    时间和商户是拆开的两行，所以要把相邻两行配对成一条记录。
    """
    stamps: Dict[int, tuple] = {}
    entries: Dict[int, tuple] = {}
    for index, line in enumerate(lines):
        if not line or _looks_like_header(line) or _looks_like_summary(line):
            continue
        stamp = _parse_cn_datetime(line, default_year, today)
        if is_datetime_only(line):
            if stamp:
                stamps[index] = stamp
            continue
        found = _trailing_amount(line)
        if found:
            value, start, sign = found
            merchant = _clean_merchant(line[:start])
            if merchant and re.search(r"[\u4e00-\u9fa5A-Za-z]", merchant):
                entries[index] = (merchant, value, sign)
        if stamp:
            stamps[index] = stamp
    # 时间戳与记录的配对分三轮，兼容三种常见排版
    assigned: Dict[int, tuple] = {}
    used_stamps: set = set()
    for index in entries:
        if index in stamps:
            assigned[index] = stamps[index]
            used_stamps.add(index)
    for index in sorted(entries):
        if index in assigned:
            continue
        below = index + 1
        if below in stamps and below not in entries and below not in used_stamps:
            assigned[index] = stamps[below]
            used_stamps.add(below)
    pending_entries = [i for i in sorted(entries) if i not in assigned]
    pending_stamps = [j for j in sorted(stamps) if j not in used_stamps]
    for entry_index, stamp_index in zip(pending_entries, pending_stamps):
        assigned[entry_index] = stamps[stamp_index]

    records: List[ParsedTransaction] = []
    for index, (merchant, value, sign) in sorted(entries.items()):
        stamp = assigned.get(index)
        if stamp is None:
            parsed_date, clock = today, ""
            confidence = 0.5
        else:
            parsed_date, clock = stamp
            confidence = 0.9
        if sign in {"-", "一"}:
            amount = -value
        elif sign == "+" or any(word in merchant for word in INCOME_WORDS):
            amount = value
        elif assume_expense:
            amount = -value
        else:
            amount = value
        records.append(
            ParsedTransaction(
                date=parsed_date.isoformat(),
                time=clock,
                merchant=merchant,
                amount=round(amount, 2),
                category=guess_category(merchant),
                raw_line=lines[index],
                confidence=confidence,
            )
        )
    return records


BILL_HINTS = ("账单", "交易明细", "收支", "退款", "红包", "群收款", "扫二维码",
              "商户", "支付", "转账", "已全额退款", "对方已退还")


def looks_like_pasted_bill(text: str) -> bool:
    """判断用户是不是直接粘了一整段账单文本进来。

    这类文本不该逐行当成指令去解析（"转账-退款 +6.50" 会被误判成转账），
    而应该整体交给账单解析器。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    amount_lines = 0
    for line in lines:
        if _looks_like_summary(line) or _looks_like_header(line):
            continue
        if _trailing_amount(line) or is_datetime_only(line):
            amount_lines += 1
    if amount_lines >= 3 and amount_lines >= len(lines) * 0.5:
        return True
    has_hint = any(hint in text for hint in BILL_HINTS)
    return has_hint and amount_lines >= 2


def looks_like_bill_fragment(line: str) -> bool:
    """单行是否"像账单里的一行"（因此后面可能还有内容）。

    命令行用它来判断：这一行要不要当成"一段粘贴的开始"。
    """
    text = (line or "").strip()
    if not text:
        return False
    normalized = normalize_line(text)
    if is_datetime_only(normalized):
        return True
    return bool(TRAILING_AMOUNT_RE.search(normalized))


def strip_comment_lines(text: str) -> str:
    """去掉以 # 开头的注释行（模板文件里用来写说明）。"""
    return "\n".join(
        line for line in (text or "").splitlines() if not line.lstrip().startswith("#")
    )


def parse_receipt_text(
    text: str,
    today: Optional[date] = None,
    assume_expense: bool = True,
) -> List[ParsedTransaction]:
    """解析账单文本，返回交易列表。

    依次尝试三种排版，取解析出记录最多的一种：
    1. 一行内同时有日期和金额；
    2. 商户+金额一行、时间在相邻一行（微信/支付宝导出格式）；
    3. 按列重排（日期列与金额列分离）。
    """
    today = today or date.today()
    lines = normalize_text(strip_comment_lines(text)).splitlines()
    year_match = YEAR_RE.search(text)
    default_year = int(year_match.group(1)) if year_match else today.year

    row_records = [record for record in (_parse_line(line, assume_expense) for line in lines) if record]
    paired_records = _parse_paired(lines, default_year, today, assume_expense)
    best = row_records if len(row_records) >= len(paired_records) else paired_records
    if len(best) >= 2:
        return best
    column_records = _column_fallback(lines, assume_expense)
    return column_records if len(column_records) > len(best) else best
