"""账单分析工具集。

覆盖赛题要求：
- 消费分类统计；
- 异常交易识别（委托给风控引擎，保证规则只有一份）；
- 月度 / 年度账单报告。
"""
from __future__ import annotations

import calendar
from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from finagent.data.models import Transaction
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, format_money, render_table
from finagent.utils import parse_period, period_bounds
from finagent.vision.receipt_reader import ReceiptReader

LEVEL_ICON = {"danger": "高危", "warning": "关注", "info": "提示"}


def _previous_bounds(period: str, today: date) -> Tuple[str, str, str]:
    """计算环比用的上一期区间。

    注意：本月才过了半个月时，要和"上月同期"比才有意义，
    所以月度类期间按天对齐，而不是直接拿整个上月来比。
    """
    start, end, label = period_bounds(period, today)
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    def month_shift(anchor: date, months: int) -> Tuple[int, int]:
        total = anchor.year * 12 + (anchor.month - 1) + months
        return total // 12, total % 12 + 1

    if period in {"this_month", "last_month"}:
        if period == "this_month":
            base_year, base_month = today.year, today.month
        else:
            base_year, base_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        year, month = month_shift(date(base_year, base_month, 1), -1)
        last_day = calendar.monthrange(year, month)[1]
        day = min(today.day, last_day)
        return (date(year, month, 1).isoformat(), date(year, month, day).isoformat(),
                f"上月同期（{month}月1-{day}日）")

    if period in {"this_year", "last_year"}:
        year_offset = -1 if period == "this_year" else -2
        prev_start = date(today.year + year_offset, 1, 1)
        try:
            prev_end = date(today.year + year_offset, today.month, today.day)
        except ValueError:
            prev_end = date(today.year + year_offset, today.month, 28)
        return prev_start.isoformat(), prev_end.isoformat(), "去年同期"

    span = (end_d - start_d).days + 1
    prev_end = start_d - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return prev_start.isoformat(), prev_end.isoformat(), f"上一期（{label}）"


def _summarize(txns: List[Transaction]) -> Dict[str, Any]:
    """收支总览。"""
    income = sum(t.amount for t in txns if t.amount > 0)
    expense = sum(-t.amount for t in txns if t.amount < 0)
    return {
        "income": round(income, 2),
        "expense": round(expense, 2),
        "net": round(income - expense, 2),
        "count": len(txns),
        "avg": round(expense / len([t for t in txns if t.amount < 0] or [1]), 2),
    }


def _category_table(txns: List[Transaction], top: int = 8) -> Tuple[List[List[str]], Dict[str, float]]:
    """按类别汇总消费。"""
    totals: Dict[str, float] = defaultdict(float)
    counts: Dict[str, int] = defaultdict(int)
    for t in txns:
        if t.amount < 0 and t.category not in {"转账", "理财"}:
            totals[t.category] += -t.amount
            counts[t.category] += 1
    total_expense = sum(totals.values()) or 1.0
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]
    rows = []
    for category, value in ordered:
        ratio = value / total_expense * 100
        rows.append([category, f"{value:,.2f}", f"{ratio:.1f}%", str(counts[category]), "=" * max(int(ratio / 4), 1)])
    return rows, totals


def _top_merchants(txns: List[Transaction], top: int = 5) -> List[List[str]]:
    totals: Dict[str, float] = defaultdict(float)
    counts: Dict[str, int] = defaultdict(int)
    for t in txns:
        if t.amount < 0:
            totals[t.merchant] += -t.amount
            counts[t.merchant] += 1
    ordered = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [[m, f"{v:,.2f}", str(counts[m])] for m, v in ordered]


class BillAnalyzeTool(Tool):
    """消费分类统计。"""

    name = "bill.analyze"
    category = "账单分析"
    description = "统计指定期间的收支总额、消费分类占比与高频商户"
    risk_level = "low"
    params = [
        ToolParam("period", "string", False, "统计期间：本月/上月/今年/去年/最近7天/最近3个月/2026-09",
                  default="this_month"),
        ToolParam("category", "string", False, "只看某个类别，如 餐饮"),
    ]
    examples = ["分析我这个月的账单", "上个月餐饮花了多少"]

    def run(self, ctx: ToolContext, period: str = "this_month", category: str = "") -> ToolResult:  # type: ignore[override]
        today = ctx.clock.today()
        period_key = parse_period(str(period), today)
        start, end, label = period_bounds(period_key, today)
        txns = ctx.store.transactions_of(ctx.user_id, start, end)
        if category:
            txns = [t for t in txns if category in t.category or category in t.merchant]
        if not txns:
            return ToolResult(True, f"{label}没有找到交易记录。", data={"count": 0}, summary=f"{label}无交易")

        stats = _summarize(txns)
        rows, _totals = _category_table(txns)
        prev_start, prev_end, _ = _previous_bounds(period_key, today)
        prev_stats = _summarize(ctx.store.transactions_of(ctx.user_id, prev_start, prev_end))
        delta = (
            (stats["expense"] - prev_stats["expense"]) / prev_stats["expense"] * 100
            if prev_stats["expense"]
            else 0.0
        )
        trend = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")

        lines = [
            f"{label}账单概览（{start} ~ {end}，共 {stats['count']} 笔）",
            f"  收入 {stats['income']:,.2f} 元 | 支出 {stats['expense']:,.2f} 元 | 结余 {stats['net']:,.2f} 元",
            f"  单笔平均支出 {stats['avg']:,.2f} 元 | 环比上月支出{trend} {abs(delta):.1f}%",
            "",
            "消费分类排行：",
            render_table(["类别", "金额", "占比", "笔数", "分布"], rows) if rows else "  （本期没有消费类支出）",
            "",
            "高频商户 Top5：",
            render_table(["商户", "金额", "笔数"], _top_merchants(txns)),
        ]
        return ToolResult(
            True,
            "\n".join(lines),
            data={"period": period_key, "label": label, "stats": stats, "start": start, "end": end},
            summary=f"{label}支出 {stats['expense']:,.2f} 元，结余 {stats['net']:,.2f} 元",
        )


class BillAnomalyTool(Tool):
    """异常交易识别。"""

    name = "bill.anomaly"
    category = "账单分析"
    description = "识别异常/可疑交易：深夜大额、重复扣款、金额离群、订阅涨价、境外消费等"
    risk_level = "low"
    params = [
        ToolParam("period", "string", False, "检查期间，默认最近 30 天", default="last_30_days"),
    ]
    examples = ["帮我看看有没有异常交易", "最近有没有被盗刷"]

    def run(self, ctx: ToolContext, period: str = "last_30_days") -> ToolResult:  # type: ignore[override]
        today = ctx.clock.today()
        period_key = parse_period(str(period), today)
        start, end, label = period_bounds(period_key, today)
        txns = ctx.store.transactions_of(ctx.user_id, start, end)
        anomalies = ctx.risk.detect_anomalies(ctx.user_id, txns)
        if not anomalies:
            return ToolResult(
                True,
                f"{label}未发现异常交易，账单看起来是正常的。",
                data={"count": 0},
                summary=f"{label}未发现异常交易",
            )
        rows = [
            [LEVEL_ICON.get(a.level, a.level), a.date, a.merchant, format_money(a.amount), a.reason[:38]]
            for a in anomalies
        ]
        lines = [
            f"{label}共发现 {len(anomalies)} 笔值得注意的交易：",
            render_table(["级别", "日期", "商户", "金额", "原因"], rows),
            "",
            "处理建议：",
        ]
        for idx, a in enumerate(anomalies[:5], start=1):
            lines.append(f"  {idx}. [{LEVEL_ICON.get(a.level, a.level)}] {a.merchant}：{a.suggestion}")
        # 高危异常同时落一条提醒，体现"主动风控"
        for a in anomalies:
            if a.level == "danger":
                ctx.store.add_alert(
                    ctx.user_id, "danger", f"疑似盗刷：{a.merchant}", a.reason, source="bill.anomaly"
                )
        return ToolResult(
            True,
            "\n".join(lines),
            data={"count": len(anomalies), "items": [a.__dict__ for a in anomalies]},
            summary=f"{label}发现 {len(anomalies)} 笔异常交易",
        )


class BillReportTool(Tool):
    """月度 / 年度账单报告。"""

    name = "bill.report"
    category = "账单分析"
    description = "生成月度或年度账单报告，含收支总览、分类结构、异常提示与省钱建议"
    risk_level = "low"
    params = [
        ToolParam("period", "string", False, "报告期间：本月/上月/今年/去年", default="this_month"),
        ToolParam("mode", "string", False, "报告类型", enum=["monthly", "annual"], default="monthly"),
    ]
    examples = ["生成我的年度账单报告", "帮我出一份本月账单"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        period: str = "this_month",
        mode: str = "monthly",
    ) -> ToolResult:
        today = ctx.clock.today()
        period_key = parse_period(str(period), today)
        if mode == "annual" and period_key in {"this_month", "last_month"}:
            period_key = "this_year"
        start, end, label = period_bounds(period_key, today)
        txns = ctx.store.transactions_of(ctx.user_id, start, end)
        if not txns:
            return ToolResult(True, f"{label}没有交易记录，无法生成报告。", summary=f"{label}无数据")

        stats = _summarize(txns)
        rows, totals = _category_table(txns, top=10)
        anomalies = ctx.risk.detect_anomalies(ctx.user_id, txns)
        top_category = max(totals.items(), key=lambda kv: kv[1])[0] if totals else "-"
        income = stats["income"] or 1.0
        saving_rate = stats["net"] / income * 100

        lines = [
            f"【{label}账单报告】{start} ~ {end}",
            "=" * 46,
            "一、收支总览",
            f"  收入 {stats['income']:,.2f} 元",
            f"  支出 {stats['expense']:,.2f} 元",
            f"  结余 {stats['net']:,.2f} 元（储蓄率 {saving_rate:.1f}%）",
            f"  交易笔数 {stats['count']} 笔，单笔平均支出 {stats['avg']:,.2f} 元",
            "",
            "二、消费结构",
            render_table(["类别", "金额", "占比", "笔数", "分布"], rows),
            "",
            "三、高频商户 Top5",
            render_table(["商户", "金额", "笔数"], _top_merchants(txns)),
            "",
            "四、异常与风险提示",
        ]
        if anomalies:
            for a in anomalies[:6]:
                lines.append(f"  - [{LEVEL_ICON.get(a.level, a.level)}] {a.date} {a.merchant} "
                             f"{format_money(a.amount)} 元：{a.reason}")
        else:
            lines.append("  - 本期未发现异常交易。")

        lines.extend(["", "五、Agent 建议"])
        if mode == "annual":
            monthly = defaultdict(float)
            for t in txns:
                if t.amount < 0:
                    monthly[t.timestamp[:7]] += -t.amount
            ordered = sorted(monthly.items())
            lines.append("  月度支出趋势：")
            lines.append(render_table(["月份", "支出"], [[m, f"{v:,.2f}"] for m, v in ordered]))
            if ordered:
                peak = max(ordered, key=lambda kv: kv[1])
                lines.append(f"  支出最高的月份是 {peak[0]}（{peak[1]:,.2f} 元），建议复盘当月大额支出。")
        lines.append(f"  本期最大支出类别是「{top_category}」，"
                     f"如果下期压降 10%，一年可多存约 {totals.get(top_category, 0) * 0.1 * 12:,.0f} 元。")
        if stats["net"] > 0:
            lines.append(f"  本期结余 {stats['net']:,.2f} 元，可以考虑转入「微众活期+」或做一笔指数基金定投。")
        else:
            lines.append("  本期支出超过收入，建议先暂停非必要订阅与冲动消费，我可以帮你列出可取消的订阅。")

        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "period": period_key,
                "label": label,
                "stats": stats,
                "top_category": top_category,
                "anomaly_count": len(anomalies),
            },
            summary=f"{label}支出 {stats['expense']:,.2f} 元，结余 {stats['net']:,.2f} 元",
        )


class BillSearchTool(Tool):
    """按商户/关键词检索消费。"""

    name = "bill.search"
    category = "账单分析"
    description = "按商户名或关键词检索消费记录并汇总金额"
    risk_level = "low"
    params = [
        ToolParam("keyword", "string", True, "商户名或关键词，如 星巴克/咖啡/打车"),
        ToolParam("period", "string", False, "检索期间，默认最近 3 个月", default="last_3_months"),
    ]
    examples = ["我今年在星巴克花了多少钱", "查一下咖啡消费"]

    def run(  # type: ignore[override]
        self,
        ctx: ToolContext,
        keyword: str,
        period: str = "last_3_months",
    ) -> ToolResult:
        today = ctx.clock.today()
        period_key = parse_period(str(period), today)
        start, end, label = period_bounds(period_key, today)
        txns = [
            t for t in ctx.store.transactions_of(ctx.user_id, start, end)
            if keyword in t.merchant or keyword in t.category or keyword in t.note
        ]
        if not txns:
            return ToolResult(True, f"{label}没有找到与「{keyword}」相关的消费。", summary=f"未找到「{keyword}」消费")
        total = sum(-t.amount for t in txns if t.amount < 0)
        rows = [[t.timestamp[:10], t.merchant, format_money(t.amount), t.category] for t in txns[:15]]
        lines = [
            f"{label}与「{keyword}」相关的交易共 {len(txns)} 笔，合计支出 {total:,.2f} 元",
            render_table(["日期", "商户", "金额", "类别"], rows),
        ]
        if len(txns) > 15:
            lines.append(f"  （仅展示最近 15 笔，共 {len(txns)} 笔）")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"count": len(txns), "total": round(total, 2)},
            summary=f"「{keyword}」共消费 {total:,.2f} 元（{len(txns)} 笔）",
        )


RECEIPT_CACHE_KEY = "receipt_cache"


class ReceiptScanTool(Tool):
    """账单图片识别：把小票/账单截图变成结构化交易。"""

    name = "bill.scan_receipt"
    category = "账单分析"
    description = "识别账单/小票截图、账单文件，或直接解析粘贴的账单文本"
    risk_level = "low"
    params = [
        ToolParam("image", "string", False, "账单图片或账单文件的路径"),
        ToolParam("text", "string", False, "直接粘贴的账单文本"),
    ]
    required_any = ["image", "text"]
    examples = ["识别这张账单截图 samples/bill_sample.png",
                "帮我看一下这张小票 C:/Users/me/Desktop/bill.jpg",
                "帮我分析一下我粘贴的账单"]

    def missing_params(self, args: Dict[str, Any]) -> List[str]:
        """图片和文本二选一即可。"""
        if args.get("image") or args.get("text"):
            return []
        return ["image"]

    def run(self, ctx: ToolContext, image: str = "", text: str = "") -> ToolResult:  # type: ignore[override]
        reader = ReceiptReader(llm=ctx.session.get("llm"))
        if text and not image:
            result = reader.read_text(text, ctx.clock.today())
        else:
            result = reader.read(image, ctx.clock.today())
        if not result.ok or not result.records:
            return ToolResult(False, result.message or "没有识别到交易明细。")

        rows = [
            [record.date, record.time or "-", record.merchant,
             format_money(record.amount), record.category]
            for record in result.records
        ]
        source_name = Path(result.image_path).name if result.image_path else "粘贴的账单文本"
        expense_count = len([r for r in result.records if r.amount < 0])
        lines = [
            f"已识别「{source_name}」中的 {len(result.records)} 笔交易"
            f"（识别方式：{result.backend_label}）：",
            render_table(["日期", "时间", "商户", "金额", "类别"], rows),
            f"  合计支出 {result.total_expense:,.2f} 元（{expense_count} 笔支出）",
        ]
        low_confidence = [r for r in result.records if r.confidence < 0.8]
        if low_confidence:
            lines.append(f"  提示：其中 {len(low_confidence)} 笔由离线 OCR 推断，"
                         f"如有偏差请核对原始账单。")

        ctx.session[RECEIPT_CACHE_KEY] = {
            "source": result.image_path,
            "backend": result.backend,
            "backend_label": result.backend_label,
            "records": [record.to_payload() for record in result.records],
        }
        return ToolResult(
            True,
            "\n".join(lines),
            data={
                "count": len(result.records),
                "total_expense": result.total_expense,
                "backend": result.backend,
                "follow_up": {
                    "tool": "bill.import_receipt",
                    "args": {},
                    "intent": "bill.import_receipt",
                    "description": (f"把刚识别出的 {len(result.records)} 笔交易导入流水"
                                    f"（{result.total_expense:,.2f} 元），"
                                    f"之后就能在账单分析与异常识别里看到它们"),
                },
            },
            summary=f"识别到 {len(result.records)} 笔交易，合计支出 {result.total_expense:,.2f} 元",
        )


class ReceiptImportTool(Tool):
    """把识别结果写入流水（由 bill.scan_receipt 的后续确认触发）。"""

    name = "bill.import_receipt"
    category = "账单分析"
    description = "把上一步识别出的账单交易写入流水，供账单分析与异常识别使用"
    risk_level = "medium"
    params = []
    examples = []

    def run(self, ctx: ToolContext) -> ToolResult:  # type: ignore[override]
        cache = ctx.session.get(RECEIPT_CACHE_KEY)
        if not cache or not cache.get("records"):
            return ToolResult(False, "没有待导入的识别结果，请先让我识别一张账单截图。")

        existing = {
            (t.timestamp[:16], t.merchant, round(t.amount, 2))
            for t in ctx.store.transactions_of(ctx.user_id)
        }
        account = ctx.store.primary_account(ctx.user_id)
        imported: List[Transaction] = []
        skipped = 0
        for record in cache["records"]:
            stamp = f"{record['date']}T{(record.get('time') or '12:00')}:00"[:16]
            key = (stamp, record["merchant"], round(float(record["amount"]), 2))
            if key in existing:
                skipped += 1
                continue
            source = Path(cache["source"]).name if cache.get("source") else "粘贴的账单文本"
            txn = ctx.store.add_transaction(
                user_id=ctx.user_id,
                account_id=account.account_id if account else "",
                amount=float(record["amount"]),
                merchant=record["merchant"],
                category=record.get("category") or "其他",
                channel="账单导入",
                note=f"来源：{source}",
                timestamp=f"{record['date']}T{record.get('time') or '12:00'}:00",
                tags=["receipt_import", cache.get("backend", "")],
            )
            imported.append(txn)
            existing.add(key)

        ctx.session.pop(RECEIPT_CACHE_KEY, None)
        anomalies = (
            ctx.risk.detect_anomalies(ctx.user_id, imported, include_subscriptions=False)
            if imported else []
        )
        lines = [
            f"已导入 {len(imported)} 笔流水"
            + (f"，跳过 {skipped} 笔重复记录" if skipped else "")
            + f"（识别来源：{cache.get('backend_label', cache.get('backend', ''))}）。",
            "  说明：导入的是账单明细补录，不会重复扣减账户余额。",
        ]
        if anomalies:
            rows = [[a.date, a.merchant, format_money(a.amount), a.reason[:32]] for a in anomalies]
            lines.append("")
            lines.append(f"导入后立即发现 {len(anomalies)} 笔异常交易：")
            lines.append(render_table(["日期", "商户", "金额", "原因"], rows))
            lines.append("  可以把卡片先冻结，或让我发起交易争议处理。")
        else:
            lines.append("  这批发现在账单里没有异常特征。")
        return ToolResult(
            True,
            "\n".join(lines),
            data={"imported": len(imported), "skipped": skipped,
                  "anomalies": len(anomalies),
                  "txn_ids": [t.txn_id for t in imported]},
            summary=f"已导入 {len(imported)} 笔账单流水，发现 {len(anomalies)} 笔异常",
        )

