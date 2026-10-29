"""Official public macro-data parsing and integration tests."""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from finagent.agent.registry import build_default_registry
from finagent.data.official_sources import (
    SNAPSHOT_FILE,
    load_official_data,
    parse_chinabond_article,
    parse_pboc_announcement,
    parse_world_bank_cpi,
    refresh_official_data,
)
from tests.support import make_agent, talk


CHINABOND_HTML = """
<html><body>
<h1>中债收益率曲线和指数日评2026年10月28日</h1>
<p>【中债指数走势】中债综合指数（净价） 109.0641，上涨0.0089%，
中债综合指数（财富）264.6089，上涨0.0152%，平均市值法到期收益率为1.5864%，
平均市值法久期为6.1776。</p>
<p>【利率品种】中债国债收益率曲线 3M期限下行1BP至1.16%；
2年期收益率下行1BP至1.23%；10年期收益率上行1BP至1.63%。</p>
<p>【信用品种】中债中短期票据收益率曲线（ AAA）3M期限稳定在1.40%，
3年期收益率稳定在1.56%，5年期收益率稳定在1.65%。
中债中短期票据收益率曲线（A）1年期稳定在6.37%。</p>
<p>【海外市场】美国国债收益率曲线10年期下行1BP至4.99%。</p>
<p>【注】示例。</p>
</body></html>
"""


PBOC_HTML = """
<html><body>
<h1>公开市场业务交易公告 [2026]第208号</h1>
<p>2026年10月28日中国人民银行以固定利率、数量招标方式开展了1510亿元
7天期逆回购操作，全额满足了一级交易商需求。</p>
<p>期限 操作 利率 投标量 中标量 7 天 1. 40 % 1510 亿元 1510 亿元</p>
<p>同时，开展了10000亿元隔夜逆回购操作。</p>
</body></html>
"""


class TestOfficialData(unittest.TestCase):
    def test_chinabond_parser_extracts_attributable_yields(self):
        source = parse_chinabond_article(
            CHINABOND_HTML,
            "https://www.chinabond.com.cn/example.html",
        )
        self.assertEqual(source["status"], "ok")
        self.assertEqual(source["published_at"], "2026-10-28")
        self.assertAlmostEqual(source["data"]["government_bond_10y_yield_pct"], 1.63)
        self.assertAlmostEqual(source["data"]["aaa_mtn_3y_yield_pct"], 1.56)
        self.assertAlmostEqual(source["data"]["us_treasury_10y_yield_pct"], 4.99)
        self.assertGreaterEqual(source["parse_confidence"], 0.9)

    def test_pboc_parser_extracts_reverse_repo_fields(self):
        source = parse_pboc_announcement(
            PBOC_HTML,
            "http://www.pbc.gov.cn/example/index.html",
        )
        self.assertEqual(source["status"], "ok")
        self.assertEqual(source["published_at"], "2026-10-28")
        self.assertAlmostEqual(source["data"]["reverse_repo_7d_rate_pct"], 1.40)
        self.assertAlmostEqual(
            source["data"]["reverse_repo_7d_amount_100m_cny"],
            1510.0,
        )
        self.assertAlmostEqual(
            source["data"]["reverse_repo_overnight_amount_100m_cny"],
            10000.0,
        )

    def test_world_bank_cpi_parser_keeps_annual_period(self):
        raw = json.dumps([
            {"page": 1},
            [{
                "date": "2025",
                "value": 0.059564691656542,
                "country": {"value": "China"},
                "indicator": {
                    "value": "Inflation, consumer prices (annual %)"
                },
            }],
        ])
        source = parse_world_bank_cpi(raw)
        self.assertEqual(source["data"]["cpi_period_year"], 2025)
        self.assertAlmostEqual(source["data"]["cpi_yoy_pct"], 0.059565)

    def test_bundled_snapshot_is_used_when_runtime_cache_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = load_official_data(
                cache_file=Path(directory) / "missing.json",
                snapshot_file=SNAPSHOT_FILE,
            )
        self.assertEqual(payload["delivery"], "bundled_snapshot")
        self.assertEqual(payload["data_class"], "official_public_data")
        self.assertFalse(payload["is_simulated"])
        self.assertAlmostEqual(payload["indicators"]["government_bond_10y_yield_pct"], 1.63)

    def test_refresh_failure_keeps_last_successful_values(self):
        chinabond = parse_chinabond_article(CHINABOND_HTML, "https://example.test/cb")
        pboc = parse_pboc_announcement(PBOC_HTML, "http://example.test/pboc")
        world_bank = parse_world_bank_cpi(json.dumps([
            {"page": 1},
            [{
                "date": "2025",
                "value": 0.059564691656542,
                "country": {"value": "China"},
                "indicator": {"value": "CPI"},
            }],
        ]))
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "official.json"
            with (
                patch(
                    "finagent.data.official_sources._fetch_chinabond",
                    return_value=chinabond,
                ),
                patch(
                    "finagent.data.official_sources._fetch_pboc",
                    return_value=pboc,
                ),
                patch(
                    "finagent.data.official_sources._fetch_world_bank",
                    return_value=world_bank,
                ),
            ):
                refresh_official_data(cache_file=cache_file)
            with (
                patch(
                    "finagent.data.official_sources._fetch_chinabond",
                    side_effect=urllib.error.URLError("offline"),
                ),
                patch(
                    "finagent.data.official_sources._fetch_pboc",
                    return_value=pboc,
                ),
                patch(
                    "finagent.data.official_sources._fetch_world_bank",
                    return_value=world_bank,
                ),
            ):
                payload = refresh_official_data(cache_file=cache_file)
        chinabond_source = next(
            source for source in payload["sources"]
            if source["id"] == "chinabond"
        )
        self.assertEqual(chinabond_source["status"], "stale_cache")
        self.assertAlmostEqual(
            payload["indicators"]["government_bond_10y_yield_pct"],
            1.63,
        )

    def test_macro_tool_is_registered_and_returns_source_links(self):
        self.assertIsNotNone(build_default_registry().get("macro.official"))
        agent = make_agent()
        reply = talk(agent, "官方宏观数据")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.intent, "macro.official")
        self.assertIn("中国债券信息网", reply.text)
        self.assertIn("中国人民银行", reply.text)
        self.assertIn("世界银行", reply.text)
        self.assertEqual(
            reply.data["official"]["data_class"],
            "official_public_data",
        )

    def test_cashflow_forecast_carries_official_macro_context(self):
        agent = make_agent()
        reply = talk(agent, "预测未来90天现金流")
        forecast = reply.data["forecast"]
        self.assertEqual(forecast["official_macro"]["data_class"], "official_public_data")
        self.assertIn("official_inflation_adjustment_daily", forecast)
        self.assertIn("10年期国债收益率", reply.text)

    def test_generated_financial_answers_quote_macro_data(self):
        agent = make_agent()
        bill = talk(agent, "分析我这个月的账单")
        self.assertEqual(bill.intent, "bill.analyze")
        self.assertIn("官方数据参照", bill.text)
        self.assertIn("中国债券信息网", bill.text)

        health = talk(agent, "我的财务健康分")
        self.assertEqual(health.intent, "advisor.health_score")
        self.assertIn("7天逆回购利率", health.text)
        self.assertIn("CPI同比", health.text)

        comparison = talk(agent, "比较一下稳健型理财产品")
        self.assertEqual(comparison.intent, "wealth.compare")
        self.assertIn("官方数据参照", comparison.text)
        self.assertIn("official_macro", comparison.data)


if __name__ == "__main__":
    unittest.main()
