"""监管口径风险问卷测试：题目结构、加权计分、C 级映射与产品匹配。"""
from __future__ import annotations

import unittest
from datetime import date

from tests.support import make_store
from finagent.risk.questionnaire import (
    QUESTIONS,
    QUESTION_MAP,
    evaluate,
    infer_answers,
    match_product,
)


class TestQuestionnaireStructure(unittest.TestCase):
    def test_question_bank_shape(self):
        self.assertEqual(len(QUESTIONS), 14)
        self.assertEqual(sum(q.max_score for q in QUESTIONS), 100)
        dimensions = {q.dimension for q in QUESTIONS}
        self.assertEqual(dimensions, {"财务状况", "投资经验", "风险态度", "投资期限", "流动性需求"})

    def test_every_question_has_options_and_scores(self):
        for question in QUESTIONS:
            with self.subTest(qid=question.qid):
                self.assertTrue(question.options)
                self.assertEqual(max(o.score for o in question.options), question.max_score)
                self.assertEqual(min(o.score for o in question.options), min(o.score for o in question.options))
                for option in question.options:
                    self.assertGreaterEqual(option.score, 0)
                    self.assertLessEqual(option.score, question.max_score)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.user = self.store.get_user("u_001")
        self.today = date(2026, 10, 20)

    def test_all_best_answers_reach_c5(self):
        best = {q.qid: max(q.options, key=lambda o: o.score).key for q in QUESTIONS}
        result = evaluate(best, self.user, self.store, self.today)
        self.assertEqual(result.total, 100)
        self.assertEqual(result.level, 5)
        self.assertEqual(result.label, "C5 进取型")

    def test_all_worst_answers_reach_c1(self):
        worst = {q.qid: min(q.options, key=lambda o: o.score).key for q in QUESTIONS}
        result = evaluate(worst, self.user, self.store, self.today)
        self.assertEqual(result.level, 1)
        self.assertEqual(result.label, "C1 保守型")

    def test_default_customer_is_c3(self):
        result = evaluate(user=self.user, store=self.store, today=self.today)
        self.assertEqual(result.level, 3)
        self.assertEqual(result.label, "C3 平衡型")
        self.assertGreaterEqual(result.total, 55)
        self.assertLessEqual(result.total, 72)

    def test_inferred_answers_cover_all_questions(self):
        answers = infer_answers(self.user, self.store, self.today)
        self.assertEqual(set(answers.keys()), set(QUESTION_MAP.keys()))

    def test_explicit_answers_override_inference(self):
        answers = infer_answers(self.user, self.store, self.today)
        answers["q10"] = "e"   # 50% 以上亏损容忍
        answers["q11"] = "d"
        result = evaluate(answers, self.user, self.store, self.today)
        self.assertEqual(result.answered, len(answers))
        detail = next(d for d in result.details if d.qid == "q10")
        self.assertFalse(detail.inferred)

    def test_partial_answers_are_inferred_for_the_rest(self):
        result = evaluate({"q1": "d"}, self.user, self.store, self.today)
        self.assertEqual(result.answered, 1)
        self.assertEqual(result.inferred, 13)
        q1 = next(d for d in result.details if d.qid == "q1")
        self.assertEqual(q1.score, 8)


class TestProductMatching(unittest.TestCase):
    def test_within_level_is_allowed(self):
        allowed, confirm, _ = match_product(3, 2)
        self.assertTrue(allowed)
        self.assertFalse(confirm)

    def test_one_level_above_needs_warning(self):
        allowed, confirm, reason = match_product(3, 4)
        self.assertTrue(allowed)
        self.assertTrue(confirm)
        self.assertIn("警示", reason)

    def test_two_levels_above_is_rejected(self):
        allowed, confirm, reason = match_product(2, 4)
        self.assertFalse(allowed)
        self.assertIn("禁止", reason)

    def test_c5_can_buy_r5(self):
        allowed, confirm, _ = match_product(5, 5)
        self.assertTrue(allowed)
        self.assertFalse(confirm)


if __name__ == "__main__":
    unittest.main()
