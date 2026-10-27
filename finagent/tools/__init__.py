"""银行业务工具集。

每个模块对应一个业务域，工具之间互不依赖，
新增业务只需"写一个 Tool 子类 + 注册"两步。
"""

from finagent.tools.account_tools import (
    AboutTool,
    BalanceTool,
    HelpTool,
    ProfileTool,
)
from finagent.tools.base import Tool, ToolContext, ToolParam, ToolResult, format_money, render_table
from finagent.tools.bill_tools import (
    BillAnalyzeTool,
    BillAnomalyTool,
    BillReportTool,
    BillSearchTool,
    BillSupportedFormatsTool,
    ReceiptImportTool,
    ReceiptScanTool,
)
from finagent.tools.card_tools import (
    CardApplyTool,
    CardBindTool,
    CardControlTool,
    CardFreezeTool,
    CardLimitAdjustTool,
    CardListTool,
    CardReportLostTool,
    CardUnlockTool,
)
from finagent.tools.crossscene_tools import (
    DailyBriefTool,
    GoalCreateTool,
    GoalListTool,
    ScheduleGiftTool,
)
from finagent.tools.membership_tools import MembershipPurchaseTool
from finagent.tools.subscription_tools import (
    SubscriptionCancelTool,
    SubscriptionDetectTool,
    SubscriptionListTool,
    SubscriptionRemindTool,
    SubscriptionResumeTool,
)
from finagent.tools.transfer_tools import (
    ListScheduledTransferTool,
    ScheduledTransferTool,
    SplitBillTool,
    TransferTool,
)
from finagent.tools.wealth_tools import (
    PortfolioTool,
    ProductRecommendTool,
    RiskAssessTool,
    SubscribeTool,
    WealthRedeemTool,
)

__all__ = [
    "Tool",
    "ToolContext",
    "ToolParam",
    "ToolResult",
    "format_money",
    "render_table",
    "TransferTool",
    "ScheduledTransferTool",
    "SplitBillTool",
    "ListScheduledTransferTool",
    "BillAnalyzeTool",
    "BillAnomalyTool",
    "BillReportTool",
    "BillSearchTool",
    "BillSupportedFormatsTool",
    "ReceiptScanTool",
    "ReceiptImportTool",
    "RiskAssessTool",
    "ProductRecommendTool",
    "SubscribeTool",
    "WealthRedeemTool",
    "PortfolioTool",
    "CardListTool",
    "CardApplyTool",
    "CardBindTool",
    "CardLimitAdjustTool",
    "CardControlTool",
    "CardFreezeTool",
    "CardUnlockTool",
    "CardReportLostTool",
    "SubscriptionDetectTool",
    "SubscriptionListTool",
    "SubscriptionCancelTool",
    "SubscriptionResumeTool",
    "SubscriptionRemindTool",
    "MembershipPurchaseTool",
    "GoalCreateTool",
    "GoalListTool",
    "ScheduleGiftTool",
    "DailyBriefTool",
    "BalanceTool",
    "ProfileTool",
    "HelpTool",
    "AboutTool",
]
