"""工具注册表。

所有银行业务工具在这里登记，调度器只通过名字找工具，
因此新增业务时不需要改动调度逻辑。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from finagent.tools import (
    AboutTool,
    AAPaymentStatusTool,
    AccountReconcileTool,
    BalanceTool,
    BillAnalyzeTool,
    BillAnomalyTool,
    BillReportTool,
    BillSearchTool,
    BillSupportedFormatsTool,
    CardApplyTool,
    CardBindTool,
    CardControlTool,
    CardFreezeTool,
    CardLimitAdjustTool,
    CardListTool,
    CardReportLostTool,
    CardStatementTool,
    CardTimelineTool,
    CardUnlockTool,
    CashflowForecastTool,
    EventCenterTool,
    FinancialHealthTool,
    CreditCardRepayTool,
    DailyBriefTool,
    GiftRecommendTool,
    GoalCreateTool,
    GoalListTool,
    HelpTool,
    InternalTransferTool,
    ListScheduledTransferTool,
    MembershipPurchaseTool,
    MarketOverviewTool,
    OfficialMacroTool,
    PortfolioTool,
    ProductCompareTool,
    ProactiveBriefTool,
    ProductRecommendTool,
    ProfileTool,
    ReceiptImportTool,
    ReceiptScanTool,
    ReconcileAdjustTool,
    RiskAssessTool,
    RiskDisputeTool,
    RiskPlaybookTool,
    ScheduleGiftTool,
    ScheduledTransferTool,
    SplitBillTool,
    SubscribeTool,
    SubscriptionCancelTool,
    SubscriptionChangeCycleTool,
    SubscriptionDetectTool,
    SubscriptionListTool,
    SubscriptionRemindTool,
    SubscriptionResumeTool,
    Tool,
    TransferTool,
    WealthAnalyticsTool,
    WealthRedeemTool,
)


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("工具必须有 name")
        self._tools[tool.name] = tool

    def register_all(self, tools: List[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def by_category(self) -> Dict[str, List[Tool]]:
        grouped: Dict[str, List[Tool]] = {}
        for tool in self._tools.values():
            grouped.setdefault(tool.category, []).append(tool)
        return grouped

    def schemas(self) -> List[Dict]:
        return [tool.to_schema() for tool in self._tools.values()]


def build_default_registry() -> ToolRegistry:
    """注册全部内置工具。新增业务在这里加一行即可。"""
    registry = ToolRegistry()
    registry.register_all(
        [
            # 账户服务
            BalanceTool(),
            ProfileTool(),
            HelpTool(),
            AboutTool(),
            AccountReconcileTool(),
            # 智能转账
            TransferTool(),
            InternalTransferTool(),
            ScheduledTransferTool(),
            SplitBillTool(),
            ListScheduledTransferTool(),
            AAPaymentStatusTool(),
            # 账单分析
            BillSupportedFormatsTool(),
            BillAnalyzeTool(),
            BillAnomalyTool(),
            BillReportTool(),
            BillSearchTool(),
            ReceiptScanTool(),
            ReceiptImportTool(),
            # 理财操作
            RiskAssessTool(),
            ProductRecommendTool(),
            ProductCompareTool(),
            SubscribeTool(),
            WealthRedeemTool(),
            PortfolioTool(),
            WealthAnalyticsTool(),
            # 卡片管理
            CardListTool(),
            CardApplyTool(),
            CardBindTool(),
            CreditCardRepayTool(),
            CardLimitAdjustTool(),
            CardControlTool(),
            CardFreezeTool(),
            CardUnlockTool(),
            CardReportLostTool(),
            CardStatementTool(),
            CardTimelineTool(),
            # 订阅管理
            SubscriptionDetectTool(),
            SubscriptionListTool(),
            SubscriptionCancelTool(),
            SubscriptionResumeTool(),
            SubscriptionRemindTool(),
            MembershipPurchaseTool(),
            SubscriptionChangeCycleTool(),
            # 跨场景联动
            GoalCreateTool(),
            GoalListTool(),
            GiftRecommendTool(),
            ScheduleGiftTool(),
            DailyBriefTool(),
            CashflowForecastTool(),
            FinancialHealthTool(),
            MarketOverviewTool(),
            OfficialMacroTool(),
            EventCenterTool(),
            ProactiveBriefTool(),
            RiskPlaybookTool(),
            RiskDisputeTool(),
            ReconcileAdjustTool(),
        ]
    )
    return registry

