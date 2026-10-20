"""finagent —— AI Banking Agent 核心包。

分层结构：
    nlu/      自然语言理解：意图识别 + 槽位抽取
    agent/    智能体编排：工具注册表、任务规划、对话状态机
    tools/    银行业务工具集：转账 / 账单 / 理财 / 卡片 / 订阅 / 跨场景
    risk/     风控引擎：限额、身份校验、异常识别、风险问卷与评分卡
    data/     数据层：领域模型、模拟数据、行情、内存 + JSON 持久化
    api/      HTTP/H5/IM 渠道适配层
    cli/      命令行交互前端
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让项目根目录始终在 import 路径上，这样 `import config` / `import finagent` 都能工作。
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import PROJECT_VERSION  # noqa: E402  (必须在路径处理之后导入)

__version__ = PROJECT_VERSION
__all__ = ["__version__"]

