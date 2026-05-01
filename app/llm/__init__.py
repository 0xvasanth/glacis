from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from app.core.config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    """Construct a LangChain BaseChatModel for Google Gemini."""
    return ChatGoogleGenerativeAI(
        model=settings.llm_model,
        google_api_key=settings.google_api_key,
        temperature=0,
        timeout=30,
        max_retries=2,
    )
