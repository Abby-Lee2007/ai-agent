"""真实行情数据源测试（默认离线，不访问网络）。"""
from __future__ import annotations

import os
import unittest

from tests.support import make_agent
from finagent.data.market import FundNav, MarketDataProvider, Quote

SAMPLE_QUOTE = (
    'v_sh600036="1~招商银行~600036~42.75~43.05~42.95~160110~78737~81371~42.69~293~'
    '42.68~236~42.67~92~42.66~83~42.65~106~42.70~477~42.71~210~42.72~134~42.73~41~'
    '42.74~103~~20261020100354~-0.30~-0.70~43.13~42.56~42.75/160110/69297~160110~'
    '69297~0.08~7.11~~43.13~42.56~1.32~8818.87~10781.48~0.94~47.36~38.75~1.02~'
    '-1~-0.72~~";'
)


class _StubProvider(MarketDataProvider):
    def __init__(self, payload: str) -> None:
        super().__init__(mode="live")
        self.payload = payload

    def _fetch(self, url: str, encoding: str = "gbk", **kwargs):
        return self.payload


class TestQuoteParsing(unittest.TestCase):
    def test_parses_tencent_payload(self):
        provider = _StubProvider(SAMPLE_QUOTE)
        quote = provider.quote("sh600036")
        self.assertIsNotNone(quote)
        self.assertEqual(quote.name, "招商银行")
        self.assertEqual(quote.symbol, "sh600036")
        self.assertAlmostEqual(quote.price, 42.75, places=2)
        self.assertAlmostEqual(quote.prev_close, 43.05, places=2)
        self.assertAlmostEqual(quote.change_pct, -0.70, places=2)
        self.assertAlmostEqual(quote.pe, 7.11, places=2)
        self.assertAlmostEqual(quote.pb, 0.94, places=2)
        self.assertEqual(quote.timestamp, "2026-10-20 10:03:54")
        self.assertTrue(quote.live)

    def test_off_mode_never_fetches(self):
        provider = MarketDataProvider(mode="off")
        self.assertFalse(provider.enabled)
        self.assertEqual(provider.quotes(["sh600036"]), {})

    def test_bad_payload_returns_empty(self):
        provider = _StubProvider("not a quote")
        self.assertEqual(provider.quotes(["sh600036"]), {})


class _FakeMarket:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def quotes(self, symbols):
        self.calls += 1
        table = {
            "sh512890": ("红利低波ETF", 1.227, -0.73, None),
            "sh510300": ("沪深300ETF", 4.217, -0.54, None),
            "sh600036": ("招商银行", 42.77, -0.65, 7.1),
            "sh600900": ("长江电力", 29.48, -0.20, 19.9),
            "sh601318": ("中国平安", 51.25, -0.81, 8.2),
            "sh019547": ("16国债19", 120.85, 0.21, None),
            "sh113050": ("南银转债", 144.967, 0.0, None),
        }
        result = {}
        for symbol in symbols:
            if symbol in table:
                name, price, pct, pe = table[symbol]
                result[symbol] = Quote(
                    symbol=symbol, name=name, price=price, prev_close=price / (1 + pct / 100),
                    change=0.0, change_pct=pct, pe=pe, timestamp="2026-10-20 10:05:00",
                )
        return result

    def quote(self, symbol):
        return self.quotes([symbol]).get(symbol)

    def fund_nav(self, code):
        if code == "000171":
            return FundNav(code=code, nav=2.0016, change_pct=-0.29, date="2026-10-19")
        return None


class TestStockRecommendation(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()
        self.agent.market = _FakeMarket()

    def test_living_expense_stock_request(self):
        reply = self.agent.handle("我有2万生活费，现在想要购买股票，给我低风险的股票推荐")
        self.assertEqual(reply.intent, "wealth.recommend")
        self.assertEqual(reply.data["asset_class"], "stock")
        self.assertEqual(reply.data["purpose"], "living")
        self.assertIn("股票没有「低风险」这一档", reply.text)
        self.assertIn("招商银行", reply.text)
        self.assertIn("SH600036", reply.text)
        self.assertIn("42.770", reply.text)
        self.assertIn("腾讯财经", reply.text)
        # 生活费不足应急金时，不给出买入配置
        self.assertNotIn("组合配置建议", reply.text)
        self.assertLessEqual(reply.data["investable"], 0)

    def test_idle_money_stock_request_gets_live_plan(self):
        reply = self.agent.handle("我有5万闲钱，想买点股票")
        self.assertIn("实时", reply.text)
        self.assertIn("按实时价格估算", reply.text)
        self.assertIn("股", reply.text)
        self.assertGreater(reply.data["live_count"], 0)

    def test_stock_request_is_not_treated_as_subscribe(self):
        reply = self.agent.handle("我想买点股票")
        self.assertEqual(reply.intent, "wealth.recommend")

    def test_fund_request_stays_fund(self):
        reply = self.agent.handle("推荐几个基金")
        self.assertEqual(reply.data["asset_class"], "fund")
        self.assertNotIn("股票没有", reply.text)

    def test_generic_wealth_request(self):
        reply = self.agent.handle("推荐几个理财产品")
        self.assertEqual(reply.data["asset_class"], "mixed")

    def test_bond_request_returns_bonds_with_live_quotes(self):
        reply = self.agent.handle("推荐点债券")
        self.assertEqual(reply.data["asset_class"], "bond")
        self.assertIn("16国债19", reply.text)
        self.assertIn("南银转债", reply.text)
        self.assertIn("SH019547", reply.text)
        self.assertIn("120.850", reply.text)

    def test_fund_request_shows_real_nav(self):
        reply = self.agent.handle("推荐几个基金")
        self.assertIn("000171", reply.text)
        self.assertIn("2.0016", reply.text)
        self.assertIn("净值 2026-10-19", reply.text)

    def test_derivative_request_is_educational_and_blocked(self):
        reply = self.agent.handle("我想买点衍生品，比如期权")
        self.assertEqual(reply.data["asset_class"], "derivative")
        self.assertTrue(reply.data["blocked_by_suitability"])
        self.assertIn("合格投资者", reply.text)
        self.assertIn("上证50ETF期权", reply.text)
        self.assertIn("不可申购", reply.text)

    def test_offline_falls_back_to_demo_and_says_so(self):
        agent = make_agent()          # market mode = off
        reply = agent.handle("我有5万闲钱，想买点股票")
        self.assertIn("演示", reply.text)
        self.assertNotIn("实时", reply.text)


class TestLiveMarketOptional(unittest.TestCase):
    @unittest.skipUnless(os.getenv("FINAGENT_LIVE_TEST") == "1", "需要联网，设置 FINAGENT_LIVE_TEST=1 才跑")
    def test_live_quote(self):
        provider = MarketDataProvider(mode="live", timeout=8)
        quote = provider.quote("sh600036")
        self.assertIsNotNone(quote, msg=provider.last_error)
        self.assertGreater(quote.price, 0)
        self.assertEqual(quote.symbol, "sh600036")


if __name__ == "__main__":
    unittest.main()
