"""Actual real-LLM spend, read from the append-only llm_spend_ledger (survives load-test resets).

Recorded cost is the paid-tier list price ("paid-equivalent"). When LLM_BILLING_TIER=free and the model
has a documented free tier, the amount actually billed is $0.

Run: make spend
"""

import asyncio
import os

import psycopg

from app.agent.llm.pricing import FREE_TIER_MODELS


async def main() -> None:
    usd_inr = float(os.environ.get("USD_INR", "88"))
    tier = os.environ.get("LLM_BILLING_TIER", "paid")
    async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"]) as conn:
        cur = await conn.execute(
            "SELECT model, count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), "
            "coalesce(sum(cache_read_tokens),0), coalesce(sum(cost_usd),0), count(*) FILTER (WHERE cost_usd IS NULL), "
            "count(DISTINCT conversation_id), min(created_at), max(created_at) "
            "FROM llm_spend_ledger GROUP BY model ORDER BY model"
        )
        rows = await cur.fetchall()
    if not rows:
        print("Real LLM spend: $0.00 (₹0.00): no real LLM calls recorded in llm_spend_ledger.")
        return
    paid_total = billed_total = 0.0
    print(f"{'model':<24}{'calls':>6}{'convs':>6}{'in_tok':>9}{'out_tok':>9}{'cache_rd':>9}{'paid_eq_usd':>13}")
    for model, calls, i, o, cr, usd, unknown, convs, first, last in rows:
        paid = float(usd)
        billed = 0.0 if (tier == "free" and model in FREE_TIER_MODELS) else paid
        paid_total += paid
        billed_total += billed
        print(f"{model:<24}{calls:>6}{convs:>6}{i:>9}{o:>9}{cr:>9}{paid:>13.6f}")
        print(
            f"  {i / calls:.0f} input + {o / calls:.0f} output tokens per call; "
            f"first {first:%Y-%m-%d %H:%M} UTC, last {last:%Y-%m-%d %H:%M} UTC"
        )
        if unknown:
            print(f"  WARNING: {unknown} calls with unknown price (not included)")
    print(f"Billing tier: {tier}")
    print(f"Billed spend: ${billed_total:.6f} (₹{billed_total * usd_inr:.2f})")
    print(f"Paid-tier equivalent: ${paid_total:.6f} (₹{paid_total * usd_inr:.4f} at ₹{usd_inr}/$)")


if __name__ == "__main__":
    asyncio.run(main())
