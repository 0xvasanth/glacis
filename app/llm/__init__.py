from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.core.config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    """Construct a LangChain BaseChatModel for the configured provider.

    Two backends are supported. Each handles the typed `NormalizedEvent`
    discriminated union differently — see Settings.llm_provider for the
    trade-off (Gemini is cheap but loosely enforces oneOf; Claude is the
    strict-schema choice).

    Imports are lazy so we don't pull both providers' SDKs when only one
    is used.
    """
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            temperature=0,
            timeout=30,
            max_retries=2,
            stop=None,
        )

    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.llm_model,
        google_api_key=settings.google_api_key,
        temperature=0,
        timeout=30,
        max_retries=2,
    )
