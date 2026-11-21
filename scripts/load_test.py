"""Load test: 1,000 webhook messages (~15% duplicates) in bursts, then a generated LOAD_REPORT.md.

Every number in the report is measured from this run (client timings, /metrics, Postgres, the
lab's double-booking detector). Run inside compose:  docker compose --profile load run --rm loadtest
"""

import argparse
import asyncio
import os
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import psycopg

from scripts import wa

IST = ZoneInfo("Asia/Kolkata")
TESTS = ["CBC", "LIPID", "TSH", "HBA1C", "VITD"]
FIRST = ["Asha", "Ravi", "Meera", "Arjun", "Divya", "Karan", "Sneha", "Vikram", "Priya", "Rahul", "Anita", "Rohan"]
LAST = ["Rao", "Iyer", "Shah", "Nair", "Gupta", "Menon", "Das", "Patel", "Reddy", "Khan", "Joshi", "Bose"]
HANDOFF_TEXTS = [
    "What does a high TSH result in my report mean?",
    "I have fever and body pain, which medicine should I take?",
    "This is the worst service, I want a refund!!!",
    "Can I talk to a real person please",
]
SLOTS_PER_DAY, SLOT_DAYS = 8, 7


@dataclass
class Send:
    at: float  # seconds from start
    phone: str
    messages: list[dict[str, Any]]  # >1 = batched webhook
    cohort: str
    duplicate: bool = False


@dataclass
class Plan:
    sends: list[Send] = field(default_factory=list)
    cohorts: Counter[str] = field(default_factory=Counter)  # phones per cohort
    booking_phones: Counter[str] = field(default_factory=Counter)
    contested_slot: str = ""


def _phone(i: int) -> str:
    return f"9199999{i:05d}"


def build_plan(rng: random.Random, total: int, dup_rate: float, today: datetime) -> Plan:
    plan = Plan()
    n = 0
    tomorrow = (today + timedelta(days=1)).date()

    def add(at: float, phone: str, text: str | None, cohort: str, msg_type: str = "text") -> None:
        plan.sends.append(Send(at, phone, [wa.message(phone, text, msg_type=msg_type)], cohort))

    def name() -> str:
        return f"{rng.choice(FIRST)} {rng.choice(LAST)}"

    # Spammers: bursts of 20 in under a second; some webhooks batch 2 messages.
    for _ in range(10):
        n += 1
        phone, t0 = _phone(n), rng.uniform(5, 40)
        plan.cohorts["spammer"] += 1
        i = 0
        while i < 20:
            batch = 2 if rng.random() < 0.25 and i < 19 else 1
            msgs = [wa.message(phone, f"hello? {i + k}") for k in range(batch)]
            plan.sends.append(Send(t0 + i * 0.04, phone, msgs, "spammer"))
            i += batch

    # Contention: 40 phones all ask for tomorrow's first slot, then all try to book it at once.
    slot = plan.contested_slot = f"S-{tomorrow:%Y%m%d}-0800"
    for _ in range(40):
        n += 1
        phone = _phone(n)
        plan.cohorts["contention"] += 1
        plan.booking_phones["contention"] += 1
        add(3 + rng.uniform(0, 0.5), phone, "Hi, I want a CBC test tomorrow", "contention")
        add(20 + rng.uniform(0, 0.3), phone, f"Book {slot}, my name is {name()}", "contention")

    # Handoff triggers (medical / angry / asks for a human).
    for _ in range(15):
        n += 1
        phone, t0 = _phone(n), rng.uniform(0, 35)
        plan.cohorts["handoff"] += 1
        add(t0, phone, "Hi", "handoff")
        add(t0 + rng.uniform(6, 9), phone, rng.choice(HANDOFF_TEXTS), "handoff")

    # Non-text (image) messages.
    for _ in range(5):
        n += 1
        plan.cohorts["non_text"] += 1
        add(rng.uniform(0, 40), _phone(n), None, "non_text", msg_type="image")

    # Normal patients: <=5 messages each, paced 6-9 s apart (bucket capacity 6 is never exceeded).
    unique_target = round(total * (1 - dup_rate))
    while sum(len(s.messages) for s in plan.sends) < unique_target:
        n += 1
        phone, t0 = _phone(n), rng.uniform(0, 30)
        plan.cohorts["normal"] += 1
        plan.booking_phones["normal"] += 1
        day = tomorrow + timedelta(days=rng.randint(1, SLOT_DAYS - 1))  # days 2..7; tomorrow is the contest
        script = [
            f"I want a {rng.choice(TESTS)} test on {day.isoformat()}",
            f"Slot {rng.randint(1, 5)}, my name is {name()}",
            "Thanks!",
        ]
        if rng.random() < 0.5:
            script.insert(0, "Hi")
        if rng.random() < 0.3:
            script.append("Ok see you then")
        remaining = unique_target - sum(len(s.messages) for s in plan.sends)
        for k, text in enumerate(script[: max(1, min(5, remaining))]):
            add(t0 + k * rng.uniform(6, 9), phone, text, "normal")

    # Duplicates: re-deliver identical single-message webhooks; half concurrently, half 1-5 s later.
    dup_target = total - sum(len(s.messages) for s in plan.sends)
    singles = [s for s in plan.sends if len(s.messages) == 1]
    for original in rng.sample(singles, dup_target):
        delay = 0.0 if rng.random() < 0.5 else rng.uniform(1, 5)
        plan.sends.append(Send(original.at + delay, original.phone, original.messages, original.cohort, True))
    plan.sends.sort(key=lambda s: s.at)
    return plan


@dataclass
class Result:
    status: int
    latency_s: float
    body: dict[str, Any]


async def run_sends(api: str, secret: str, plan: Plan, concurrency: int) -> list[tuple[Send, Result]]:
    sem = asyncio.Semaphore(concurrency)
    start = time.perf_counter()
    out: list[tuple[Send, Result]] = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=api, timeout=10, limits=limits) as client:

        async def one(s: Send) -> None:
            await asyncio.sleep(max(0.0, s.at - (time.perf_counter() - start)))
            raw, headers = wa.encode(wa.payload(s.messages), secret)
            async with sem:
                t = time.perf_counter()
                resp = await client.post("/webhook", content=raw, headers=headers)
                out.append((s, Result(resp.status_code, time.perf_counter() - t, resp.json())))

        await asyncio.gather(*(one(s) for s in plan.sends))
    return out


async def send_extras(api: str, secret: str) -> dict[str, Counter[int]]:
    extras: dict[str, Counter[int]] = {"bad_signature": Counter(), "status_callback": Counter()}
    async with httpx.AsyncClient(base_url=api, timeout=10) as client:
        for i in range(10):
            raw, headers = wa.encode(wa.payload([wa.message(_phone(90000 + i), "forged")]), "wrong-secret")
            extras["bad_signature"][(await client.post("/webhook", content=raw, headers=headers)).status_code] += 1
            raw, headers = wa.encode(wa.status_payload(_phone(90000 + i)), secret)
            extras["status_callback"][(await client.post("/webhook", content=raw, headers=headers)).status_code] += 1
    return extras


async def q(dsn: str, sql: str) -> list[dict[str, Any]]:
    async with (
        await psycopg.AsyncConnection.connect(dsn) as conn,
        conn.cursor(row_factory=psycopg.rows.dict_row) as cur,
    ):
        await cur.execute(sql)  # type: ignore[arg-type]
        return list(await cur.fetchall())


async def wait_drained(dsn: str, timeout_s: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        [row] = await q(
            dsn,
            "SELECT (SELECT count(*) FROM inbound_messages WHERE status='queued') AS q, "
            "(SELECT count(*) FROM bookings WHERE status IN ('pending','unknown')) AS b, "
            "(SELECT count(*) FROM outbound_messages WHERE status='pending') AS o",
        )
        if row["q"] == row["b"] == row["o"] == 0:
            return time.monotonic() - start
        await asyncio.sleep(1)
    raise SystemExit(f"queue did not drain within {timeout_s}s")


def parse_metrics(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.rpartition(" ")
            out[key] = float(value)
    return out


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p * (len(ordered) - 1)))]


def ms(v: float) -> str:
    return f"{v * 1000:.1f} ms"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    ap.add_argument("--lab", default=os.environ.get("LAB_URL", "http://localhost:8100"))
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--messages", type=int, default=1000)
    ap.add_argument("--dup-rate", type=float, default=0.15)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261120)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--out", default="LOAD_REPORT.md")
    args = ap.parse_args()
    secret = os.environ.get("WHATSAPP_APP_SECRET", "replace-with-meta-app-secret")
    admin = {"Authorization": f"Bearer {os.environ.get('ADMIN_TOKEN', 'replace-with-long-random-admin-token')}"}
    usd_inr = float(os.environ.get("USD_INR", "88"))

    async with httpx.AsyncClient(timeout=10) as c:
        if args.reset:
            (await c.post(f"{args.api}/admin/reset-mock-data", headers=admin)).raise_for_status()
            (await c.post(f"{args.lab}/admin/reset", headers=admin)).raise_for_status()
        chaos = (await c.get(f"{args.lab}/admin/chaos", headers=admin)).json()

    plan = build_plan(random.Random(args.seed), args.messages, args.dup_rate, datetime.now(UTC).astimezone(IST))
    sent_messages = sum(len(s.messages) for s in plan.sends)
    print(f"sending {sent_messages} messages in {len(plan.sends)} webhooks ...", flush=True)
    t0 = time.monotonic()
    results = await run_sends(args.api, secret, plan, args.concurrency)
    send_s = time.monotonic() - t0
    extras = await send_extras(args.api, secret)
    print(f"sent in {send_s:.1f}s; waiting for workers to drain ...", flush=True)
    drain_s = await wait_drained(args.dsn, 300)

    async with httpx.AsyncClient(timeout=10) as c:
        metrics = parse_metrics((await c.get(f"{args.api}/metrics")).text)
        detector = (await c.get(f"{args.lab}/admin/double-bookings", headers=admin)).json()

    db = {
        "messages": await q(args.dsn, "SELECT status, count(*) AS n FROM inbound_messages GROUP BY 1 ORDER BY 1"),
        "dupes": (await q(args.dsn, "SELECT count(*) AS n FROM duplicate_deliveries"))[0]["n"],
        "bookings": await q(args.dsn, "SELECT status, count(*) AS n FROM bookings GROUP BY 1 ORDER BY 1"),
        "slot_dupes": (
            await q(
                args.dsn,
                "SELECT count(*) AS n FROM (SELECT slot_id FROM bookings WHERE status IN "
                "('pending','confirmed','unknown') GROUP BY 1 HAVING count(*)>1) x",
            )
        )[0]["n"],
        "patient_dupes": (
            await q(
                args.dsn,
                "SELECT count(*) AS n FROM (SELECT phone, test_code FROM bookings WHERE "
                "status IN ('pending','confirmed','unknown') GROUP BY 1,2 "
                "HAVING count(*)>1) x",
            )
        )[0]["n"],
        "contention_winners": (
            await q(
                args.dsn,
                f"SELECT count(*) AS n FROM bookings WHERE status='confirmed' AND slot_id = '{plan.contested_slot}'",
            )
        )[0]["n"],
        "lab_calls": await q(
            args.dsn, "SELECT endpoint, outcome, count(*) AS n FROM lab_calls GROUP BY 1,2 ORDER BY 1,2"
        ),
        "retries": (await q(args.dsn, "SELECT count(*) AS n FROM lab_calls WHERE attempt > 1"))[0]["n"],
        "handoffs": await q(args.dsn, "SELECT reason, count(*) AS n FROM handoffs GROUP BY 1 ORDER BY 1"),
        "events": await q(args.dsn, "SELECT kind, count(*) AS n FROM events GROUP BY 1 ORDER BY 1"),
        "llm": (
            await q(
                args.dsn,
                "SELECT count(*) AS calls, coalesce(sum(input_tokens),0) AS i, "
                "coalesce(sum(output_tokens),0) AS o, coalesce(sum(cost_usd),0) AS usd, "
                "count(DISTINCT conversation_id) AS convs, model FROM llm_usage GROUP BY model",
            )
        ),
        "outbound": await q(args.dsn, "SELECT status, count(*) AS n FROM outbound_messages GROUP BY 1"),
        "processed": (await q(args.dsn, "SELECT count(*) AS n FROM inbound_messages WHERE status <> 'rate_limited'"))[
            0
        ]["n"],
    }

    statuses = Counter(r.status for _, r in results)
    lat = [r.latency_s for _, r in results]
    by_cohort_rl = Counter()
    for s, r in results:
        by_cohort_rl[s.cohort] += r.body.get("rate_limited", 0)
    unique_sent = sum(len(s.messages) for s in plan.sends if not s.duplicate)
    dup_sent = sent_messages - unique_sent
    msg_status = {r["status"]: r["n"] for r in db["messages"]}
    booking_status = {r["status"]: r["n"] for r in db["bookings"]}
    normal_slots = SLOTS_PER_DAY * (SLOT_DAYS - 1)
    expected_max = 1 + min(normal_slots, plan.booking_phones["normal"])
    llm = db["llm"][0] if db["llm"] else {"calls": 0, "i": 0, "o": 0, "usd": 0, "convs": 0, "model": "-"}
    usd = float(llm["usd"])
    double_total = (
        db["slot_dupes"] + db["patient_dupes"] + len(detector["slot_violations"]) + len(detector["patient_violations"])
    )

    def server_q(q: str) -> float:
        return metrics.get(f'webhook_ack_recent_seconds{{quantile="{q}"}}', 0.0)

    def row(label: str, value: Any, note: str = "") -> str:
        return f"| {label} | {value} | {note} |"

    lines = [
        "# LOAD_REPORT",
        "",
        f"Generated by `scripts/load_test.py` on {datetime.now(UTC):%Y-%m-%d %H:%M UTC}. Every number below is "
        "measured from this run; nothing is hand-edited.",
        "",
        "## Run parameters",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| Messages sent (incl. duplicates) | {sent_messages} in {len(plan.sends)} webhooks |",
        f"| Duplicate rate (target) | {args.dup_rate:.0%} |",
        f"| Client concurrency | {args.concurrency} |",
        f"| Seed | {args.seed} |",
        f"| Lab chaos | failure_rate={chaos['failure_rate']}, "
        f"latency {chaos['latency_min_ms']}–{chaos['latency_max_ms']} ms, "
        f"enforce_idempotency={chaos['enforce_idempotency']}, enforce_capacity={chaos['enforce_capacity']} |",
        "| LLM | mock (deterministic, zero cost; tokens estimated, priced as `claude-haiku-5-5`) |",
        "| Workers | 2 containers × WORKER_CONCURRENCY claim loops |",
        f"| Phones per cohort | {dict(plan.cohorts)} |",
        f"| Send phase / drain after send | {send_s:.1f} s / {drain_s:.1f} s |",
        "",
        "## Headline",
        "",
        "| Metric | Value | Notes |",
        "|---|---|---|",
        row("Sent", sent_messages, f"{unique_sent} unique + {dup_sent} duplicate deliveries"),
        row("HTTP responses", dict(statuses), "every valid webhook acknowledged with 200"),
        row("Unique messages stored", sum(msg_status.values()), f"by status: {msg_status}"),
        row("Unique processed by workers", db["processed"], "done + held (handoff) + dead; excludes rate-limited"),
        row("Duplicates dropped", db["dupes"], f"of {dup_sent} duplicate deliveries sent"),
        row("Rate-limited", msg_status.get("rate_limited", 0), f"by cohort: {dict(+by_cohort_rl)}"),
        row(
            "Bookings",
            booking_status,
            f"expected max confirmed = 1 (contested slot) + min({normal_slots} "
            f"free slots, {plan.booking_phones['normal']} normal booking phones) = {expected_max}",
        ),
        row("Contention cohort winners", db["contention_winners"], "40 phones raced for one slot; must be ≤ 1"),
        row(
            "**Double bookings**",
            f"**{double_total}**",
            f"gateway DB: slot={db['slot_dupes']}, patient={db['patient_dupes']}; lab detector: "
            f"slot={len(detector['slot_violations'])}, patient={len(detector['patient_violations'])}; "
            f"lab total bookings={detector['total_bookings']}",
        ),
        row("Lab retries", db["retries"], "HTTP attempts beyond the first, all endpoints"),
        row("Handoffs", {r["reason"]: r["n"] for r in db["handoffs"]}, ""),
        row("DLQ", msg_status.get("dead", 0), ""),
        row("Events", {r["kind"]: r["n"] for r in db["events"]}, ""),
        row("Outbound replies", {r["status"]: r["n"] for r in db["outbound"]}, "via outbox, mock sender"),
        "",
        "## Webhook ack latency",
        "",
        "| Source | p50 | p95 | p99 | max | n |",
        "|---|---|---|---|---|---|",
        f"| Client-observed (load script) | {ms(pct(lat, 0.5))} | {ms(pct(lat, 0.95))} | {ms(pct(lat, 0.99))} | "
        f"{ms(max(lat))} | {len(lat)} |",
        f"| Server-side (`webhook_ack_recent_seconds`) | {ms(server_q('0.5'))} | {ms(server_q('0.95'))} | "
        f"{ms(server_q('0.99'))} | – | – |",
        "",
        f"Mean client latency {ms(statistics.mean(lat))}. Target: p99 < 200 ms.",
        "",
        "## Lab calls",
        "",
        "| Endpoint | Outcome | Count |",
        "|---|---|---|",
        *[f"| {r['endpoint']} | {r['outcome']} | {r['n']} |" for r in db["lab_calls"]],
        "",
        "## Tokens and cost (mock LLM)",
        "",
        "| LLM calls | Conversations | Input tokens | Output tokens | Est. cost USD | Est. cost INR |",
        "|---|---|---|---|---|---|",
        f"| {llm['calls']} | {llm['convs']} | {llm['i']} | {llm['o']} | ${usd:.4f} | ₹{usd * usd_inr:.2f} |",
        "",
        f"Per processed message: {llm['calls'] / max(1, db['processed']):.2f} LLM calls, "
        f"{int(llm['i']) / max(1, db['processed']):.0f} input + {int(llm['o']) / max(1, db['processed']):.0f} "
        f"output tokens. Model label `{llm['model']}`; mock tokens are `ceil(chars/4)` estimates, not billed. "
        f"USD→INR at {usd_inr}.",
        "",
        "## Extras (not counted in the messages above)",
        "",
        f"- 10 forged-signature webhooks → HTTP {dict(extras['bad_signature'])}",
        f"- 10 delivery-status callbacks → HTTP {dict(extras['status_callback'])}",
        "",
        "## How to read this",
        "",
        "- **Duplicates**: every re-delivered message id hit `UNIQUE(wa_message_id)` and was acknowledged "
        "with 200 without being processed again.",
        "- **Rate limit**: only the spammer cohort should be limited (bucket capacity 6, refill 1 per 10 s); "
        "normal patients send at most 5 messages.",
        "- **Bookings below the expected maximum** are normal patients who picked a slot another patient "
        "already took (`slot_taken`), or whose slot list came back empty. They are not lost bookings.",
        "- **Double bookings must be 0** in both the gateway DB and the naive lab, which accepts anything it is sent.",
    ]
    await asyncio.to_thread(Path(args.out).write_text, "\n".join(lines) + "\n")
    print("\n".join(lines[lines.index("## Headline") : lines.index("## Lab calls")]))
    print(f"\nreport written to {args.out}")
    if double_total:
        raise SystemExit("DOUBLE BOOKING DETECTED")


if __name__ == "__main__":
    asyncio.run(main())
