"""Live LLM smoke check: at most 3 calls. Run inside the worker container (the only one holding the key):

    docker compose run --rm --no-deps -e LLM_MODE=real worker python -m app.agent.llm.smoke [--simplify-schema]

1) plain completion, 2) a check_slots tool call, 3) the tool result sent back for a final reply.
Prints outcomes and token usage only, never the key or headers.
"""

import argparse
import asyncio
import json
import sys
from datetime import date

from app.agent.llm.anthropic_client import AnthropicLLM
from app.agent.llm.base import LLMClient, LLMError, Message
from app.agent.llm.factory import build_llm
from app.agent.llm.openai_compat import OpenAICompatLLM
from app.agent.llm.pricing import cost_usd
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOLS
from app.config import get_settings


def _usage(label: str, client: LLMClient, resp_usage: object, model: str) -> None:
    print(f"  usage[{label}]: {resp_usage}  paid-equivalent cost: ${cost_usd(model, resp_usage)}")  # type: ignore[arg-type]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simplify-schema", action="store_true", help="strip keywords some providers reject")
    args = ap.parse_args()
    settings = get_settings()
    if settings.llm_mode != "real":
        print("LLM_MODE is not 'real': nothing sent.")
        return 2
    llm = build_llm(settings)
    if isinstance(llm, OpenAICompatLLM) and args.simplify_schema:
        llm.simplify_schemas = True
    print(
        f"provider={settings.llm_provider} model={llm.model} simplified_schemas="
        f"{getattr(llm, 'simplify_schemas', 'n/a') if not isinstance(llm, AnthropicLLM) else 'n/a'}"
    )
    system = build_system_prompt(settings.lab_name, date(2026, 11, 21), "COLLECTING", {})
    try:
        r1 = await llm.complete("Reply with exactly: OK", [{"role": "user", "content": "ping"}], [])
        print(f"1) plain completion -> {r1.text!r}")
        _usage("1", llm, r1.usage, llm.model)

        msgs: list[Message] = [{"role": "user", "content": "Hi, I want a CBC test on 2026-11-23, any time."}]
        r2 = await llm.complete(system, msgs, TOOLS)
        print(f"2) tool call -> {[(c.id, c.name, c.arguments) for c in r2.tool_calls]} text={r2.text!r}")
        _usage("2", llm, r2.usage, llm.model)
        if not r2.tool_calls or r2.tool_calls[0].name != "check_slots":
            print("FAIL: expected a check_slots call")
            return 1
        call = r2.tool_calls[0]
        msgs.append(
            {"role": "assistant", "content": r2.text, "tool_calls": [c.as_dict() for c in r2.tool_calls], "raw": r2.raw}
        )
        result = {
            "ok": True,
            "test_code": "CBC",
            "slots": [
                {"slot_id": "S-20261123-0800", "time": "Mon 23 Nov, 08:00"},
                {"slot_id": "S-20261123-0830", "time": "Mon 23 Nov, 08:30"},
            ],
        }
        msgs.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result)})
        r3 = await llm.complete(system, msgs, TOOLS)
        print(f"3) after tool result -> text={r3.text!r} calls={[c.name for c in r3.tool_calls]}")
        _usage("3", llm, r3.usage, llm.model)
    except LLMError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
