"""NLU 层测试：金额/日期解析与意图识别。"""
from __future__ import annotations

import unittest
from datetime import date

from tests.support import make_store
from finagent.nlu.intent_parser import IntentParser
from finagent.utils import parse_amount, parse_date, parse_period


class TestAmountParsing(unittest.TestCase):
    def test_plain_and_unit_amounts(self):
        self.assertEqual(parse_amount("给老婆转2000块"), 2000.0)
        self.assertEqual(parse_amount("转账500给王强"), 500.0)
        self.assertEqual(parse_amount("预算1000元"), 1000.0)

    def test_scale_words(self):
        self.assertEqual(parse_amount("把信用卡额度提到3万"), 30000.0)
        self.assertEqual(parse_amount("我有5万块"), 50000.0)

    def test_ignores_phone_and_date_numbers(self):
        self.assertEqual(parse_amount("给13700003333转500"), 500.0)
        self.assertEqual(parse_amount("下个月5号自动给房东转3800房租"), 3800.0)
        self.assertIsNone(parse_amount("下个月5号"))


class TestDateParsing(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 14)
        self.birthday = date(2026, 11, 20)

    def test_relative_and_absolute(self):
        self.assertEqual(parse_date("明天", self.today), date(2026, 10, 15))
        self.assertEqual(parse_date("下个月5号", self.today), date(2026, 11, 5))
        self.assertEqual(parse_date("2026-11-18", self.today), date(2026, 11, 18))
        self.assertEqual(parse_date("11月20日", self.today), date(2026, 11, 20))

    def test_relative_to_birthday(self):
        self.assertEqual(parse_date("生日前2天", self.today, base=self.birthday), date(2026, 11, 18))

    def test_period(self):
        self.assertEqual(parse_period("上个月", self.today), "last_month")
        self.assertEqual(parse_period("今年的账单", self.today), "this_year")
        self.assertEqual(parse_period("最近7天", self.today), "last_7_days")


class TestIntentRecognition(unittest.TestCase):
    def setUp(self):
        self.parser = IntentParser(store=make_store(), user_id="u_001", llm=None, mode="off")

    def _intent(self, text: str) -> str:
        return self.parser.parse(text).name

    def test_six_core_scenarios(self):
        cases = {
            "给老婆转2000块": "transfer.send",
            "下个月5号自动给房东转3800房租": "transfer.schedule",
            "昨晚聚餐960，我、王强、李思琪AA": "transfer.split_aa",
            "分析我这个月的账单": "bill.analyze",
            "最近7天花了多少": "bill.analyze",
            "上个月花了多少": "bill.analyze",
            "帮我看看有没有异常交易": "bill.anomaly",
            "生成我的年度账单报告": "bill.report",
            "我今年在星巴克花了多少钱": "bill.search",
            "推荐几个稳健的理财产品": "wealth.recommend",
            "买1万块稳利债券A": "wealth.subscribe",
            "赎回稳利债券A": "wealth.redeem",
            "我有哪些卡": "card.list",
            "信用卡额度提到3万": "card.limit_adjust",
            "关闭信用卡的境外交易": "card.control",
            "我的卡丢了，帮我挂失": "card.report_lost",
            "有哪些自动续费": "subscription.detect",
            "取消腾讯视频会员": "subscription.cancel",
            "恢复腾讯视频VIP自动续费": "subscription.resume",
            "帮我充腾讯视频3个月60元、Keep12个月199元": "membership.purchase",
            "把腾讯视频改成12个月续费，续费价288元": "subscription.change_cycle",
            "我老婆快生日了，帮我准备一下": "crossscene.birthday_plan",
            "今天有什么要注意的": "crossscene.daily_brief",
            "我还有多少钱": "account.balance",
            "你用的什么大模型": "agent.about",
            "你是谁": "agent.about",
            "识别这张账单截图 samples/bill_sample.png": "bill.scan_receipt",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self._intent(text), expected)

    def test_unknown_intent(self):
        self.assertTrue(self.parser.parse("今天天气怎么样").is_unknown())

    def test_slash_list_is_not_a_file_path(self):
        """'信用卡/虚拟卡/附属卡' 不能被当成账单图片路径。"""
        intent = self.parser.parse("申请新卡（信用卡/虚拟卡/附属卡），提交后进入审批流程")
        self.assertEqual(intent.name, "card.apply")

    def test_real_path_still_detected(self):
        for text in ["识别这张账单截图 samples/bill_sample.png",
                     "帮我看一下 C:\\Users\\me\\bill.jpg",
                     "识别 /tmp/receipt.png",
                     "识别这张账单截图 C:\\tmp\\runtime_uploads\\aa1234.txt"]:
            with self.subTest(text=text):
                parsed = self.parser.parse(text)
                self.assertEqual(parsed.name, "bill.scan_receipt")
                self.assertTrue(parsed.slots.get("image"))

    def test_keyword_fallback_covers_colloquial_phrasing(self):
        """不使用预设句式时，靠关键词兜底也能识别。"""
        cases = {
            "我这卡怎么提额": "card.limit_adjust",
            "有啥自动扣钱的": "subscription.detect",
            "卡丢了咋整": "card.report_lost",
            "能不能给我解冻一下": "card.unlock",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.parser.parse(text).name, expected)

    def test_upload_format_question_is_not_bill_analysis(self):
        cases = [
            "可以上传哪些格式的账单",
            "Excel 支持什么格式",
            "能识别哪些文件",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(self.parser.parse(text).name, "bill.supported_formats")

    def test_slot_extraction(self):
        intent = self.parser.parse("给老婆转2000块，备注生日礼物")
        self.assertEqual(intent.slots.get("payee"), "老婆")
        self.assertEqual(intent.slots.get("amount"), 2000.0)
        self.assertEqual(intent.slots.get("note"), "生日礼物")

    def test_aa_participants(self):
        intent = self.parser.parse("昨晚聚餐960，我、王强、李思琪AA")
        self.assertEqual(intent.slots.get("total"), 960.0)
        self.assertEqual(set(intent.slots.get("participants", [])), {"我", "王强", "李思琪"})


if __name__ == "__main__":
    unittest.main()
