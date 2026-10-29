"""全局配置。

把限额、风控阈值、LLM 接入参数、数据目录集中在这里，
后续要调整风控策略或切换大模型时，只需要改这一个文件。
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量，解析失败时回退到默认值。"""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


# --------------------------------------------------------------------------
# 应用元信息
# --------------------------------------------------------------------------
APP_NAME = "AI Banking Agent"
APP_SUBTITLE = "2026 FinTechathon 深圳国际金融科技大赛 · 人工智能国内赛道"
PROJECT_VERSION = "0.2.0"

DISCLAIMER = (
    "本系统为赛事原型 Demo：所有账户、交易、理财与卡片数据均为模拟数据，"
    "不接入任何真实银行系统、征信或资金接口，不可用于真实银行业务。"
)

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.getenv("FINAGENT_RUNTIME_DIR", str(BASE_DIR / "runtime_data")))
STORE_FILE = RUNTIME_DIR / "store.json"
DB_FILE = Path(os.getenv("FINAGENT_DB_PATH", str(RUNTIME_DIR / "finagent.db")))
OFFICIAL_DATA_FILE = RUNTIME_DIR / "official_data.json"
OFFICIAL_SNAPSHOT_FILE = BASE_DIR / "data" / "official_data_snapshot.json"

# 默认演示用户（Demo 内置的模拟客户）
DEFAULT_USER_ID = "u_001"

# --------------------------------------------------------------------------
# LLM 接入参数（任何 OpenAI 兼容接口都可用：DeepSeek / 通义 / 智谱 / OpenAI ...）
# --------------------------------------------------------------------------


class LLMConfig:
    """底层语言理解模型的接入配置。

    mode 说明：
      - auto：有 API Key 就用大模型，没有就回退到内置规则解析器；
      - on  ：强制使用大模型（没有 Key 会给出明确报错提示）；
      - off ：强制使用内置规则解析器（离线演示、断网路演时使用）。
    """

    MODE = os.getenv("FINAGENT_LLM_MODE", "auto").strip().lower()
    API_KEY = os.getenv("FINAGENT_LLM_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    BASE_URL = os.getenv("FINAGENT_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    MODEL = os.getenv("FINAGENT_LLM_MODEL", "deepseek-chat")
    TIMEOUT = _env_float("FINAGENT_LLM_TIMEOUT", 20.0)
    TEMPERATURE = _env_float("FINAGENT_LLM_TEMPERATURE", 0.0)
    MAX_TOKENS = int(_env_float("FINAGENT_LLM_MAX_TOKENS", 1024))
    TOOL_CALLING = _env_bool("FINAGENT_LLM_TOOL_CALLING", True)

    @classmethod
    def enabled(cls) -> bool:
        """当前是否会真正调用大模型。"""
        if cls.MODE == "off":
            return False
        if cls.MODE == "on":
            return True
        return bool(cls.API_KEY)


class LicensedMarketConfig:
    """Optional licensed market-data gateway.

    The adapter expects a REST gateway that normalizes its licensed provider
    response into the fields documented in finagent/data/licensed_market.py.
    """

    URL = os.getenv("FINAGENT_LICENSED_MARKET_URL", "").strip()
    API_KEY = os.getenv("FINAGENT_LICENSED_MARKET_KEY", "").strip()
    NAME = os.getenv("FINAGENT_LICENSED_MARKET_NAME", "licensed-market").strip()
    TIMEOUT = _env_float("FINAGENT_LICENSED_MARKET_TIMEOUT", 8.0)


# --------------------------------------------------------------------------
# 风控策略参数
# --------------------------------------------------------------------------


class RiskPolicy:
    """金融风控阈值。

    这里的数字都刻意做得"原型可演示"而不是生产精确值，
    评委/老师可以直观看到触发二次确认、身份校验、拦截的分界点。
    """

    # 单笔转账上限（元）
    SINGLE_TRANSFER_LIMIT = _env_float("FINAGENT_SINGLE_TRANSFER_LIMIT", 50_000.0)
    # 单日累计转账上限（元）
    DAILY_TRANSFER_LIMIT = _env_float("FINAGENT_DAILY_TRANSFER_LIMIT", 200_000.0)
    # 首次交易的新收款人单笔上限（元）
    NEW_PAYEE_LIMIT = _env_float("FINAGENT_NEW_PAYEE_LIMIT", 5_000.0)
    # 新收款人较大金额进入冷静期，需等待后再次确认
    NEW_PAYEE_COOLING_THRESHOLD = _env_float(
        "FINAGENT_NEW_PAYEE_COOLING_THRESHOLD",
        3_000.0,
    )
    NEW_PAYEE_COOLING_MINUTES = int(
        _env_float("FINAGENT_NEW_PAYEE_COOLING_MINUTES", 10)
    )
    # 超过该金额需要二次确认
    CONFIRM_THRESHOLD = _env_float("FINAGENT_CONFIRM_THRESHOLD", 1_000.0)
    # 常用收款人（通讯录已有）的单笔确认门槛更宽松
    TRUSTED_CONFIRM_THRESHOLD = _env_float("FINAGENT_TRUSTED_CONFIRM_THRESHOLD", 5_000.0)
    # 超过该金额需要身份验证（模拟短信验证码）
    VERIFY_THRESHOLD = _env_float("FINAGENT_VERIFY_THRESHOLD", 20_000.0)
    # 夜间时段（小时），该时段大额交易进入人工确认
    NIGHT_START_HOUR = 0
    NIGHT_END_HOUR = 5
    # 高频转账判定：N 分钟内同账户转出超过 M 笔
    RAPID_TRANSFER_WINDOW_MINUTES = 10
    RAPID_TRANSFER_COUNT = 3
    # 异常交易识别：金额偏离同类别均值多少倍标准差算异常
    ANOMALY_ZSCORE = _env_float("FINAGENT_ANOMALY_ZSCORE", 3.0)
    # 重复扣款判定窗口（分钟）
    DUPLICATE_WINDOW_MINUTES = 10
    # 演示用短信验证码（原型阶段固定值）
    DEMO_SMS_CODE = "123456"
    # 赛事原型免责声明
    DISCLAIMER = DISCLAIMER


# --------------------------------------------------------------------------
# 交互与演示
# --------------------------------------------------------------------------
class UIConfig:
    """命令行交互的展示配置。"""

    USE_COLOR = _env_bool("FINAGENT_COLOR", True)
    SHOW_TRACE = _env_bool("FINAGENT_SHOW_TRACE", True)
    MAX_HISTORY_TURNS = 40

