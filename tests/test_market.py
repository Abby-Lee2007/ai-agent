"""真实行情数据源测试（默认离线，不访问网络）。"""
from __future__ import annotations

import os
import unittest
from datetime import datetime

from tests.support import make_agent, run_to_completion
from finagent.data.market import (
    FundNav,
    MarketDataProvider,
    Quote,
    build_market_overview,
)

SAMPLE_QUOTE = (
    'v_sh600036="1~招商银行~600036~42.75~43.05~42.95~160110~78737~81371~42.69~293~'
    '42.68~236~42.67~92~42.66~83~42.65~106~42.70~477~42.71~210~42.72~134~42.73~41~'
    '42.74~103~~20261020100354~-0.30~-0.70~43.13~42.56~42.75/160110/69297~160110~'
    '69297~0.08~7.11~~43.13~42.56~1.32~8818.87~10781.48~0.94~47.36~38.75~1.02~'
    '-1~-0.72~~";'
)

SAMPLE_HISTORY = (
    '{"code":0,"data":{"sh600036":{"qfqday":['
    '["2026-09-01","40.00","40.20","40.50","39.80","1000"],'
    '["2026-09-02","40.20","40.50","40.80","40.00","1200"],'
    '["2026-09-03","40.50","40.30","40.90","40.10","900"],'
    '["2026-09-04","40.30","40.90","41.00","40.20","1300"],'
    '["2026-09-05","40.90","41.20","41.30","40.70","1100"],'
    '["2026-09-06","41.20","41.00","41.40","40.90","1000"],'
    '["2026-09-07","41.00","41.50","41.60","40.80","1500"],'
    '["2026-09-08","41.50","41.30","41.70","41.10","950"],'
    '["2026-09-09","41.30","41.80","41.90","41.20","1400"],'
    '["2026-09-10","41.80","42.00","42.10","41.60","1250"]'
    '],"qt":{"sh600036":["1","招商银行","600036"]}}}}'
)

SAMPLE_FUND_HISTORY = (
    '{"Data":{"LSJZList":['
    '{"FSRQ":"2026-09-01","DWJZ":"2.0000","JZZZL":"0.10"},'
    '{"FSRQ":"2026-09-02","DWJZ":"2.0100","JZZZL":"0.50"},'
    '{"FSRQ":"2026-09-03","DWJZ":"2.0050","JZZZL":"-0.25"},'
    '{"FSRQ":"2026-09-04","DWJZ":"2.0200","JZZZL":"0.75"},'
    '{"FSRQ":"2026-09-05","DWJZ":"2.0300","JZZZL":"0.50"},'
    '{"FSRQ":"2026-09-06","DWJZ":"2.0250","JZZZL":"-0.25"},'
    '{"FSRQ":"2026-09-07","DWJZ":"2.0400","JZZZL":"0.74"},'
    '{"FSRQ":"2026-09-08","DWJZ":"2.0500","JZZZL":"0.49"},'
    '{"FSRQ":"2026-09-09","DWJZ":"2.0450","JZZZL":"-0.24"},'
    '{"FSRQ":"2026-09-10","DWJZ":"2.0600","JZZZL":"0.73"}'
    ']}}'
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

    def test_parses_public_daily_history(self):
        provider = _StubProvider(SAMPLE_HISTORY)
        history = provider.history("sh600036", 30)
        self.assertIsNotNone(history)
        self.assertEqual(history["source"], "腾讯财经")
        self.assertEqual(len(history["points"]), 10)
        self.assertAlmostEqual(history["points"][-1]["close"], 42.0)

    def test_parses_fund_nav_history_in_date_order(self):
        provider = _StubProvider(SAMPLE_FUND_HISTORY)
        history = provider.fund_nav_history("000171", 30)
        self.assertIsNotNone(history)
        self.assertEqual(history["points"][0]["date"], "2026-09-01")
        self.assertEqual(history["points"][-1]["date"], "2026-09-10")
        self.assertAlmostEqual(history["points"][-1]["close"], 2.06)


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
            "sh601398": ("工商银行", 7.12, 0.28, 6.5),
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

    def history(self, symbol, limit=250):
        base = {
            "sh512890": 1.227,
            "sh510300": 4.217,
            "sh600036": 42.77,
            "sh600900": 29.48,
            "sh601318": 51.25,
            "sh601398": 7.12,
        }.get(symbol, 10.0)
        points = []
        for index in range(max(130, min(int(limit), 250))):
            close = base * (1 + index * 0.0002)
            points.append({
                "date": f"2026-{index // 28 % 12 + 1:02d}-{index % 28 + 1:02d}",
                "close": round(close, 4),
                "adjusted_close": round(close, 4),
            })
        return {
            "symbol": symbol.upper(),
            "source": "腾讯财经",
            "points": points,
            "is_demo": False,
        }

    def fund_nav_history(self, code, limit=250):
        if code != "000171":
            return None
        points = []
        for index in range(140):
            nav = 1.9 * (1 + index * 0.0003)
            points.append({
                "date": f"2026-{index // 28 % 12 + 1:02d}-{index % 28 + 1:02d}",
                "close": round(nav, 4),
                "adjusted_close": round(nav * 1.2, 4),
            })
        return {
            "symbol": code,
            "source": "东方财富",
            "points": points,
            "is_demo": False,
        }


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
        self.assertGreater(reply.data["live_quant_count"], 0)
        self.assertTrue(all(
            item["advice_score"] is not None
            for item in reply.data["product_details"]
        ))

    def test_stock_request_is_not_treated_as_subscribe(self):
        reply = self.agent.handle("我想买点股票")
        self.assertEqual(reply.intent, "wealth.recommend")

    def test_fund_request_stays_fund(self):
        reply = self.agent.handle("推荐几个基金")
        self.assertEqual(reply.data["asset_class"], "fund")
        self.assertNotIn("股票没有", reply.text)
        self.assertFalse(any(
            item["category"] == "债券基金"
            for item in reply.data["product_details"]
        ))

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
        reply = self.agent.handle("推荐债券基金")
        self.assertEqual(reply.data["asset_class"], "bond_fund")
        self.assertIn("000171", reply.text)
        self.assertIn("2.0016", reply.text)
        self.assertIn("2026-10-19", reply.text)

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

    def test_direct_symbol_purchase_uses_fresh_quote_and_whole_lots(self):
        before = self.agent.providers.cards.find(
            self.agent.user_id,
            "工资卡",
        ).balance
        reply = self.agent.handle("买601398，金额1000元，用工资卡")
        self.assertEqual(reply.intent, "wealth.subscribe")
        if reply.kind == "question":
            reply = self.agent.handle("工资卡")
        self.assertIn("最新价 7.120 元", reply.text)
        self.assertIn("预计买入 100股", reply.text)
        completed = run_to_completion(self.agent, reply)
        self.assertTrue(completed.ok, completed.text)
        self.assertIn("模拟买入数量：100 股", completed.text)
        self.assertAlmostEqual(completed.data["execution_price"], 7.12)
        self.assertEqual(completed.data["quantity"], 100)
        self.assertAlmostEqual(completed.data["amount"], 717.0)
        after = self.agent.providers.cards.find(
            self.agent.user_id,
            "工资卡",
        ).balance
        self.assertAlmostEqual(before - after, 717.0)

    def test_direct_fund_code_uses_latest_disclosed_nav(self):
        reply = self.agent.handle("买000171，金额2000元，用工资卡")
        self.assertEqual(reply.intent, "wealth.subscribe")
        if reply.kind == "question":
            reply = self.agent.handle("工资卡")
        self.assertIn("最新净值 2.0016 元", reply.text)
        completed = run_to_completion(self.agent, reply)
        self.assertTrue(completed.ok, completed.text)
        self.assertIn("估算确认份额", completed.text)
        self.assertEqual(completed.data["execution_price"], 2.0016)
        product = self.agent.store.find_product("000171")
        expected_fee = round(2000 * product.fee_rate, 2)
        self.assertAlmostEqual(
            completed.data["quantity"],
            round((2000 - expected_fee) / 2.0016, 4),
        )


class TestMarketOverview(unittest.TestCase):
    def test_overview_includes_major_indices_turnover_and_breadth_roles(self):
        market = _FakeMarket()
        market.quotes = lambda symbols: {
            symbol: Quote(
                symbol=symbol,
                name={
                    "sh000001": "上证指数",
                    "sz399001": "深证成指",
                    "sh000300": "沪深300",
                    "sz399006": "创业板指",
                    "sh000688": "科创50",
                    "bj899050": "北证50",
                }[symbol],
                price=100 + float(symbol[-1]),
                prev_close=100,
                change=1,
                change_pct=({"sz399006": 2.5, "bj899050": -0.8}).get(symbol, 0.5),
                timestamp="2026-10-28 15:00:00",
                source="腾讯财经",
                turnover_100m_cny=(
                    7510.19 if symbol == "sh000001"
                    else 8774.68 if symbol == "sz399001"
                    else None
                ),
            )
            for symbol in symbols
        }
        overview = build_market_overview(
            market,
            now=datetime(2026, 10, 28, 16, 0, 0),
        )
        self.assertTrue(overview["available"])
        self.assertEqual(len(overview["indices"]), 6)
        self.assertEqual(overview["market_state"], "已收盘")
        self.assertAlmostEqual(overview["turnover_100m_cny"], 16284.87)
        self.assertEqual(overview["leading_index"]["name"], "创业板指")
        self.assertEqual(overview["lagging_index"]["name"], "北证50")
        agent = make_agent()
        agent.market = market
        reply = agent.handle("今天大盘怎么样")
        self.assertEqual(reply.intent, "market.overview")
        self.assertIn("A股市场行情", reply.text)
        self.assertIn("沪深两市成交额", reply.text)
        self.assertIn("盘面温度", reply.text)


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
