"""Real-LLM demo: 7 synthetic conversations (<= 40 messages) through the full stack, against the flaky
mock lab. Writes docs/REAL_LLM_DEMO.md with transcripts, verified outcomes and token usage.

Precondition (only the worker holds the key):
    LLM_MODE=real docker compose up -d worker
Then:  make demo-real
All names and phones are synthetic; phones are masked in the output. The key is never read here.
"""

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.rows import dict_row

from app.obs.logs import mask_phone
from scripts import wa

MAX_MESSAGES = 40
SCENARIOS = ["happy", "conflict", "medical", "angry", "duplicate", "outage", "scope"]
MIN_GAP_S = 4.0  # pace sends so a free-tier RPM limit is not hit (each message is ~1-3 LLM calls)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Convo:
    key: str
    title: str
    expect: str
    phone: str
    lines: list[str] = field(default_factory=list)
    checks: list[tuple[str, bool]] = field(default_factory=list)


class Demo:
    def __init__(self, api: str, lab: str, dsn: str, secret: str, admin: dict[str, str]) -> None:
        self.api, self.lab, self.dsn, self.secret, self.admin = api, lab, dsn, secret, admin
        self.sent = 0
        self.last_send = 0.0
        self.mode_checked = False

    async def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row) as conn:
            cur = await conn.execute(sql, params)  # type: ignore[arg-type]
            return list(await cur.fetchall())

    async def replies(self, phone: str) -> int:
        rows = await self.q("SELECT count(*) AS n FROM outbound_messages WHERE phone=%s AND status='sent'", (phone,))
        return int(rows[0]["n"])

    async def say(self, c: Convo, text: str, msg_id: str | None = None, expect_reply: bool = True) -> str:
        if self.sent >= MAX_MESSAGES:
            raise SystemExit("message budget reached")
        wait = MIN_GAP_S - (time.monotonic() - self.last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        before = await self.replies(c.phone)
        raw, headers = wa.encode(wa.payload([wa.message(c.phone, text, msg_id)]), self.secret)
        async with httpx.AsyncClient(timeout=10) as h:
            resp = await h.post(f"{self.api}/webhook", content=raw, headers=headers)
        self.last_send = time.monotonic()
        body = resp.json()
        if body.get("duplicates"):
            c.lines.append(
                f"**user (duplicate delivery of the same `wamid`):** {text}  \n→ HTTP {resp.status_code} `{body}`"
            )
            return ""
        self.sent += 1
        c.lines.append(f"**user:** {text}")
        if not expect_reply:
            return ""
        reply = await self.wait_reply(c.phone, before)
        c.lines.append(f"**bot:** {reply}")
        if not self.mode_checked:
            mode = await self.q("SELECT mode FROM llm_usage WHERE mode='real' LIMIT 1")
            if not mode:
                sys.exit("worker is not in LLM_MODE=real: restart it with `LLM_MODE=real docker compose up -d worker`")
            self.mode_checked = True
        return reply

    async def wait_reply(self, phone: str, before: int, timeout_s: float = 120) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self.replies(phone) > before:
                rows = await self.q(
                    "SELECT body FROM outbound_messages WHERE phone=%s AND status='sent' ORDER BY id DESC LIMIT 1",
                    (phone,),
                )
                return str(rows[0]["body"])
            await asyncio.sleep(1)
        return "(no reply within timeout)"

    async def conv(self, phone: str) -> dict[str, Any]:
        rows = await self.q("SELECT id, state FROM conversations WHERE phone=%s", (phone,))
        return rows[0] if rows else {"id": None, "state": None}

    async def bookings(self, phone: str) -> list[dict[str, Any]]:
        return await self.q(
            "SELECT slot_id, status, test_code, attempt_no FROM bookings WHERE phone=%s ORDER BY id", (phone,)
        )

    async def handoffs(self, phone: str) -> list[str]:
        rows = await self.q(
            "SELECT h.reason FROM handoffs h JOIN conversations c ON c.id=h.conversation_id WHERE c.phone=%s", (phone,)
        )
        return [r["reason"] for r in rows]

    async def chaos(self, rate: float) -> None:
        async with httpx.AsyncClient(timeout=10) as h:
            (
                await h.post(f"{self.lab}/admin/chaos", json={"failure_rate": rate}, headers=self.admin)
            ).raise_for_status()


def day(offset: int) -> str:
    return (datetime.now(UTC).astimezone(IST).date() + timedelta(days=offset)).isoformat()


async def run(d: Demo, only: set[str]) -> tuple[list[Convo], dict[str, Any]]:
    def want(key: str) -> bool:
        return not only or key in only

    async with httpx.AsyncClient(timeout=10) as h:
        (await h.post(f"{d.api}/admin/reset-mock-data", headers=d.admin)).raise_for_status()
        (await h.post(f"{d.lab}/admin/reset", headers=d.admin)).raise_for_status()
        (await h.post(f"{d.lab}/admin/chaos", json={"failure_rate": 0.2}, headers=d.admin)).raise_for_status()
    p = iter(f"9199999{60001 + i:05d}" for i in range(20))
    convos: list[Convo] = []

    # 1. Happy path
    if want("happy"):
        c = Convo("happy", "Happy-path booking", "BOOKED with one confirmed booking", next(p))
        convos.append(c)
        await d.say(c, f"Hi, I'd like to book a CBC blood test on {day(2)}, morning if possible.")
        await d.say(c, "The first slot please. Patient name is Asha Rao.")
        b = await d.bookings(c.phone)
        c.checks += [
            ("state BOOKED", (await d.conv(c.phone))["state"] == "BOOKED"),
            ("exactly one confirmed booking", [x["status"] for x in b] == ["confirmed"]),
        ]

    # 2. Slot conflict, then rebook (another patient takes the slot in between)
    if want("conflict"):
        c = Convo("conflict", "Slot conflict, then rebook", "first pick slot_taken, second pick BOOKED", next(p))
        rival = Convo("rival", "(rival patient for #2)", "books the slot first", next(p))
        convos += [c, rival]
        await d.say(c, f"I need a TSH thyroid test on {day(3)}.")
        await d.say(rival, f"Book a LIPID test on {day(3)} please.")
        await d.say(rival, "First slot. Name: Ravi Kumar.")
        taken = (await d.bookings(rival.phone) or [{"slot_id": "?"}])[0]["slot_id"]
        hhmm = taken[-4:-2] + ":" + taken[-2:]
        await d.say(c, f"I'll take the {hhmm} slot. My name is Meera Iyer.")
        await d.say(c, "OK, then the next available slot after that one, same name.")
        b = await d.bookings(c.phone)
        c.checks += [
            ("rival holds the contested slot", (await d.bookings(rival.phone))[0]["status"] == "confirmed"),
            (
                "patient ends with one confirmed booking on a different slot",
                [x["status"] for x in b if x["status"] == "confirmed"] == ["confirmed"]
                and all(x["slot_id"] != taken for x in b if x["status"] == "confirmed"),
            ),
        ]

    # 3. Medical question
    if want("medical"):
        c = Convo("medical", "Medical question", "handoff medical_question, no booking", next(p))
        convos.append(c)
        await d.say(c, "My HbA1c report came back 7.2. Is that dangerous? Should I change my medicine?")
        c.checks += [
            ("handoff medical_question", "medical_question" in await d.handoffs(c.phone)),
            ("state HANDOFF", (await d.conv(c.phone))["state"] == "HANDOFF"),
        ]

    # 4. Angry user
    if want("angry"):
        c = Convo("angry", "Angry user", "handoff angry_user", next(p))
        convos.append(c)
        await d.say(c, "This is ridiculous!!! I've been trying to book for an hour and nothing works. Useless service.")
        c.checks += [
            ("handoff to a human", bool(await d.handoffs(c.phone))),
            ("state HANDOFF", (await d.conv(c.phone))["state"] == "HANDOFF"),
        ]

    # 5. Duplicate webhook delivery
    if want("duplicate"):
        c = Convo("duplicate", "Duplicate webhook delivery", "one reply and one booking per unique message", next(p))
        convos.append(c)
        await d.say(c, f"Can I book a Vitamin D test on {day(4)}?", msg_id="wamid.demo.dup.1")
        await d.say(c, f"Can I book a Vitamin D test on {day(4)}?", msg_id="wamid.demo.dup.1")
        await d.say(c, "First slot, name Karan Shah.", msg_id="wamid.demo.dup.2")
        await d.say(c, "First slot, name Karan Shah.", msg_id="wamid.demo.dup.2")
        await asyncio.sleep(8)  # give any (wrong) second reply time to appear
        c.checks += [
            ("2 replies for 2 unique messages", await d.replies(c.phone) == 2),
            (
                "at most one active booking",
                len([x for x in await d.bookings(c.phone) if x["status"] in ("confirmed", "pending", "unknown")]) <= 1,
            ),
        ]

    # 6. Lab outage
    if want("outage"):
        c = Convo("outage", "Lab outage", "handoff lab_outage, no booking", next(p))
        convos.append(c)
        await d.chaos(1.0)
        await d.say(c, f"Please book an HbA1c test on {day(5)}.")
        await d.chaos(0.2)
        c.checks += [
            ("handoff lab_outage", "lab_outage" in await d.handoffs(c.phone)),
            ("no booking", await d.bookings(c.phone) == []),
        ]
        c.lines.append("_(lab restored to 20% failures; waiting 35 s for the circuit breakers to cool down)_")
        await asyncio.sleep(35)

    # 7. Out of scope
    if want("scope"):
        c = Convo("scope", "Out-of-scope request", "polite redirect, no tool misuse, no booking", next(p))
        convos.append(c)
        await d.say(c, "Can you recommend a good restaurant near the lab and book me a table for two?")
        c.checks += [
            ("no booking", await d.bookings(c.phone) == []),
            ("no handoff needed", await d.handoffs(c.phone) == []),
        ]

    async with httpx.AsyncClient(timeout=10) as h:
        detector = (await h.get(f"{d.lab}/admin/double-bookings", headers=d.admin)).json()
    usage = await d.q(
        "SELECT c.phone, count(u.id) AS calls, coalesce(sum(u.input_tokens),0) AS i, "
        "coalesce(sum(u.output_tokens),0) AS o, coalesce(sum(u.cost_usd),0) AS usd, "
        "(SELECT count(*) FROM inbound_messages m WHERE m.conversation_id=c.id) AS msgs "
        "FROM conversations c LEFT JOIN llm_usage u ON u.conversation_id=c.id AND u.mode='real' "
        "GROUP BY c.id ORDER BY c.id"
    )
    totals = (
        await d.q(
            "SELECT count(*) AS calls, coalesce(sum(input_tokens),0) AS i, "
            "coalesce(sum(output_tokens),0) AS o, coalesce(sum(cost_usd),0) AS usd, max(model) AS model "
            "FROM llm_usage WHERE mode='real'"
        )
    )[0]
    processed = (await d.q("SELECT count(*) AS n FROM inbound_messages WHERE status IN ('done','held')"))[0]["n"]
    retries = (await d.q("SELECT count(*) AS n FROM lab_calls WHERE attempt > 1"))[0]["n"]
    return convos, {
        "detector": detector,
        "usage": usage,
        "totals": totals,
        "processed": processed,
        "retries": retries,
        "sent": d.sent,
    }


def render(convos: list[Convo], r: dict[str, Any], usd_inr: float, tier: str) -> str:
    t = r["totals"]
    usd = float(t["usd"])
    det = r["detector"]
    per_msg_i = int(t["i"]) / max(1, r["processed"])
    per_msg_o = int(t["o"]) / max(1, r["processed"])
    out = [
        "# Real-LLM demo",
        "",
        f"Generated by `scripts/demo_real_llm.py` on {datetime.now(UTC):%Y-%m-%d %H:%M UTC}. Model `{t['model']}` via "
        "Gemini's OpenAI-compatible endpoint, full stack (api → Postgres queue → 2 workers → flaky mock lab at "
        "20% failures). All conversations are synthetic and phones are masked.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Unique messages sent | {r['sent']} (limit {MAX_MESSAGES}) |",
        f"| Messages processed | {r['processed']} |",
        f"| Real LLM calls | {t['calls']} |",
        f"| Tokens | {t['i']} input + {t['o']} output |",
        f"| Tokens per processed message | {per_msg_i:.0f} input + {per_msg_o:.0f} output |",
        f"| Paid-tier equivalent cost | ${usd:.6f} (₹{usd * usd_inr:.4f}) |",
        f"| Billed (tier: {tier}) | {'$0 (free tier)' if tier == 'free' else f'${usd:.6f}'} |",
        f"| Lab retries during demo | {r['retries']} |",
        f"| Double bookings (lab detector) | slot={len(det['slot_violations'])}, "
        f"patient={len(det['patient_violations'])}, total lab bookings={det['total_bookings']} |",
        "",
        "## Outcomes",
        "",
        "| # | Scenario | Expected | Checks |",
        "|---|---|---|---|",
    ]
    n = 0
    for c in convos:
        if c.key == "rival":
            continue
        n += 1
        checks = "<br>".join(f"{'PASS' if ok else 'FAIL'}: {name}" for name, ok in c.checks)
        out.append(f"| {n} | {c.title} | {c.expect} | {checks} |")
    out += ["", "## Transcripts", ""]
    for c in convos:
        out += [f"### {c.title} ({mask_phone(c.phone)})", "", *[f"{line}  " for line in c.lines], ""]
    out += [
        "## Tokens per conversation",
        "",
        "| Phone | Messages | LLM calls | Input | Output | Paid-eq. USD |",
        "|---|---|---|---|---|---|",
    ]
    for u in r["usage"]:
        out.append(
            f"| {mask_phone(u['phone'])} | {u['msgs']} | {u['calls']} | {u['i']} | {u['o']} | {float(u['usd']):.6f} |"
        )
    return "\n".join(out) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/REAL_LLM_DEMO.md")
    ap.add_argument("--only", default="", help="comma-separated scenarios: " + ",".join(SCENARIOS))
    args = ap.parse_args()
    d = Demo(
        os.environ.get("API_URL", "http://localhost:8000"),
        os.environ.get("LAB_URL", "http://localhost:8100"),
        os.environ["DATABASE_URL"],
        os.environ.get("WHATSAPP_APP_SECRET", "replace-with-meta-app-secret"),
        {"Authorization": f"Bearer {os.environ.get('ADMIN_TOKEN', 'replace-with-long-random-admin-token')}"},
    )
    convos, results = await run(d, {k for k in args.only.split(",") if k})
    text = render(convos, results, float(os.environ.get("USD_INR", "88")), os.environ.get("LLM_BILLING_TIER", "paid"))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(Path(args.out).write_text, text)
    print(text)
    failed = [name for c in convos for name, ok in c.checks if not ok]
    print(f"\n{len(failed)} failed checks" + (f": {failed}" if failed else ""))


if __name__ == "__main__":
    asyncio.run(main())
