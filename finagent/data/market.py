"""实时行情数据源（真实数据）。

数据来源都是公开、免 Key 的接口：
- 股票 / ETF 实时行情：腾讯财经 `qt.gtimg.cn`
- 场外基金净值：东方财富 `api.fund.eastmoney.com/f10/lsjz`

设计原则：
1. **诚实**：拿不到就明确说"实时行情不可用"，绝不拿模拟数字冒充真实数据；
2. **可降级**：主源失败时尝试东方财富备用源，再失败才进入冷却期；
3. **可复现**：结果缓存 60 秒，避免一次对话里反复打接口；
4. **可观测**：来源、更新时间、缓存命中、失败次数和过期状态都可查询。

注意：这里提供的是**当前价格与当日涨跌**，不是"预期收益"。
历史涨跌不代表未来收益，界面文案里必须写清楚。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional

from finagent.data.provenance import is_stale

QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
FALLBACK_QUOTE_URL = (
    "https://push2.eastmoney.com/api/qt/stock/get"
    "?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f167,f168,f169,f170"
)
FUND_URL = ("https://api.fund.eastmoney.com/f10/lsjz"
            "?fundCode={code}&pageIndex=1&pageSize={page_size}")
FUND_FALLBACK_URL = "https://fundgz.1234567.com.cn/js/{code}.js?rt={stamp}"
FUND_HISTORY_URL = (
    "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNHisNetList"
    "?FCODE={code}&pageIndex=1&pageSize={limit}"
    "&deviceid=finagent&plat=Iphone&product=EFund&version=6.3.8"
)
HISTORY_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={symbol},day,,,{limit},qfq"
)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}


def _env_positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass
class Quote:
    """一只股票 / ETF 的实时快照。"""

    symbol: str
    name: str
    price: float
    prev_close: float
    change: float
    change_pct: float
    open_price: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pe: Optional[float] = None
    pb: Optional[float] = None
    total_cap: Optional[float] = None      # 亿元
    timestamp: str = ""
    source: str = "腾讯财经"
    live: bool = True
    updated_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    is_demo: bool = False
    freshness_seconds: int = 60
    turnover_100m_cny: Optional[float] = None

    @property
    def is_live(self) -> bool:
        return self.live

    @property
    def stale(self) -> bool:
        return is_stale(self.updated_at, self.freshness_seconds)


@dataclass
class FundNav:
    """一只场外基金的最新净值。"""

    code: str
    nav: float
    change_pct: Optional[float]
    date: str
    source: str = "东方财富"
    live: bool = True
    updated_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    is_demo: bool = False
    freshness_seconds: int = 86400

    @property
    def is_live(self) -> bool:
        return self.live

    @property
    def stale(self) -> bool:
        return is_stale(self.updated_at, self.freshness_seconds)


def _to_float(value: str) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class MarketDataProvider:
    """行情提供者（带缓存与失败冷却）。"""

    def __init__(
        self,
        mode: Optional[str] = None,
        timeout: float = 5.0,
        ttl: float = 60.0,
        cooldown: float = 120.0,
        max_retries: int = 1,
        min_interval: float = 0.2,
    ) -> None:
        self.mode = (mode or os.getenv("FINAGENT_MARKET_MODE", "live")).strip().lower()
        self.timeout = timeout
        self.ttl = ttl
        self.cooldown = cooldown
        self.max_retries = max(0, int(max_retries))
        self.min_interval = max(0.0, float(min_interval))
        self.quote_freshness_seconds = _env_positive_int(
            "FINAGENT_QUOTE_FRESHNESS_SECONDS", 60
        )
        self.fund_freshness_seconds = _env_positive_int(
            "FINAGENT_FUND_FRESHNESS_SECONDS", 86400
        )
        self._cache: Dict[str, tuple] = {}
        self._cooldown_until = 0.0
        self._last_attempt_at = 0.0
        self.last_success_at = ""
        self.last_error = ""
        self.last_source = ""
        self.calls = 0
        self.failures = 0
        self.cache_hits = 0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    # ------------------------------------------------------------------
    def _cached(self, key: str):
        item = self._cache.get(key)
        if item and item[0] > time.time():
            self.cache_hits += 1
            return item[1]
        return None

    def _store(self, key: str, value) -> None:
        self._cache[key] = (time.time() + self.ttl, value)

    def _fetch(
        self,
        url: str,
        encoding: str = "gbk",
        extra_headers: Optional[Dict[str, str]] = None,
        bypass_cooldown: bool = False,
    ) -> Optional[str]:
        if not self.enabled:
            return None
        if not bypass_cooldown and time.time() < self._cooldown_until:
            return None
        headers = dict(HEADERS)
        if extra_headers:
            headers.update(extra_headers)
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.time() - self._last_attempt_at)
            if wait > 0:
                time.sleep(wait)
            self._last_attempt_at = time.time()
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.calls += 1
                    self.last_success_at = datetime.now().isoformat(timespec="seconds")
                    return response.read().decode(encoding, errors="replace")
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.failures += 1
                if attempt >= self.max_retries:
                    self._cooldown_until = time.time() + self.cooldown
                    return None
        return None

    # ------------------------------------------------------------------
    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """批量取股票/ETF 实时行情。键为规范化后的 symbol。"""
        wanted = [s.strip() for s in symbols if s and s.strip()]
        if not wanted:
            return {}
        result: Dict[str, Quote] = {}
        missing = []
        for symbol in wanted:
            cached = self._cached(f"q:{symbol}")
            if cached is not None:
                result[symbol] = cached
            else:
                missing.append(symbol)
        if not missing:
            return result
        raw = self._fetch(QUOTE_URL.format(symbols=",".join(missing)))
        if raw:
            self.last_source = "腾讯财经"
            result.update(self._parse_tencent_quotes(raw))
        missing = [symbol for symbol in wanted if symbol not in result]
        if missing:
            result.update(self._fetch_fallback_quotes(missing))
        return result

    def _parse_tencent_quotes(self, raw: str) -> Dict[str, Quote]:
        result: Dict[str, Quote] = {}
        for chunk in raw.split(";"):
            if "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            key = key.strip()
            parts = value.strip().strip('"').split("~")
            if len(parts) < 33 or not key.startswith("v_"):
                continue
            symbol = key[2:]
            price = _to_float(parts[3])
            if price is None or price <= 0:
                continue
            quote = Quote(
                symbol=symbol,
                name=parts[1].strip(),
                price=price,
                prev_close=_to_float(parts[4]) or price,
                change=_to_float(parts[31]) or 0.0,
                change_pct=_to_float(parts[32]) or 0.0,
                open_price=_to_float(parts[5]),
                high=_to_float(parts[33]),
                low=_to_float(parts[34]),
                pe=_to_float(parts[39]) if len(parts) > 39 else None,
                pb=_to_float(parts[46]) if len(parts) > 46 else None,
                total_cap=_to_float(parts[45]) if len(parts) > 45 else None,
                timestamp=self._format_stamp(parts[30]),
                updated_at=datetime.now().isoformat(timespec="seconds"),
                freshness_seconds=self.quote_freshness_seconds,
                turnover_100m_cny=(
                    round((_to_float(parts[37]) or 0.0) / 10_000, 2)
                    if len(parts) > 37 else None
                ),
            )
            result[symbol] = quote
            self._store(f"q:{symbol}", quote)
        return result

    def _fetch_fallback_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        result: Dict[str, Quote] = {}
        for symbol in symbols:
            secid = self._eastmoney_secid(symbol)
            if not secid:
                continue
            raw = self._fetch(
                FALLBACK_QUOTE_URL.format(secid=secid),
                encoding="utf-8",
                bypass_cooldown=True,
            )
            quote = self._parse_eastmoney_quote(symbol, raw or "")
            if quote is not None:
                result[symbol] = quote
                self._store(f"q:{symbol}", quote)
                self.last_source = "东方财富"
        return result

    @staticmethod
    def _eastmoney_secid(symbol: str) -> str:
        text = (symbol or "").strip().lower()
        if text.startswith("sh") and len(text) == 8:
            return f"1.{text[2:]}"
        if text.startswith("sz") and len(text) == 8:
            return f"0.{text[2:]}"
        return ""

    def _parse_eastmoney_quote(self, symbol: str, raw: str) -> Optional[Quote]:
        try:
            payload = json.loads(raw)
            data = payload.get("data") or {}
            scale = int(data.get("f59", 2) or 2)
            divisor = 10 ** scale
            price = MarketDataProvider._scaled_number(data.get("f43"), divisor)
            prev_close = MarketDataProvider._scaled_number(data.get("f60"), divisor)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if price is None or price <= 0 or prev_close is None:
            return None
        open_price = MarketDataProvider._scaled_number(data.get("f46"), divisor)
        high = MarketDataProvider._scaled_number(data.get("f44"), divisor)
        low = MarketDataProvider._scaled_number(data.get("f45"), divisor)
        return Quote(
            symbol=(symbol or "").lower(),
            name=str(data.get("f58") or symbol or "").strip(),
            price=price,
            prev_close=prev_close,
            change=MarketDataProvider._scaled_number(data.get("f169"), divisor) or 0.0,
            change_pct=_to_float(data.get("f170")) or 0.0,
            open_price=open_price,
            high=high,
            low=low,
            pe=_to_float(data.get("f168")),
            pb=_to_float(data.get("f167")),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            source="东方财富",
            updated_at=datetime.now().isoformat(timespec="seconds"),
            freshness_seconds=self.quote_freshness_seconds,
            turnover_100m_cny=(
                round((_to_float(data.get("f48")) or 0.0) / 100_000_000, 2)
                if data.get("f48") is not None else None
            ),
        )

    @staticmethod
    def _scaled_number(value: object, divisor: int) -> Optional[float]:
        number = _to_float(value)
        if number is None:
            return None
        if isinstance(value, float) and not value.is_integer():
            return number
        if isinstance(value, str) and "." in value:
            return number
        return number / divisor

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes([symbol]).get(symbol)

    # ------------------------------------------------------------------
    def history(self, symbol: str, limit: int = 250) -> Optional[Dict[str, object]]:
        """Fetch adjusted daily OHLC history for stocks, ETFs and bonds."""
        normalized = str(symbol or "").strip().lower()
        if not normalized or not self.enabled:
            return None
        limit = max(30, min(int(limit or 250), 500))
        cache_key = f"h:{normalized}:{limit}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        raw = self._fetch(
            HISTORY_URL.format(symbol=normalized, limit=limit),
            encoding="utf-8",
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            data = (payload.get("data") or {}).get(normalized) or {}
            rows = data.get("qfqday") or data.get("day") or []
            points = []
            previous_close: Optional[float] = None
            for row in rows:
                parts = row if isinstance(row, list) else str(row).split(",")
                if len(parts) < 6:
                    continue
                close = _to_float(parts[2])
                if close is None or close <= 0:
                    continue
                change_pct = (
                    (close / previous_close - 1) * 100
                    if previous_close and previous_close > 0 else None
                )
                points.append({
                    "date": str(parts[0]),
                    "open": _to_float(parts[1]),
                    "close": close,
                    "high": _to_float(parts[3]),
                    "low": _to_float(parts[4]),
                    "volume": _to_float(parts[5]),
                    "amount": _to_float(parts[6]) if len(parts) > 6 else None,
                    "change_pct": round(change_pct, 4) if change_pct is not None else None,
                })
                previous_close = close
            if len(points) < 10:
                return None
            quote_row = (data.get("qt") or {}).get(normalized) or []
            result = {
                "symbol": normalized.upper(),
                "name": (
                    str(quote_row[1]) if len(quote_row) > 1
                    else normalized.upper()
                ),
                "source": "腾讯财经",
                "points": points,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "is_demo": False,
            }
            self._store(cache_key, result)
            return result
        except (json.JSONDecodeError, TypeError, ValueError):
            self.last_error = "历史行情接口返回格式异常"
            return None

    # ------------------------------------------------------------------
    def fund_nav(self, code: str) -> Optional[FundNav]:
        """取场外基金最新单位净值与当日涨跌。"""
        code = (code or "").strip()
        if not code:
            return None
        cached = self._cached(f"f:{code}")
        if cached is not None:
            return cached
        raw = self._fetch(
            FUND_URL.format(code=code, page_size=1),
            encoding="utf-8",
            extra_headers={"Referer": "https://fundf10.eastmoney.com/"},
        )
        if not raw:
            raw = self._fetch(
                FUND_FALLBACK_URL.format(code=code, stamp=int(time.time() * 1000)),
                encoding="utf-8",
                extra_headers={"Referer": "https://fund.eastmoney.com/"},
                bypass_cooldown=True,
            )
            fallback_source = "天天基金"
        else:
            fallback_source = ""
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            rows = (payload.get("Data") or {}).get("LSJZList") or []
            if not rows:
                match = re.search(r"jsonpgz\((.*)\)\s*;?", raw.strip(), re.S)
                if not match:
                    return None
                payload = json.loads(match.group(1))
                nav = _to_float(payload.get("dwjz"))
                if nav is None:
                    return None
                result = FundNav(
                    code=code,
                    nav=nav,
                    change_pct=_to_float(payload.get("gszzl")),
                    date=str(payload.get("jzrq") or ""),
                    source=fallback_source or "天天基金",
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                    freshness_seconds=self.fund_freshness_seconds,
                )
                self.last_source = result.source
                self._store(f"f:{code}", result)
                return result
            row = rows[0]
            nav = _to_float(row.get("DWJZ", ""))
            if nav is None:
                return None
            result = FundNav(
                code=code,
                nav=nav,
                change_pct=_to_float(row.get("JZZZL", "")),
                date=str(row.get("FSRQ", "")),
                source="东方财富",
                updated_at=datetime.now().isoformat(timespec="seconds"),
                freshness_seconds=self.fund_freshness_seconds,
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            self.last_error = "基金净值接口返回格式异常"
            return None
        self.last_source = result.source
        self._store(f"f:{code}", result)
        return result

    def fund_nav_history(
        self,
        code: str,
        limit: int = 250,
    ) -> Optional[Dict[str, object]]:
        """Fetch recent disclosed NAV history for an off-exchange fund."""
        code = str(code or "").strip()
        if not code or not self.enabled:
            return None
        limit = max(20, min(int(limit or 250), 500))
        cache_key = f"fh:{code}:{limit}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        raw = self._fetch(
            FUND_HISTORY_URL.format(code=code, limit=limit),
            encoding="utf-8",
            extra_headers={"User-Agent": "Mozilla/5.0"},
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            rows = (
                payload.get("Datas")
                or (payload.get("Data") or {}).get("LSJZList")
                or []
            )
            points = []
            for row in rows:
                nav = _to_float(row.get("DWJZ"))
                if nav is None or nav <= 0:
                    continue
                points.append({
                    "date": str(row.get("FSRQ") or ""),
                    "close": nav,
                    "adjusted_close": _to_float(row.get("LJJZ")) or nav,
                    "change_pct": _to_float(row.get("JZZZL")),
                })
            points.sort(key=lambda item: item["date"])
            if len(points) < 10:
                return None
            result = {
                "symbol": code,
                "name": code,
                "source": "东方财富",
                "points": points,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "is_demo": False,
            }
            self._store(cache_key, result)
            return result
        except (json.JSONDecodeError, TypeError, ValueError):
            self.last_error = "基金历史净值接口返回格式异常"
            return None

    # ------------------------------------------------------------------
    @staticmethod
    def _format_stamp(raw: str) -> str:
        text = (raw or "").strip()
        if len(text) >= 14 and text.isdigit():
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}:{text[12:14]}"
        return text

    def status(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "role": "public-data-enhancement",
            "production_grade": False,
            "cooling_down": time.time() < self._cooldown_until,
            "cooldown_seconds": max(0, round(self._cooldown_until - time.time())),
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_source": self.last_source,
            "source_order": ["腾讯财经", "东方财富"],
            "fund_source_order": ["东方财富", "天天基金"],
            "quote_freshness_seconds": self.quote_freshness_seconds,
            "fund_freshness_seconds": self.fund_freshness_seconds,
            "calls": self.calls,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "cache_entries": len(self._cache),
            "stale_cache_entries": sum(
                1 for _, value in self._cache.values()
                if bool(getattr(value, "stale", False))
            ),
        }


MARKET_OVERVIEW_INDICES = (
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sh000300", "沪深300"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("bj899050", "北证50"),
)


def _a_share_market_state(now: Optional[datetime] = None) -> str:
    current = now or datetime.now()
    if current.weekday() >= 5:
        return "休市"
    clock = current.time()
    if clock < dt_time(9, 15):
        return "未开盘"
    if clock < dt_time(9, 30):
        return "集合竞价"
    if clock <= dt_time(11, 30):
        return "交易中"
    if clock < dt_time(13, 0):
        return "午间休市"
    if clock <= dt_time(15, 0):
        return "交易中"
    return "已收盘"


def build_market_overview(
    market: MarketDataProvider,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Return a compact, source-attributed A-share market dashboard."""
    symbols = [symbol for symbol, _ in MARKET_OVERVIEW_INDICES]
    quotes = market.quotes(symbols) if market.enabled else {}
    indices = []
    for symbol, fallback_name in MARKET_OVERVIEW_INDICES:
        quote = quotes.get(symbol)
        if quote is None:
            indices.append({
                "symbol": symbol.upper(),
                "name": fallback_name,
                "available": False,
            })
            continue
        indices.append({
            "symbol": symbol.upper(),
            "name": quote.name or fallback_name,
            "price": round(quote.price, 3),
            "change": round(quote.change, 3),
            "change_pct": round(quote.change_pct, 3),
            "timestamp": quote.timestamp,
            "source": quote.source,
            "available": True,
        })
    available = [item for item in indices if item.get("available")]
    turnover = round(sum(
        quote.turnover_100m_cny or 0.0
        for symbol, quote in quotes.items()
        if symbol in {"sh000001", "sz399001"}
    ), 2)
    observed_times = [str(item.get("timestamp") or "") for item in available]
    leading = max(available, key=lambda item: item["change_pct"]) if available else None
    lagging = min(available, key=lambda item: item["change_pct"]) if available else None
    sources = sorted({str(item.get("source") or "") for item in available if item.get("source")})
    return {
        "available": bool(available),
        "status": "live" if len(available) == len(indices) else ("partial" if available else "unavailable"),
        "market_state": _a_share_market_state(now),
        "observed_at": max(observed_times) if observed_times else "",
        "indices": indices,
        "turnover_100m_cny": turnover if turnover > 0 else None,
        "leading_index": leading,
        "lagging_index": lagging,
        "sources": sources,
        "source_mode": "public_market_data",
        "is_simulated": False,
        "production_grade": False,
        "data_note": (
            "指数为公开行情快照；收盘后显示最近交易时点价格。"
            "成交额仅汇总上证指数与深证成指行情字段，单位为亿元。"
        ),
    }
