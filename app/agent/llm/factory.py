from app.agent.llm.anthropic_client import AnthropicLLM
from app.agent.llm.base import LLMClient
from app.agent.llm.mock import MockLLM
from app.agent.llm.openai_compat import OpenAICompatLLM
from app.agent.llm.pricing import price_for
from app.config import Settings

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class MissingAPIKey(RuntimeError):
    pass


class UnpricedModel(RuntimeError):
    pass


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_mode == "mock":
        return MockLLM(settings.mock_price_model)
    if not settings.llm_api_key:
        raise MissingAPIKey("LLM_MODE=real requires LLM_API_KEY (or API_KEY)")
    if price_for(settings.llm_model) is None:
        # The budget guard sums known costs; an unpriced model could spend without limit.
        raise UnpricedModel(f"add a published price for {settings.llm_model!r} to pricing.py first")
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(
            settings.llm_api_key, settings.llm_model, settings.llm_max_output_tokens, settings.llm_timeout_s
        )
    gemini = settings.llm_provider == "gemini"
    return OpenAICompatLLM(
        settings.llm_base_url or (GEMINI_BASE_URL if gemini else ""),
        settings.llm_api_key,
        settings.llm_model,
        settings.llm_max_output_tokens,
        settings.llm_timeout_s,
        gemini=gemini,
        max_retries=settings.llm_max_retries,
    )
