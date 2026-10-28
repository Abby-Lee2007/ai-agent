"""Licensed market gateway and public fallback tests."""
from __future__ import annotations

import unittest

from finagent.data.licensed_market import (
    LicensedRestMarketProvider,
    MarketDataRouter,
)
from finagent.data.market import FundNav, Quote


def quote(symbol: str, source: str) -> Quote:
    return Quote(
        symbol=symbol,
        name=symbol.upper(),
        price=10.0,
        prev_close=9.9,
        change=0.1,
        change_pct=1.01,
        timestamp="2026-10-28 10:00:00",
        source=source,
        live=True,
        is_demo=False,
    )


class _LicensedStub:
    available = True

    def quotes(self, symbols):
        return {"sh600036": quote("sh600036", "licensed-test")} if "sh600036" in symbols else {}

    def fund_nav(self, code):
        return FundNav(
            code=code,
            nav=1.25,
            change_pct=0.1,
            date="2026-10-27",
            source="licensed-test",
            live=True,
            is_demo=False,
        )

    def status(self):
        return {"configured": True, "provider": "licensed-test"}


class _PublicStub:
    enabled = True
    mode = "live"

    def quotes(self, symbols):
        return {symbol: quote(symbol, "public-test") for symbol in symbols}

    def fund_nav(self, code):
        return FundNav(code=code, nav=2.0, change_pct=0, date="2026-10-27")

    def status(self):
        return {"enabled": True, "provider": "public-test"}


class TestLicensedMarketRouter(unittest.TestCase):
    def test_licensed_source_is_primary_and_public_fills_missing(self):
        router = MarketDataRouter(
            public=_PublicStub(),
            licensed=_LicensedStub(),
        )
        result = router.quotes(["sh600036", "sh600900"])

        self.assertEqual(result["sh600036"].source, "licensed-test")
        self.assertEqual(result["sh600900"].source, "public-test")
        self.assertEqual(
            router.status()["role"],
            "licensed-primary-with-public-fallback",
        )

    def test_licensed_nav_precedes_public_nav(self):
        router = MarketDataRouter(
            public=_PublicStub(),
            licensed=_LicensedStub(),
        )
        nav = router.fund_nav("000171")

        self.assertEqual(nav.source, "licensed-test")
        self.assertEqual(nav.nav, 1.25)

    def test_rest_adapter_normalizes_quote_payload(self):
        provider = LicensedRestMarketProvider(
            url="https://licensed.example/api/{symbols}",
            api_key="test-key",
            provider_name="licensed-test",
        )
        provider._request = lambda path, params: {
            "quotes": [{
                "symbol": "sh600036",
                "name": "招商银行",
                "price": 42.75,
                "prev_close": 43.05,
                "change_pct": -0.7,
                "timestamp": "2026-10-20 10:03:54",
            }]
        }

        result = provider.quotes(["sh600036"])

        self.assertAlmostEqual(result["sh600036"].price, 42.75)
        self.assertEqual(result["sh600036"].source, "licensed-test")
        self.assertFalse(result["sh600036"].is_demo)


if __name__ == "__main__":
    unittest.main()
