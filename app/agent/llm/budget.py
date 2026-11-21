"""Usage recording and the real-spend budget guard."""

from decimal import Decimal

from app.agent.llm.base import LLMResponse
from app.agent.llm.pricing import cost_usd
from app.db import Pool


class BudgetExceeded(Exception):
    pass


async def real_spend_usd(pool: Pool) -> Decimal:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT coalesce(sum(cost_usd), 0) AS s FROM llm_spend_ledger")
        row = await cur.fetchone()
        assert row is not None
        return Decimal(row["s"])


async def check_budget(pool: Pool, mode: str, budget_usd: float) -> None:
    """Refuse a real call once recorded real spend reaches the cap. Mock calls are never limited."""
    if mode != "real":
        return
    if await real_spend_usd(pool) >= Decimal(str(budget_usd)):
        raise BudgetExceeded(f"real LLM spend reached LLM_BUDGET_USD={budget_usd}")


async def record_usage(pool: Pool, conversation_id: int | None, mode: str, resp: LLMResponse) -> Decimal | None:
    cost = cost_usd(resp.model, resp.usage)
    u = resp.usage
    row = (
        conversation_id,
        resp.model,
        u.input_tokens,
        u.output_tokens,
        u.cache_read_tokens,
        u.cache_write_tokens,
        cost,
    )
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO llm_usage (conversation_id, mode, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (row[0], mode, *row[1:]),
        )
        if mode == "real":
            await conn.execute(
                "INSERT INTO llm_spend_ledger (conversation_id, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                row,
            )
    return cost
