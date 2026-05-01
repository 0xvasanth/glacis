from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    """Construct the Anthropic Claude chat model."""
    return ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        temperature=0,
        timeout=30,
        max_retries=2,
        stop=None,
    )
