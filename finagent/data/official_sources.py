"""Official public macro-data adapters.

The project only reads public pages and APIs. It does not bypass logins,
CAPTCHAs, paywalls or anti-scraping controls. Live refreshes are explicit,
cached for a day, and always retain source URLs and publication timestamps.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

from config import OFFICIAL_DATA_FILE, OFFICIAL_SNAPSHOT_FILE, RUNTIME_DIR


CHINABOND_HOME = "https://www.chinabond.com.cn/"
PBOC_LIST = (
    "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/"
    "index.html"
)
WORLD_BANK_CPI = (
    "https://api.worldbank.org/v2/country/CHN/indicator/"
    "FP.CPI.TOTL.ZG?format=json&per_page=10"
)
SNAPSHOT_FILE = OFFICIAL_SNAPSHOT_FILE
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 3_000_000
ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = r"""{name}\s*=\s*(['"])(?P<value>.*?)\1"""


class OfficialDataError(RuntimeError):
    """Raised when no official source can be refreshed or loaded."""


def _ttl_hours() -> float:
    try:
        return max(1.0, float(os.getenv("FINAGENT_OFFICIAL_DATA_TTL_HOURS", "24")))
    except (TypeError, ValueError):
        return 24.0


def _timeout_seconds() -> float:
    try:
        return max(3.0, float(os.getenv("FINAGENT_OFFICIAL_DATA_TIMEOUT", "12")))
    except (TypeError, ValueError):
        return 12.0


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _strip_tags(value: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", value or "", flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_html_text(html: str) -> str:
    return _strip_tags(html)


def _snippet(text: str, marker: str, radius: int = 420) -> str:
    index = text.find(marker)
    if index < 0:
        return text[: min(len(text), radius * 2)]
    start = max(0, index - 80)
    return text[start: min(len(text), index + radius)]


def _fetch_text(url: str, timeout: Optional[float] = None) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=timeout or _timeout_seconds(),
    ) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("响应超过 3MB 安全上限")
        charset = response.headers.get_content_charset() or ""
    for encoding in [charset, "utf-8", "gb18030"]:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _attr(attrs: str, name: str) -> str:
    match = re.search(ATTR_RE.format(name=re.escape(name)), attrs, re.I | re.S)
    return unescape(match.group("value")) if match else ""


def _links(html: str) -> Iterable[Tuple[str, str, str]]:
    for match in ANCHOR_RE.finditer(html or ""):
        attrs = match.group("attrs")
        href = _attr(attrs, "href")
        title = _attr(attrs, "title")
        label = title or _strip_tags(match.group("body"))
        if href and label:
            yield href, label, attrs


def discover_chinabond_url(homepage_html: str) -> Tuple[str, str]:
    """Find the latest ChinaBond daily yield report from its public homepage."""
    for href, title, _ in _links(homepage_html):
        if "中债收益率曲线和指数日评" in title:
            return urljoin(CHINABOND_HOME, href), title
    raise ValueError("中国债券信息网首页未找到日评链接")


def discover_pboc_url(list_html: str) -> Tuple[str, str]:
    """Find the latest PBOC open-market operation announcement."""
    for href, title, _ in _links(list_html):
        if (
            re.search(r"公开市场业务交易公告\s*\[?\d{4}\]?\s*第\d+号", title)
            and re.search(r"/\d{10,24}/index\.html", href)
        ):
            return urljoin(PBOC_LIST, href), title
    raise ValueError("人民银行公告列表未找到公开市场交易公告")


def _number(text: str) -> Optional[float]:
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _first_percent(text: str, label: str, window: int = 48) -> Optional[float]:
    patterns = [
        rf"{re.escape(label)}[^。；]{{0,{window}}}?(?:至|为|在)\s*"
        rf"(\d+(?:\.\d+)?)\s*%",
        rf"{re.escape(label)}[^。；]{{0,{window}}}?(\d+(?:\.\d+)?)\s*%",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _number(match.group(1))
    return None


def parse_chinabond_article(html: str, url: str) -> Dict[str, Any]:
    """Parse the public ChinaBond daily report without external packages."""
    text = _clean_html_text(html)
    title_match = re.search(
        r"中债收益率曲线和指数日评\s*(\d{4})年(\d{1,2})月(\d{1,2})日",
        text,
    )
    published_at = ""
    if title_match:
        year, month, day = (int(title_match.group(i)) for i in range(1, 4))
        published_at = f"{year:04d}-{month:02d}-{day:02d}"
    data = {
        "government_bond_3m_yield_pct": _first_percent(text, "3M期限"),
        "government_bond_2y_yield_pct": _first_percent(text, "2年期"),
        "government_bond_10y_yield_pct": _first_percent(text, "10年期"),
        "aaa_mtn_3m_yield_pct": _first_percent(text, "AAA）3M期限", 72),
        "aaa_mtn_3y_yield_pct": _first_percent(text, "3年期收益率", 72),
        "aaa_mtn_5y_yield_pct": _first_percent(text, "5年期收益率", 72),
        "a_mtn_1y_yield_pct": _first_percent(text, "A）1年期", 72),
        "composite_net_price_index": None,
        "composite_net_price_change_pct": None,
        "composite_wealth_index": None,
        "composite_wealth_change_pct": None,
        "average_ytm_pct": None,
        "average_duration_years": None,
        "us_treasury_10y_yield_pct": _first_percent(text, "10年期", 96),
    }
    net = re.search(
        r"中债综合指数（净价）\s*([\d,.]+)\s*[，,]\s*(上涨|下跌)\s*([\d.]+)%",
        text,
    )
    if net:
        value = _number(net.group(1))
        change = _number(net.group(3))
        data["composite_net_price_index"] = value
        data["composite_net_price_change_pct"] = (
            change if change is not None and net.group(2) == "上涨"
            else -change if change is not None else None
        )
    wealth = re.search(
        r"中债综合指数（财富）\s*([\d,.]+)\s*[，,]\s*(上涨|下跌)\s*([\d.]+)%",
        text,
    )
    if wealth:
        value = _number(wealth.group(1))
        change = _number(wealth.group(3))
        data["composite_wealth_index"] = value
        data["composite_wealth_change_pct"] = (
            change if change is not None and wealth.group(2) == "上涨"
            else -change if change is not None else None
        )
    average = re.search(r"平均市值法到期收益率为\s*([\d.]+)%", text)
    if average:
        data["average_ytm_pct"] = _number(average.group(1))
    duration = re.search(r"平均市值法久期为\s*([\d.]+)", text)
    if duration:
        data["average_duration_years"] = _number(duration.group(1))

    # The article also contains a US 10Y yield; keep the China 10Y value
    # unambiguous by replacing it with the section-local match.
    overseas = re.search(r"【海外市场】(.*?)(?:【注】|$)", text)
    if overseas:
        overseas_yield = _first_percent(overseas.group(1), "10年期")
        if overseas_yield is not None:
            data["us_treasury_10y_yield_pct"] = overseas_yield

    parseable = [value for value in data.values() if isinstance(value, (int, float))]
    confidence = round(min(0.99, 0.55 + 0.055 * len(parseable)), 2)
    return {
        "id": "chinabond",
        "name": "中国债券信息网",
        "title": (
            title_match.group(0) if title_match
            else "中债收益率曲线和指数日评"
        ),
        "url": url,
        "published_at": published_at,
        "fetched_at": _now_iso(),
        "status": "ok",
        "data": data,
        "parse_confidence": confidence,
        "snippets": [_snippet(text, "【中债指数走势】")],
        "error": "",
    }


def parse_pboc_announcement(html: str, url: str) -> Dict[str, Any]:
    """Parse the PBOC public open-market announcement."""
    text = _clean_html_text(html)
    title_match = re.search(
        r"(公开市场业务交易公告\s*\[?\d{4}\]?\s*第\d+号)",
        text,
    )
    date_match = re.search(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日中国人民银行",
        text,
    )
    published_at = ""
    if date_match:
        published_at = (
            f"{int(date_match.group(1)):04d}-"
            f"{int(date_match.group(2)):02d}-"
            f"{int(date_match.group(3)):02d}"
        )
    reverse_repo = re.search(
        r"开展了?\s*([\d,.]+)\s*亿元\s*7\s*天期逆回购操作",
        text,
    )
    overnight = re.search(
        r"开展了?\s*([\d,.]+)\s*亿元隔夜逆回购操作",
        text,
    )
    rate = re.search(
        r"7\s*天\s*(\d)\s*[.]\s*(\d{1,2})\s*%\s*"
        r"([\d,.]+)\s*亿元\s*([\d,.]+)\s*亿元",
        text,
    )
    rate_value = None
    bid_amount = None
    awarded_amount = None
    if rate:
        rate_value = _number(f"{rate.group(1)}.{rate.group(2)}")
        bid_amount = _number(rate.group(3))
        awarded_amount = _number(rate.group(4))
    data = {
        "reverse_repo_7d_amount_100m_cny": _number(reverse_repo.group(1)) if reverse_repo else None,
        "reverse_repo_7d_rate_pct": rate_value,
        "reverse_repo_7d_bid_amount_100m_cny": bid_amount,
        "reverse_repo_7d_awarded_amount_100m_cny": awarded_amount,
        "reverse_repo_overnight_amount_100m_cny": _number(overnight.group(1)) if overnight else None,
    }
    parseable = [value for value in data.values() if isinstance(value, (int, float))]
    return {
        "id": "pboc",
        "name": "中国人民银行",
        "title": title_match.group(1) if title_match else "公开市场业务交易公告",
        "url": url,
        "published_at": published_at,
        "fetched_at": _now_iso(),
        "status": "ok" if len(parseable) >= 2 else "partial",
        "data": data,
        "parse_confidence": round(min(0.99, 0.55 + 0.075 * len(parseable)), 2),
        "snippets": [
            _snippet(
                text,
                date_match.group(0) if date_match else "中国人民银行",
            )
        ],
        "error": "",
    }


def parse_world_bank_cpi(raw: str) -> Dict[str, Any]:
    """Parse World Bank China CPI (annual percentage) from JSON."""
    payload = json.loads(raw)
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("世界银行返回格式异常")
    rows = [
        row for row in payload[1] or []
        if isinstance(row, dict) and row.get("value") is not None
    ]
    if not rows:
        raise ValueError("世界银行未返回中国 CPI 数值")
    row = sorted(rows, key=lambda item: str(item.get("date", "")), reverse=True)[0]
    return {
        "id": "world_bank",
        "name": "世界银行",
        "title": "中国：按消费者价格指数衡量的通货膨胀（年同比）",
        "url": WORLD_BANK_CPI,
        "published_at": str(row.get("date") or ""),
        "fetched_at": _now_iso(),
        "status": "ok",
        "data": {
            "cpi_yoy_pct": round(float(row["value"]), 6),
            "cpi_period_year": int(row.get("date") or 0) or None,
            "country": (
                (row.get("country") or {}).get("value")
                if isinstance(row.get("country"), dict) else "中国"
            ),
            "indicator": (
                (row.get("indicator") or {}).get("value")
                if isinstance(row.get("indicator"), dict) else ""
            ),
        },
        "parse_confidence": 0.99,
        "snippets": [
            f"{row.get('date')} 年同比 {float(row['value']):.6f}%"
        ],
        "error": "",
    }


def _fetch_chinabond() -> Dict[str, Any]:
    homepage = _fetch_text(CHINABOND_HOME)
    url, title = discover_chinabond_url(homepage)
    article = _fetch_text(url)
    parsed = parse_chinabond_article(article, url)
    parsed["homepage_url"] = CHINABOND_HOME
    parsed["discovered_title"] = title
    return parsed


def _fetch_pboc() -> Dict[str, Any]:
    listing = _fetch_text(PBOC_LIST)
    url, title = discover_pboc_url(listing)
    article = _fetch_text(url)
    parsed = parse_pboc_announcement(article, url)
    parsed["listing_url"] = PBOC_LIST
    parsed["discovered_title"] = title
    return parsed


def _fetch_world_bank() -> Dict[str, Any]:
    return parse_world_bank_cpi(_fetch_text(WORLD_BANK_CPI))


def _indicator_map(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    indicators: Dict[str, Any] = {}
    for source in sources:
        for key, value in (source.get("data") or {}).items():
            if value is not None and key not in indicators:
                indicators[key] = value
    return indicators


def _freshness(payload: Dict[str, Any], delivery: str) -> Dict[str, Any]:
    fetched_at = str(payload.get("fetched_at") or "")
    age_hours: Optional[float] = None
    try:
        stamp = datetime.fromisoformat(fetched_at)
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        age_hours = round(
            max(0.0, (datetime.now().astimezone() - stamp).total_seconds() / 3600),
            1,
        )
    except ValueError:
        pass
    if delivery == "bundled_snapshot":
        label = "随项目发布的官方数据快照"
        state = "snapshot"
    elif age_hours is not None and age_hours <= _ttl_hours():
        label = "官方公开数据缓存，仍在有效期内"
        state = "fresh"
    else:
        label = "官方公开数据缓存，已超过时效"
        state = "stale"
    return {
        "state": state,
        "label": label,
        "age_hours": age_hours,
        "ttl_hours": _ttl_hours(),
        "live_network_call_this_request": False,
    }


def _finish_payload(
    sources: List[Dict[str, Any]],
    *,
    delivery: str,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    usable_sources = [
        source for source in sources
        if source.get("status") in {"ok", "partial"}
    ]
    success_sources = [
        source for source in sources
        if source.get("status") == "ok"
    ]
    source_times = [
        str(source.get("published_at") or "")
        for source in success_sources
        if source.get("published_at")
    ]
    latest = sorted(source_times)[-1] if source_times else ""
    if len(latest) == 4:
        latest = f"{latest}-12-31"
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "status": "ok" if len(success_sources) == len(sources) else "partial",
        "delivery": delivery,
        "data_class": "official_public_data",
        "is_simulated": False,
        "fetched_at": _now_iso(),
        "as_of": latest,
        "sources": sources,
        "indicators": _indicator_map(sources),
        "warnings": list(warnings or []),
    }
    payload["freshness"] = _freshness(payload, delivery)
    if delivery == "refresh":
        payload["status"] = "live" if payload["status"] == "ok" else "partial"
        payload["freshness"]["live_network_call_this_request"] = True
    return payload


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _source_failure(source_id: str, name: str, error: Exception) -> Dict[str, Any]:
    return {
        "id": source_id,
        "name": name,
        "title": "",
        "url": "",
        "published_at": "",
        "fetched_at": _now_iso(),
        "status": "error",
        "data": {},
        "parse_confidence": 0.0,
        "snippets": [],
        "error": f"{type(error).__name__}: {error}",
    }


def refresh_official_data(
    *,
    cache_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fetch each public source once and persist a mergeable cache."""
    cache_path = Path(cache_file or OFFICIAL_DATA_FILE)
    previous = _read_json(cache_path) or {}
    previous_sources = {
        str(source.get("id")): source
        for source in previous.get("sources") or []
        if isinstance(source, dict) and source.get("id")
    }
    specifications = [
        ("chinabond", "中国债券信息网", _fetch_chinabond),
        ("pboc", "中国人民银行", _fetch_pboc),
        ("world_bank", "世界银行", _fetch_world_bank),
    ]
    sources: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []
    for source_id, name, fetcher in specifications:
        try:
            source = fetcher()
            if source.get("status") not in {"ok", "partial"}:
                raise ValueError("核心字段解析不足")
            source["source_mode"] = "public_http"
            sources.append(source)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            failure = _source_failure(source_id, name, exc)
            errors.append(f"{name}：{failure['error']}")
            old = previous_sources.get(source_id)
            if old:
                fallback = dict(old)
                fallback["status"] = "stale_cache"
                fallback["last_attempt_at"] = _now_iso()
                fallback["last_attempt_error"] = failure["error"]
                sources.append(fallback)
                warnings.append(f"{name}刷新失败，继续使用上次成功数据。")
            else:
                sources.append(failure)

    payload = _finish_payload(
        sources,
        delivery="refresh",
        warnings=warnings + (
            ["世界银行 CPI 为年度指标，不代表当月通胀。"]
            if any(source.get("id") == "world_bank" and source.get("status") == "ok"
                   for source in sources)
            else []
        ),
    )
    if errors:
        payload["refresh_errors"] = errors
    if not any(
        source.get("status") in {"ok", "partial", "stale_cache"}
        for source in sources
    ):
        raise OfficialDataError("所有官方数据源均刷新失败")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(cache_path, payload)
    return payload


def load_official_data(
    *,
    refresh: bool = False,
    cache_file: Optional[Path] = None,
    snapshot_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load live cache, then bundled snapshot. Never substitute demo values."""
    cache_path = Path(cache_file or OFFICIAL_DATA_FILE)
    if refresh:
        return refresh_official_data(cache_file=cache_path)
    cached = _read_json(cache_path)
    if cached:
        payload = dict(cached)
        payload["delivery"] = "runtime_cache"
        payload["freshness"] = _freshness(payload, "runtime_cache")
        return payload
    bundled = _read_json(Path(snapshot_file or SNAPSHOT_FILE))
    if bundled:
        payload = dict(bundled)
        payload["delivery"] = "bundled_snapshot"
        payload["original_status"] = payload.get("status")
        payload["status"] = "snapshot"
        payload["freshness"] = _freshness(payload, "bundled_snapshot")
        payload["warning"] = (
            "当前展示项目随附的官方数据快照，尚未成功读取本地实时缓存；"
            "日期和来源均已标明。"
        )
        return payload
    raise OfficialDataError("没有可用的官方数据缓存或随附快照")


def official_macro_context(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a compact, model-friendly subset for forecasts and advice."""
    data = payload or load_official_data()
    indicators = dict(data.get("indicators") or {})
    if not indicators and data.get("data_class") == "official_public_data":
        indicators = dict(data)
    return {
        "data_class": data.get("data_class", "official_public_data"),
        "delivery": data.get("delivery", ""),
        "status": data.get("status", ""),
        "fetched_at": data.get("fetched_at", ""),
        "as_of": data.get("as_of", ""),
        "freshness": dict(data.get("freshness") or {}),
        "reverse_repo_7d_rate_pct": indicators.get("reverse_repo_7d_rate_pct"),
        "reverse_repo_7d_amount_100m_cny": indicators.get(
            "reverse_repo_7d_amount_100m_cny"
        ),
        "government_bond_10y_yield_pct": indicators.get(
            "government_bond_10y_yield_pct"
        ),
        "government_bond_2y_yield_pct": indicators.get(
            "government_bond_2y_yield_pct"
        ),
        "cpi_yoy_pct": indicators.get("cpi_yoy_pct"),
        "cpi_period_year": indicators.get("cpi_period_year"),
        "sources": [
            {
                "name": source.get("name"),
                "title": source.get("title"),
                "url": source.get("url"),
                "published_at": source.get("published_at"),
                "status": source.get("status"),
            }
            for source in data.get("sources") or []
        ],
        "warnings": list(data.get("warnings") or []),
    }


def official_macro_answer_line(payload: Optional[Dict[str, Any]] = None) -> str:
    """Build one attributable macro-context line for generated answers."""
    macro = official_macro_context(payload)
    if macro.get("status") in {"", "unavailable", None}:
        return ""
    bits: List[str] = []
    if macro.get("reverse_repo_7d_rate_pct") is not None:
        bits.append(
            f"7天逆回购利率 {float(macro['reverse_repo_7d_rate_pct']):.2f}%"
        )
    if macro.get("government_bond_10y_yield_pct") is not None:
        bits.append(
            f"10年期国债收益率 "
            f"{float(macro['government_bond_10y_yield_pct']):.2f}%"
        )
    if macro.get("cpi_yoy_pct") is not None:
        period = macro.get("cpi_period_year") or "最新年度"
        bits.append(
            f"{period}年CPI同比 {float(macro['cpi_yoy_pct']):.2f}%"
        )
    if not bits:
        return ""
    delivery = {
        "refresh": "本次联网刷新",
        "runtime_cache": "官方缓存",
        "bundled_snapshot": "官方快照",
    }.get(str(macro.get("delivery")), "官方公开数据")
    source_names = [
        str(source.get("name"))
        for source in macro.get("sources") or []
        if source.get("name")
    ]
    source_text = f"；来源：{'、'.join(source_names)}" if source_names else ""
    return (
        f"官方数据参照（截至 {macro.get('as_of') or '-'}，{delivery}）："
        f"{'；'.join(bits)}{source_text}。"
    )
