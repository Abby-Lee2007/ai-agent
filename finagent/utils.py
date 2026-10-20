"""通用解析与格式化工具。

规则解析器（NLU 的离线兜底）和工具层都要用到这里的能力，
所以单独抽出来，避免两个模块互相 import。
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Optional, Tuple

CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

PERIOD_LABELS = {
    "this_month": "本月",
    "last_month": "上月",
    "this_year": "今年",
    "last_year": "去年",
    "last_7_days": "最近 7 天",
    "last_30_days": "最近 30 天",
    "last_3_months": "最近 3 个月",
    "all": "全部",
}


# --------------------------------------------------------------------------
# 金额
# --------------------------------------------------------------------------
def parse_cn_number(text: str) -> Optional[float]:
    """把 "一千二百" 这类中文数字转成 1200（仅支持常见量级，够 Demo 用）。"""
    if not text:
        return None
    if any(ch.isdigit() for ch in text):
        return None
    units = {"万": 10_000, "千": 1_000, "百": 100, "十": 10}
    total = 0.0
    section = 0.0
    current = 0.0
    for ch in text:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
        elif ch in units:
            unit = units[ch]
            if unit == 10_000:
                section = (section + current) * unit
                total += section
                section = 0.0
            else:
                section += (current or 1) * unit
            current = 0.0
        elif ch in {"零", "元", "块", "钱", " "}:
            continue
        else:
            return None
    return total + section + current if (total or section or current) else None


def parse_amount(text: str) -> Optional[float]:
    """从自然语言里抽取金额。支持 500 / 1.5万 / 三千 / 一百二十块。

    抽取顺序很关键：先找带货币单位或数量级的写法，再找动词后面的数字，
    最后才退化为"第一个像金额的数字"，这样可以避开"下个月5号"里的 5。
    """
    if not text:
        return None
    cleaned = text.replace(",", "").replace("，", "")
    # 手机号/卡号不是金额，先摘掉
    cleaned = re.sub(r"1[3-9]\d{9}", " ", cleaned)
    multiplier_map = {"万": 10_000, "千": 1_000, "百": 100}

    # 1a) 带数量级：1.5万 / 3千
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)\s*(?:元|块)?", cleaned)
    if match:
        return float(match.group(1)) * multiplier_map[match.group(2)]

    # 1b) 带货币单位：500元 / 3800块
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块|人民币|rmb|RMB|¥)", cleaned)
    if match:
        return float(match.group(1))

    # 2) 动词/量词后面的数字：转3800 / 降到500 / 额度5万
    match = re.search(
        r"(?:转|付|给|打|汇|充|借|还|花|存|锁定|冻结|买|申购|赎回|预算|额度|提到|调到|降到|"
        r"设为|设成|改成|改到|到|是|了)\s*(?:了)?\s*(\d+(?:\.\d+)?)\s*(万|千)?",
        cleaned,
    )
    if match:
        value = float(match.group(1))
        return value * multiplier_map.get(match.group(2) or "", 1)

    # 3) 中文数字 + 元/块
    cn = re.search(r"([零一二两三四五六七八九十百千万]+)\s*(?:元|块|钱)", cleaned)
    if cn:
        return parse_cn_number(cn.group(1))

    # 4) 兜底：第一个后面不是日期/数量单位的数字
    for item in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|千)?", cleaned):
        tail = cleaned[item.end():item.end() + 1]
        if tail in {"号", "日", "月", "年", "点", "次", "个", "人", "笔", "天", "分", "秒",
                    "%", "折", ":", "：", "时"}:
            continue
        value = float(item.group(1))
        return value * multiplier_map.get(item.group(2) or "", 1)
    return None


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
def parse_date(text: str, today: date, base: Optional[date] = None) -> Optional[date]:
    """从自然语言里抽取日期。base 用于"生日前两天"这类相对表达。"""
    if not text:
        return None
    compact = text.replace(" ", "")

    # 相对天数
    offsets = {
        "今天": 0, "今日": 0, "当天": 0,
        "明天": 1, "明日": 1, "一天后": 1,
        "后天": 2, "两天后": 2,
        "大后天": 3, "三天后": 3,
    }
    for key, delta in offsets.items():
        if key in compact:
            return today + timedelta(days=delta)

    # 生日前 N 天
    m = re.search(r"(?:前|提前)([一二两三四五六七八九十\d]+)天", compact)
    if m and base:
        raw = m.group(1)
        delta = int(raw) if raw.isdigit() else (CN_DIGITS.get(raw, 2))
        return base - timedelta(days=delta)

    # 生日当天 / 纪念日
    if base and ("生日当天" in compact or "生日那天" in compact):
        return base

    # 明年的某月某日
    year_shift = 0
    if "明年" in compact:
        year_shift = 1
    elif "今年" in compact:
        year_shift = 0

    # 绝对日期：2026-11-20 / 2026年11月20日
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?", compact)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # 月日：11月20日 / 11-20
    m = re.search(r"(\d{1,2})[-/月](\d{1,2})[日号]?", compact)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year + year_shift
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if year_shift == 0 and candidate < today:
            candidate = date(year + 1, month, day)
        return candidate

    # 下个月 X 号 / 本月 X 号
    m = re.search(r"(下个?月|本月|这个月)(\d{1,2})[日号]", compact)
    if m:
        offset = 1 if "下" in m.group(1) else 0
        year, month = today.year, today.month + offset
        if month > 12:
            year, month = year + 1, month - 12
        day = int(m.group(2))
        day = min(day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    # X 号（默认最近的未来日期）
    m = re.search(r"(\d{1,2})[日号]", compact)
    if m:
        day = int(m.group(1))
        day = min(day, calendar.monthrange(today.year, today.month)[1])
        candidate = date(today.year, today.month, day)
        if candidate < today:
            year, month = today.year, today.month + 1
            if month > 12:
                year, month = year + 1, 1
            day = min(day, calendar.monthrange(year, month)[1])
            candidate = date(year, month, day)
        return candidate

    # 下周X / 周X
    weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    m = re.search(r"(下+)?(?:周|星期|礼拜)([一二三四五六日天])", compact)
    if m:
        target = weekday_map[m.group(2)]
        weeks = len(m.group(1) or "")
        delta = (target - today.weekday()) % 7
        delta = delta if delta else 7
        return today + timedelta(days=delta + 7 * weeks)

    if "月底" in compact:
        return date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    if "月初" in compact:
        year, month = today.year, today.month + 1
        if month > 12:
            year, month = year + 1, 1
        return date(year, month, 1)
    return None


def period_bounds(period: str, today: date) -> Tuple[str, str, str]:
    """把期间关键词转换成 (开始日期, 结束日期, 展示名称)。"""
    key = (period or "this_month").strip()
    if key in PERIOD_LABELS:
        label = PERIOD_LABELS[key]
    else:
        label = key

    if key == "this_month":
        start = today.replace(day=1)
        end = today
    elif key == "last_month":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "this_year":
        start = date(today.year, 1, 1)
        end = today
    elif key == "last_year":
        start = date(today.year - 1, 1, 1)
        end = date(today.year - 1, 12, 31)
    elif key == "last_7_days":
        start = today - timedelta(days=6)
        end = today
    elif key == "last_30_days":
        start = today - timedelta(days=29)
        end = today
    elif key == "last_3_months":
        month = today.month - 2
        year = today.year
        if month <= 0:
            month += 12
            year -= 1
        start = date(year, month, 1)
        end = today
    elif key == "all":
        start = date(2000, 1, 1)
        end = today
    elif re.fullmatch(r"\d{4}-\d{2}", key):
        year, month = int(key[:4]), int(key[5:7])
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        label = f"{year}年{month}月"
    elif re.fullmatch(r"\d{4}", key):
        year = int(key)
        start, end = date(year, 1, 1), date(year, 12, 31)
        label = f"{year}年"
    else:
        start = today.replace(day=1)
        end = today
    return start.isoformat(), end.isoformat(), label


def parse_period(text: str, today: date) -> str:
    """自然语言 -> 期间关键词。"""
    raw = (text or "").strip()
    # 已经是标准关键词或 YYYY-MM / YYYY 时直接透传（避免二次解析丢信息）
    if raw in PERIOD_LABELS or re.fullmatch(r"\d{4}(-\d{2})?", raw):
        return raw
    compact = raw.replace(" ", "")
    if re.search(r"去年|上一年", compact):
        return "last_year"
    if re.search(r"今年|本年度|年度|一整年|全年", compact):
        return "this_year"
    if re.search(r"近7天|最近7天|这周|本周|近一周|最近一周", compact):
        return "last_7_days"
    if re.search(r"近30天|最近30天|近一个月|最近一个月", compact):
        return "last_30_days"
    if re.search(r"近3个月|最近3个月|近三个月|最近三个月|近三个月", compact):
        return "last_3_months"
    m = re.search(r"(\d{4})年(\d{1,2})月", compact)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{4})[-/](\d{1,2})(?![-/]\d)", compact)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    if re.search(r"上个?月|上月", compact):
        return "last_month"
    if re.search(r"本月|这个月|当月|本月账单", compact):
        return "this_month"
    return "this_month"

