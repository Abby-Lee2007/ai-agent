"""粘贴输入与鲁棒性测试：整段账单粘贴、时间戳不当收款人、自我说明。"""
from __future__ import annotations

import io
import sys
import unittest
from datetime import date

from tests.support import make_agent
from finagent.cli.app import make_completeness_check
from finagent.cli.input_reader import read_message
from finagent.vision.receipt_parser import (
    looks_like_bill_fragment,
    looks_like_pasted_bill,
    parse_receipt_text,
    strip_comment_lines,
)

# 真实微信账单导出的排版：商户+金额一行，时间在下一行
WECHAT_PASTE = """2026年10月
全部账单  查找交易  收支统计>
抖音电商商家   -148.72
10月14日13:11
微信红包-发给何心熠   -2.50
10月14日08:47
转账-退款   +6.50
10月14日08:45
转账-转给何心熠   -6.50
10月13日23:33  对方已退还
罗森(深圳大学粤海校    -20.40
10月13日10:00
湘小悦湘菜   -16.00
10月12日17:35
支出¥7283.34 收入¥9577.09
"""


class TestPastedBillDetection(unittest.TestCase):
    def test_detects_pasted_bill(self):
        self.assertTrue(looks_like_pasted_bill(WECHAT_PASTE))

    def test_does_not_flag_normal_sentence(self):
        for text in ["给老婆转2000块", "分析我这个月的账单", "你能做什么"]:
            with self.subTest(text=text):
                self.assertFalse(looks_like_pasted_bill(text))

    def test_parses_wechat_layout(self):
        records = parse_receipt_text(WECHAT_PASTE, date(2026, 10, 14))
        self.assertEqual(len(records), 6)
        first = records[0]
        self.assertEqual(first.date, "2026-10-14")
        self.assertEqual(first.time, "13:11")
        self.assertEqual(first.merchant, "抖音电商商家")
        self.assertEqual(first.amount, -148.72)
        self.assertEqual(records[1].merchant, "微信红包 发给何心熠")
        self.assertEqual(records[1].category, "人情往来")
        # 退款是收入，不能变成支出
        self.assertEqual(records[2].amount, 6.50)
        self.assertEqual(records[5].date, "2026-10-12")

    def test_summary_line_is_not_a_record(self):
        records = parse_receipt_text(WECHAT_PASTE, date(2026, 10, 14))
        self.assertFalse(any("支出" in r.merchant for r in records))


class TestPastedBillFlow(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_pasted_bill_is_one_message_not_many(self):
        reply = self.agent.handle(WECHAT_PASTE)
        self.assertEqual(reply.intent, "bill.scan_receipt")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(reply.data["count"], 6)
        self.assertEqual([t["tool"] for t in reply.trace], ["bill.scan_receipt"])

    def test_pasted_bill_import(self):
        self.agent.handle(WECHAT_PASTE)
        before = len(self.agent.store.transactions_of("u_001"))
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["imported"], 6)
        self.assertEqual(len(self.agent.store.transactions_of("u_001")), before + 6)

    def test_transfer_word_in_bill_does_not_trigger_transfer(self):
        """账单里的"转账-退款"不能被当成转账指令。"""
        reply = self.agent.handle(WECHAT_PASTE)
        self.assertNotEqual(reply.intent, "transfer.send")
        self.assertTrue(all(t["tool"] != "transfer.send" for t in reply.trace))


class TestNoiseSlotGuard(unittest.TestCase):
    """时间戳/金额绝不能被当成收款人。"""

    def setUp(self):
        self.agent = make_agent()

    def test_timestamp_is_not_accepted_as_payee(self):
        self.agent.handle("转账-退款   +6.50")
        reply = self.agent.handle("10月14日08:45")
        self.assertEqual(reply.kind, "question")
        pending = self.agent.session.pending
        self.assertIsNotNone(pending)
        self.assertNotIn("payee", pending.slots)
        self.assertIsNone(pending.plan)

    def test_plain_number_is_not_accepted_as_payee(self):
        self.agent.handle("转给")
        reply = self.agent.handle("1234.56")
        self.assertEqual(reply.kind, "question")

    def test_real_name_still_works(self):
        self.agent.handle("转给")
        reply = self.agent.handle("王强")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.slots.get("payee"), "王强")


class TestSelfIntroduction(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_about_intent(self):
        for text in ["你用的什么大模型", "你是谁", "你用的什么技术"]:
            with self.subTest(text=text):
                reply = self.agent.handle(text)
                self.assertEqual(reply.intent, "agent.about")
                self.assertIn("自然语言理解", reply.text)
                self.assertIn("FINAGENT_LLM_BASE_URL", reply.text)

    def test_about_reports_offline_channel(self):
        reply = self.agent.handle("你用的什么大模型")
        self.assertFalse(reply.data["api_key_configured"])
        self.assertIn("规则", reply.text)


class TestInputReader(unittest.TestCase):
    def test_piped_input_falls_back_to_single_line(self):
        original = sys.stdin
        sys.stdin = io.StringIO("第一行\n第二行\n")
        try:
            self.assertEqual(read_message(""), "第一行")
            self.assertEqual(read_message(""), "第二行")
            self.assertIsNone(read_message(""))
        finally:
            sys.stdin = original

    def test_incomplete_first_line_collects_the_rest(self):
        """第一行不像完整指令时，把后面的内容一起收进来。"""
        original = sys.stdin
        sys.stdin = io.StringIO("抖音电商商家   -148.72\n10月14日13:11\n湘小悦湘菜   -16.00\n")
        agent = make_agent()
        try:
            block = read_message("", is_complete=make_completeness_check(agent))
        finally:
            sys.stdin = original
        self.assertEqual(len(block.splitlines()), 3)
        self.assertIn("湘小悦湘菜", block)

    def test_complete_first_line_is_not_merged(self):
        original = sys.stdin
        sys.stdin = io.StringIO("分析我这个月的账单\n推荐几个理财产品\n")
        agent = make_agent()
        try:
            first = read_message("", is_complete=make_completeness_check(agent))
            second = read_message("", is_complete=make_completeness_check(agent))
        finally:
            sys.stdin = original
        self.assertEqual(first, "分析我这个月的账单")
        self.assertEqual(second, "推荐几个理财产品")

    def test_command_line_stops_the_block(self):
        """块输入遇到 /命令 要停下来，不能被吞进账单里。"""
        original = sys.stdin
        sys.stdin = io.StringIO("抖音电商商家   -148.72\n/quit\n")
        agent = make_agent()
        try:
            block = read_message("", is_complete=make_completeness_check(agent))
            following = read_message("", is_complete=make_completeness_check(agent))
        finally:
            sys.stdin = original
        self.assertEqual(block, "抖音电商商家   -148.72")
        self.assertEqual(following, "/quit")


class TestCompletenessCheck(unittest.TestCase):
    def setUp(self):
        self.check = make_completeness_check(make_agent())

    def test_bill_rows_are_fragments(self):
        for line in ["抖音电商商家   -148.72", "星巴克咖啡 -38.00", "10月14日13:11",
                     "2026-10-12 10:05 携程旅行网 -2480.00"]:
            with self.subTest(line=line):
                self.assertFalse(self.check(line))

    def test_normal_sentences_are_complete(self):
        for line in ["给老婆转2000块", "分析我这个月的账单", "你能做什么"]:
            with self.subTest(line=line):
                self.assertTrue(self.check(line))

    def test_slash_commands_are_complete(self):
        self.assertTrue(self.check("/quit"))
        self.assertTrue(self.check("/paste"))

    def test_bill_fragment_helper(self):
        self.assertTrue(looks_like_bill_fragment("抖音电商商家   -148.72"))
        self.assertTrue(looks_like_bill_fragment("10月14日13:11"))
        self.assertFalse(looks_like_bill_fragment("给老婆转2000块"))


class TestBillTemplate(unittest.TestCase):
    def test_template_file_imports_only_data_rows(self):
        from pathlib import Path
        template = Path(__file__).resolve().parent.parent / "samples" / "bill_template.txt"
        self.assertTrue(template.exists())
        records = parse_receipt_text(template.read_text(encoding="utf-8"), date(2026, 10, 14))
        self.assertEqual(len(records), 3)
        self.assertEqual([r.merchant for r in records],
                         ["携程旅行网", "星巴克咖啡", "滴滴出行"])

    def test_comment_lines_are_stripped(self):
        text = "# 携程旅行网 -9999.00\n2026-10-12 星巴克咖啡 -38.00\n"
        self.assertEqual(strip_comment_lines(text).count("携程"), 0)
        records = parse_receipt_text(text, date(2026, 10, 14))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].merchant, "星巴克咖啡")


if __name__ == "__main__":
    unittest.main()
