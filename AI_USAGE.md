# AI usage

This project was built with AI help, as the brief allows. This page lists where AI was used and what was checked by hand. Accuracy is the point: nothing here claims a check that didn't happen.

**Tools I confirm:** _<edit me: e.g. "Claude Code (Claude Opus 5.5) only">_

## Claude Code (Anthropic CLI agent, model: Claude Opus 5.5)

| Area | What the AI did | How it was verified |
|---|---|---|
| Planning | Drafted `BUILD_PROMPT.md` (the master spec), its v2 changelog, and `PLAN.md` (architecture, data model, API contract, failure-mode table, residual risks). | I reviewed and approved them before any code was written. |
| Code | Wrote all application code: `app/`, `mocklab/`, `migrations/`, the SQL functions, Docker, compose, Makefile and CI. | `ruff`, `mypy --strict` and the test suite, all run inside Docker. |
| Tests | Wrote the unit, DB and integration tests. These cover a 20-way concurrent dedupe, a 30-way slot race, lab-commits-then-times-out reconciliation, lease expiry, the DLQ, and the outage/circuit-breaker path. | Run against real Postgres and the live compose stack. |
| Load test | Wrote `scripts/load_test.py`. It generates `LOAD_REPORT.md` from measured data, and the AI never edited the numbers. | I ran it. During development it showed 56 confirmed bookings against an expected maximum of 49. That traced to a mock-LLM bug: it silently searched another day. The mock was fixed and a regression test added. |
| Pricing research | Fetched the current published prices: the Anthropic and OpenAI pricing pages, Fly.io, Neon, and Render's docs. | The source URLs and fetch date are in `app/agent/llm/pricing.py` and `COST.md`. |
| Docs | Drafted the README, COST.md, DEPLOY.md, VIDEO_SCRIPT.md and this file. | Every number traces back to a run or a cited source. |

## Google Gemini (runtime LLM, not a development tool)

| Where | What |
|---|---|
| `app/agent/llm/openai_compat.py` (`LLM_PROVIDER=gemini`) | `gemini-3.1-flash-lite`, called through Gemini's OpenAI-compatible endpoint, is the booking agent's model when `LLM_MODE=real`. |
| `app/agent/llm/smoke.py` (`make smoke-real`) | 3 live calls: a plain completion and a `check_slots` tool round trip. |
| `scripts/demo_real_llm.py` (`make demo-real`) | 20 synthetic messages / 37 calls → `docs/REAL_LLM_DEMO.md` and `docs/REAL_LLM_DEMO_RERUN.md`. Billed ₹0 (free tier); paid-equivalent ₹0.97. |

Gemini was **not** used to write code, tests or docs. Tests and the 1,000-message load test always use the deterministic mock LLM. Only synthetic conversations were sent to Gemini; the free tier may use prompts to improve Google's products.

Claude Code also did the Gemini integration work:
- read Google's current docs (models, pricing, OpenAI compatibility, function calling, thought signatures);
- chose the model after a live 404 on `gemini-2.5-flash-lite`;
- ran the live smoke check and the demo;
- tuned the system prompt after reviewing the real transcripts (README, "What I learned from the real model").

## What the AI did not do

- It ran no git commands. I made every commit myself.
- It used no real patient data. All names and phone numbers are synthetic, and phones use the reserved `+91 99999 0XXXX` range.
- It put no API keys in the repo. Only `.env.example` with placeholders is committed.
- It never printed, logged or wrote the API key. It read the key only through the worker's environment; checks reported only counts and lengths.
- It made real LLM calls only for the smoke check and the demo (≤40 messages, budget-capped). See the README "LLM spend" section.
