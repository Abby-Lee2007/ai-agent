"""自然语言理解层：把用户的一句话变成结构化意图 + 槽位。"""

from finagent.nlu.intent_parser import IntentParser, RuleIntentParser
from finagent.nlu.intent_schema import INTENTS, Intent, IntentSpec, SlotSpec, catalog_text
from finagent.nlu.llm_client import LLMClient

__all__ = [
    "INTENTS",
    "Intent",
    "IntentSpec",
    "SlotSpec",
    "catalog_text",
    "IntentParser",
    "RuleIntentParser",
    "LLMClient",
]
