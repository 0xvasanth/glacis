from __future__ import annotations

from typing import Any, Protocol

import orjson
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.events import NormalizedEvent
from app.llm.prompts import SYSTEM_PROMPT


class SupportsClassify(Protocol):
    async def classify(self, payload: dict[str, Any]) -> NormalizedEvent: ...


class Classifier:
    """LangChain-backed normalization client.

    The LLM contract is `NormalizedEvent` itself: an envelope (vendor /
    event_at / confidence) wrapping a typed `payload` discriminated by
    `canonical_state`. Pydantic enforces required fields per state — if
    the LLM omits one, validation fires with "missing field X for state
    Y" and the worker parks the row in the DLQ.
    """

    def __init__(self, llm: BaseChatModel):
        self._structured = llm.with_structured_output(NormalizedEvent)

    async def classify(self, payload: dict[str, Any]) -> NormalizedEvent:
        user = HumanMessage(content=orjson.dumps(payload, option=orjson.OPT_SORT_KEYS).decode())
        result = await self._structured.ainvoke([SystemMessage(content=SYSTEM_PROMPT), user])
        if isinstance(result, NormalizedEvent):
            return result
        return NormalizedEvent.model_validate(result)
