"""Optional licensed market-data gateway with public-data fallback."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from config import LicensedMarketConfig
from finagent.data.market import FundNav, MarketDataProvider, Quote, _to_float


class LicensedRestMarketProvider:
    """Normalize a licensed REST vendor into the local Quote/FundNav contract.

    Expected quote response:
      {"quotes":[{"symbol","name","price","prev_close","change","change_pct",
                  "timestamp","pe","pb"}]}

    Expected fund response:
      {"nav":{"code","nav","change_pct","date"}}

    A vendor-specific gateway can translate its licensed payload into this schema.
    """

    def __init__(
        self,
        url: str = "",
        api_key: str = "",
        provider_name: str = "",
        timeout: float = 8.0,
    ) -> None:
        self.url = (url or LicensedMarketConfig.URL).strip().rstrip("/")
        self.api_key = (api_key or LicensedMarketConfig.API_KEY).strip()
        self.provider_name = (
            provider_name or LicensedMarketConfig.NAME or "licensed-market"
        )
        self.timeout = timeout or LicensedMarketConfig.TIMEOUT
        self.last_error = ""
        self.last_success_at = ""
        self.calls = 0

    @property
    def available(self) -> bool:
        return bool(self.url and self.api_key)

    def _request(self, path: str, params: Dict[str, str]) -> Optional[Dict]:
        if not self.available:
            return None
        query = urllib.parse.urlencode(params)
        separator = "&" if "?" in path else "?"
        url = (
            self.url
            .replace("{symbols}", urllib.parse.quote(params.get("symbols", "")))
            .replace("{code}", urllib.parse.quote(params.get("code", "")))
        )
        if "{" not in path and url == self.url:
            url = f"{url}/{path.lstrip('/')}"
        if "?" not in url and query:
            url = f"{url}{separator}{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-API-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": "FinAgent-Sandbox/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                self.calls += 1
                self.last_success_at = datetime.now().isoformat(timespec="seconds")
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        wanted = [str(item).strip() for item in symbols if str(item).strip()]
        if not wanted or not self.available:
            return {}
        payload = self._request("quotes", {"symbols": ",".join(wanted)})
        rows = payload.get("quotes") if isinstance(payload, dict) else None
        result: Dict[str, Quote] = {}
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().lower()
            price = _to_float(item.get("price"))
            if not symbol or price is None or price <= 0:
                continue
            prev_close = _to_float(item.get("prev_close")) or price
            change = _to_float(item.get("change"))
            if change is None:
                change = round(price - prev_close, 4)
            change_pct = _to_float(item.get("change_pct"))
            if change_pct is None and prev_close:
                change_pct = round(change / prev_close * 100, 4)
            result[symbol] = Quote(
                symbol=symbol,
                name=str(item.get("name") or symbol),
                price=price,
                prev_close=prev_close,
                change=change,
                change_pct=change_pct or 0.0,
                pe=_to_float(item.get("pe")),
                pb=_to_float(item.get("pb")),
                timestamp=str(item.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                source=str(item.get("source") or self.provider_name),
                live=True,
                is_demo=False,
            )
        return result

    def fund_nav(self, code: str) -> Optional[FundNav]:
        if not str(code or "").strip() or not self.available:
            return None
        payload = self._request("fund-nav", {"code": str(code).strip()})
        item = payload.get("nav") if isinstance(payload, dict) else None
        if not isinstance(item, dict):
            return None
        nav = _to_float(item.get("nav"))
        if nav is None:
            return None
        return FundNav(
            code=str(item.get("code") or code),
            nav=nav,
            change_pct=_to_float(item.get("change_pct")),
            date=str(item.get("date") or ""),
            source=str(item.get("source") or self.provider_name),
            live=True,
            is_demo=False,
        )

    def status(self) -> Dict[str, object]:
        return {
            "configured": self.available,
            "provider": self.provider_name,
            "calls": self.calls,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }


class MarketDataRouter:
    """Try licensed data first, then fall back to public quote adapters."""

    def __init__(
        self,
        public: Optional[MarketDataProvider] = None,
        licensed: Optional[LicensedRestMarketProvider] = None,
    ) -> None:
        self.public = public or MarketDataProvider()
        self.licensed = licensed or LicensedRestMarketProvider()
        self.mode = self.public.mode

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and (
            self.licensed.available or self.public.enabled
        )

    def quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if not self.enabled:
            return {}
        result = self.licensed.quotes(symbols) if self.licensed.available else {}
        missing = [item for item in symbols if item not in result]
        if missing and self.public.enabled:
            result.update(self.public.quotes(missing))
        return result

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes([symbol]).get(symbol)

    def fund_nav(self, code: str) -> Optional[FundNav]:
        if not self.enabled:
            return None
        if self.licensed.available:
            result = self.licensed.fund_nav(code)
            if result is not None:
                return result
        return self.public.fund_nav(code) if self.public.enabled else None

    def status(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "licensed": self.licensed.status(),
            "public": self.public.status(),
            "role": (
                "licensed-primary-with-public-fallback"
                if self.licensed.available
                else "public-data-enhancement"
            ),
            "production_grade": self.licensed.available,
        }


def build_market_router() -> MarketDataRouter:
    return MarketDataRouter()
