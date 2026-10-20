"""实时行情数据源（真实数据）。

数据来源都是公开、免 Key 的接口：
- 股票 / ETF 实时行情：腾讯财经 `qt.gtimg.cn`
- 场外基金净值：东方财富 `api.fund.eastmoney.com/f10/lsjz`

设计原则：
1. **诚实**：拿不到就明确说"实时行情不可用"，绝不拿模拟数字冒充真实数据；
2. **可降级**：断网、被墙、接口改版时自动进入冷却期，不影响其它业务；
3. **可复现**：结果缓存 60 秒，避免一次对话里反复打接口。

注意：这里提供的是**当前价格与当日涨跌**，不是"预期收益"。
历史涨跌不代表未来收益，界面文案里必须写清楚。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
FUND_URL = ("https://api.fund.eastmoney.com/f10/lsjz"
            "?fundCode={code}&pageIndex=1&pageSize=1")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
}


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


@dataclass
class FundNav:
    """一只场外基金的最新净值。"""

    code: str
    nav: float
    change_pct: Optional[float]
    date: str
    source: str = "东方财富"
    live: bool = True


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value.strip())
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
    ) -> None:
        self.mode = (mode or os.getenv("FINAGENT_MARKET_MODE", "live")).strip().lower()
        self.timeout = timeout
        self.ttl = ttl
        self.cooldown = cooldown
        self._cache: Dict[str, tuple] = {}
        self._cooldown_until = 0.0
        self.last_error = ""
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    # ------------------------------------------------------------------
    def _cached(self, key: str):
        item = self._cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        return None

    def _store(self, key: str, value) -> None:
        self._cache[key] = (time.time() + self.ttl, value)

    def _fetch(self, url: str, encoding: str = "gbk",
               extra_headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        if not self.enabled:
            return None
        if time.time() < self._cooldown_until:
            return None
        try:
            headers = dict(HEADERS)
            if extra_headers:
                headers.update(extra_headers)
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.calls += 1
                return response.read().decode(encoding, errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._cooldown_until = time.time() + self.cooldown
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
        if not raw:
            return result
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
            )
            result[symbol] = quote
            self._store(f"q:{symbol}", quote)
        return result

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes([symbol]).get(symbol)

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
            FUND_URL.format(code=code),
            encoding="utf-8",
            extra_headers={"Referer": "https://fundf10.eastmoney.com/"},
        )
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            rows = (payload.get("Data") or {}).get("LSJZList") or []
            if not rows:
                return None
            row = rows[0]
            nav = _to_float(row.get("DWJZ", ""))
            if nav is None:
                return None
            result = FundNav(
                code=code,
                nav=nav,
                change_pct=_to_float(row.get("JZZZL", "")),
                date=str(row.get("FSRQ", "")),
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            self.last_error = "基金净值接口返回格式异常"
            return None
        self._store(f"f:{code}", result)
        return result

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
            "cooling_down": time.time() < self._cooldown_until,
            "last_error": self.last_error,
            "calls": self.calls,
        }
