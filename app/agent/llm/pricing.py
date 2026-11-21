"""Per-model prices in USD per million tokens, copied from the providers' published pages.

Sources (fetched 2026-11-20):
  Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
             (Haiku 5.5 row is the <=100K-token-prompt tier; our prompts are ~1-2K tokens)
  OpenAI:    https://developers.openai.com/api/docs/pricing  (standard tier, short context)
  Google:    https://ai.google.dev/gemini-api/docs/pricing -> https://aistudio.google.com/docs/pricing
             (fetched 2026-11-21; paid Standard tier, text; output price includes thinking tokens;
             caching storage $1.00/1M tokens/hour not modelled). These models also have a FREE tier
             ("Free of charge" input/output; free-tier content "used to improve our products").
An unknown model returns None: cost is recorded as unknown, never silently as 0.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.agent.llm.base import Usage


@dataclass(frozen=True, slots=True)
class Price:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # 5-minute cache write (Anthropic); OpenAI has no write surcharge


def _p(i: str, o: str, cr: str, cw: str) -> Price:
    return Price(Decimal(i), Decimal(o), Decimal(cr), Decimal(cw))


PRICES: dict[str, Price] = {
    # Anthropic
    "claude-haiku-5-5": _p("0.10", "0.50", "0.01", "0.125"),
    "claude-haiku-4-5": _p("1", "5", "0.10", "1.25"),
    "claude-sonnet-5-5": _p("2", "10", "0.10", "2.50"),
    "claude-opus-5-5": _p("4", "20", "0.20", "5"),
    # OpenAI (cache write billed as normal input)
    "gpt-5-nano": _p("0.05", "0.40", "0.005", "0.05"),
    "gpt-5-mini": _p("0.25", "2.00", "0.025", "0.25"),
    "gpt-4.1-mini": _p("0.40", "1.60", "0.10", "0.40"),
    "gpt-4o-mini": _p("0.15", "0.60", "0.075", "0.15"),
    # Google Gemini (paid-tier list price; cache write billed as normal input)
    "gemini-2.5-flash-lite": _p("0.10", "0.40", "0.01", "0.10"),
    "gemini-3.1-flash-lite": _p("0.25", "1.50", "0.025", "0.25"),
    "gemini-3.5-flash-lite": _p("0.30", "2.50", "0.03", "0.30"),
}
# Models with a documented free tier. Recorded cost is always the paid-equivalent (so the budget guard
# is conservative); `LLM_BILLING_TIER=free` only changes what spend_report calls "billed".
FREE_TIER_MODELS = {"gemini-2.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"}
PRICES_FETCHED = "2026-11-20"
MTOK = Decimal(1_000_000)


def price_for(model: str) -> Price | None:
    return PRICES.get(model.removeprefix("mock:"))


def cost_usd(model: str, usage: Usage) -> Decimal | None:
    price = price_for(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / MTOK
