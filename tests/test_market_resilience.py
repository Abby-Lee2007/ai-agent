"""Market data freshness and provider resilience tests."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from finagent.data.market import MarketDataProvider, Quote
from finagent.data.provenance import is_stale


class TestFreshness(unittest.TestCase):
    def test_old_timestamp_is_stale(self):
        now = datetime(2026, 10, 20, 12, 0, 0)
        updated = (now - timedelta(minutes=10)).isoformat()

        self.assertTrue(is_stale(updated, ttl_seconds=60, now=now))

    def test_quote_exposes_staleness(self):
        quote = Quote(
            symbol="sh600036",
            name="招商银行",
            price=42.75,
            prev_close=43.05,
            change=-0.30,
            change_pct=-0.70,
            updated_at=(datetime.now() - timedelta(minutes=5)).isoformat(),
            freshness_seconds=60,
        )

        self.assertTrue(quote.stale)


class TestMarketProviderStatus(unittest.TestCase):
    def test_public_market_provider_is_labeled_as_enhancement(self):
        provider = MarketDataProvider(mode="off")

        status = provider.status()

        self.assertEqual(status["role"], "public-data-enhancement")
        self.assertFalse(status["production_grade"])
        self.assertIn("cache_entries", status)
        self.assertIn("cooldown_seconds", status)
        self.assertEqual(status["source_order"], ["腾讯财经", "东方财富"])


class _FallbackQuoteProvider(MarketDataProvider):
    def __init__(self) -> None:
        super().__init__(mode="live")
        self.urls: list[str] = []

    def _fetch(self, url: str, encoding: str = "gbk", **kwargs):
        self.urls.append(url)
        if "qt.gtimg.cn" in url:
            return ""
        return (
            '{"data":{"f43":42.75,"f44":43.13,"f45":42.56,"f46":42.95,'
            '"f57":"600036","f58":"招商银行","f60":43.05,"f169":-0.30,'
            '"f170":-0.70,"f59":2,"f168":7.11,"f167":0.94}}'
        )


class TestFallbackSource(unittest.TestCase):
    def test_uses_backup_source_when_primary_returns_nothing(self):
        provider = _FallbackQuoteProvider()

        quote = provider.quote("sh600036")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, "东方财富")
        self.assertTrue(any("push2.eastmoney.com" in url for url in provider.urls))
        self.assertEqual(provider.status()["last_source"], "东方财富")

    def test_backup_is_tried_even_after_primary_opens_circuit(self):
        class _CircuitThenFallback(_FallbackQuoteProvider):
            def _fetch(self, url: str, encoding: str = "gbk", **kwargs):
                if "qt.gtimg.cn" in url:
                    self._cooldown_until = float("inf")
                    return None
                return super()._fetch(url, encoding, **kwargs)

        provider = _CircuitThenFallback()
        quote = provider.quote("sh600036")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, "东方财富")


if __name__ == "__main__":
    unittest.main()
