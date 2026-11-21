"""/metrics: in-process webhook metrics plus DB-derived counters.

DB-derived series are computed at scrape time, so they are correct across all worker replicas and
survive restarts. Per-conversation series are capped to the most recently active conversations to
bound label cardinality; the full breakdown is at /metrics/conversations.
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request, Response
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from app.api.state import AppState
from app.api.timing import RECENT

router = APIRouter()
PER_CONVERSATION_SERIES = 200

_CONVERSATION_USAGE_SQL = """
SELECT c.id AS conversation_id, c.state,
       (SELECT count(*) FROM inbound_messages m WHERE m.conversation_id = c.id) AS messages,
       count(u.id) AS llm_calls,
       coalesce(sum(u.input_tokens), 0) AS input_tokens,
       coalesce(sum(u.output_tokens), 0) AS output_tokens,
       coalesce(sum(u.cache_read_tokens), 0) AS cache_read_tokens,
       coalesce(sum(u.cost_usd), 0) AS cost_usd,
       count(u.id) FILTER (WHERE u.id IS NOT NULL AND u.cost_usd IS NULL) AS cost_unknown_calls
FROM conversations c
LEFT JOIN llm_usage u ON u.conversation_id = c.id
GROUP BY c.id
ORDER BY c.updated_at DESC
LIMIT %s
"""


class _Snapshot(Collector):
    def __init__(self, metrics: list[Metric]) -> None:
        self._metrics = metrics

    def collect(self) -> Iterable[Metric]:
        return self._metrics


async def _rows(conn: AsyncConnection[DictRow], sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    return list(await cur.fetchall())


async def _scalar(conn: AsyncConnection[DictRow], sql: str) -> float:
    rows = await _rows(conn, sql)
    value = next(iter(rows[0].values())) if rows else 0
    return float(value or 0)


def _counter(name: str, doc: str, value: float) -> CounterMetricFamily:
    return CounterMetricFamily(name, doc, value=value)


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, value=value)


async def collect_db_metrics(conn: AsyncConnection[DictRow], usd_inr: float) -> list[Metric]:
    out: list[Metric] = []
    msg = await _rows(conn, "SELECT status, count(*) AS n FROM inbound_messages GROUP BY status")
    by_status = {r["status"]: float(r["n"]) for r in msg}
    out.append(_counter("messages_received", "Unique inbound messages stored", sum(by_status.values())))
    fam = GaugeMetricFamily("messages_by_status", "Unique inbound messages by status", labels=["status"])
    for status in ("queued", "done", "rate_limited", "held", "dead"):
        fam.add_metric([status], by_status.get(status, 0.0))
    out.append(fam)
    out.append(
        _counter(
            "dedupe_hits",
            "Duplicate webhook deliveries dropped",
            await _scalar(conn, "SELECT count(*) FROM duplicate_deliveries"),
        )
    )
    out.append(
        _counter("rate_limited", "Messages dropped by the per-phone rate limit", by_status.get("rate_limited", 0.0))
    )
    out.append(_gauge("queue_depth", "Queued inbound messages awaiting a worker", by_status.get("queued", 0.0)))
    out.append(
        _gauge(
            "queue_oldest_age_seconds",
            "Age of the oldest queued message",
            await _scalar(
                conn,
                "SELECT coalesce(extract(epoch FROM now() - min(received_at)), 0) "
                "FROM inbound_messages WHERE status='queued'",
            ),
        )
    )
    out.append(_gauge("dlq_size", "Dead-lettered messages", by_status.get("dead", 0.0)))

    lab = CounterMetricFamily("lab_calls", "Lab API HTTP attempts", labels=["endpoint", "outcome"])
    for r in await _rows(conn, "SELECT endpoint, outcome, count(*) AS n FROM lab_calls GROUP BY 1, 2"):
        lab.add_metric([r["endpoint"], r["outcome"]], float(r["n"]))
    out.append(lab)
    out.append(
        _counter(
            "lab_retries",
            "Lab attempts beyond the first for the same logical call",
            await _scalar(conn, "SELECT count(*) FROM lab_calls WHERE attempt > 1"),
        )
    )
    lat = await _rows(
        conn,
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
        "percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99 FROM lab_calls",
    )
    lab_lat = GaugeMetricFamily("lab_call_latency_ms", "Lab call latency percentiles", labels=["quantile"])
    lab_lat.add_metric(["0.5"], float(lat[0]["p50"] or 0))
    lab_lat.add_metric(["0.99"], float(lat[0]["p99"] or 0))
    out.append(lab_lat)

    bookings = CounterMetricFamily("bookings", "Bookings by status", labels=["status"])
    for r in await _rows(conn, "SELECT status, count(*) AS n FROM bookings GROUP BY status"):
        bookings.add_metric([r["status"]], float(r["n"]))
    out.append(bookings)
    out.append(
        _gauge(
            "double_bookings",
            "Active bookings violating slot or patient uniqueness (must be 0)",
            await _scalar(
                conn,
                """
            SELECT (SELECT count(*) FROM (SELECT slot_id FROM bookings WHERE status IN
                      ('pending','confirmed','unknown') GROUP BY slot_id HAVING count(*) > 1) s)
                 + (SELECT count(*) FROM (SELECT phone, test_code FROM bookings WHERE status IN
                      ('pending','confirmed','unknown') GROUP BY 1, 2 HAVING count(*) > 1) p)""",
            ),
        )
    )

    handoffs = CounterMetricFamily("handoffs", "Handoffs to humans by reason", labels=["reason"])
    for r in await _rows(conn, "SELECT reason, count(*) AS n FROM handoffs GROUP BY reason"):
        handoffs.add_metric([r["reason"]], float(r["n"]))
    out.append(handoffs)

    events = CounterMetricFamily("events", "Operational events", labels=["kind"])
    for r in await _rows(conn, "SELECT kind, count(*) AS n FROM events GROUP BY kind"):
        events.add_metric([r["kind"]], float(r["n"]))
    out.append(events)

    outbox = GaugeMetricFamily("outbound_messages", "Outbox rows by status", labels=["status"])
    for r in await _rows(conn, "SELECT status, count(*) AS n FROM outbound_messages GROUP BY status"):
        outbox.add_metric([r["status"]], float(r["n"]))
    out.append(outbox)

    tokens = CounterMetricFamily("llm_tokens", "LLM tokens", labels=["direction", "mode", "model"])
    cost_usd = CounterMetricFamily("llm_cost_usd", "LLM cost in USD (known prices only)", labels=["mode", "model"])
    cost_inr = CounterMetricFamily("llm_cost_inr", "LLM cost in INR (known prices only)", labels=["mode", "model"])
    calls = CounterMetricFamily("llm_calls", "LLM calls", labels=["mode", "model"])
    unknown = CounterMetricFamily(
        "llm_cost_unknown_calls", "LLM calls whose model price is unknown", labels=["mode", "model"]
    )
    for r in await _rows(
        conn,
        """
            SELECT mode, model, count(*) AS n, sum(input_tokens) AS i, sum(output_tokens) AS o,
                   sum(cache_read_tokens) AS cr, sum(cache_write_tokens) AS cw,
                   coalesce(sum(cost_usd), 0) AS usd, count(*) FILTER (WHERE cost_usd IS NULL) AS unk
            FROM llm_usage GROUP BY 1, 2""",
    ):
        labels = [r["mode"], r["model"]]
        for direction, key in (("in", "i"), ("out", "o"), ("cache_read", "cr"), ("cache_write", "cw")):
            tokens.add_metric([direction, *labels], float(r[key]))
        usd = float(Decimal(r["usd"]))
        cost_usd.add_metric(labels, usd)
        cost_inr.add_metric(labels, usd * usd_inr)
        calls.add_metric(labels, float(r["n"]))
        unknown.add_metric(labels, float(r["unk"]))
    out += [tokens, cost_usd, cost_inr, calls, unknown]
    out.append(
        _counter(
            "llm_real_spend_usd",
            "Recorded real LLM spend (append-only ledger)",
            await _scalar(conn, "SELECT coalesce(sum(cost_usd), 0) FROM llm_spend_ledger"),
        )
    )

    conv_tokens = GaugeMetricFamily(
        "conversation_llm_tokens", "LLM tokens per conversation", labels=["conversation_id", "direction"]
    )
    conv_usd = GaugeMetricFamily(
        "conversation_llm_cost_usd", "LLM cost per conversation (USD)", labels=["conversation_id"]
    )
    conv_inr = GaugeMetricFamily(
        "conversation_llm_cost_inr", "LLM cost per conversation (INR)", labels=["conversation_id"]
    )
    for r in await _rows(conn, _CONVERSATION_USAGE_SQL, (PER_CONVERSATION_SERIES,)):
        cid = str(r["conversation_id"])
        conv_tokens.add_metric([cid, "in"], float(r["input_tokens"]))
        conv_tokens.add_metric([cid, "out"], float(r["output_tokens"]))
        usd = float(r["cost_usd"])
        conv_usd.add_metric([cid], usd)
        conv_inr.add_metric([cid], usd * usd_inr)
    out += [conv_tokens, conv_usd, conv_inr]

    hb = GaugeMetricFamily(
        "worker_last_seen_seconds", "Seconds since each worker's heartbeat", labels=["worker_id", "leader"]
    )
    for r in await _rows(
        conn,
        "SELECT worker_id, is_leader, extract(epoch FROM now() - last_seen) AS age "
        "FROM worker_heartbeats WHERE last_seen > now() - interval '10 minutes'",
    ):
        hb.add_metric([r["worker_id"], str(r["is_leader"]).lower()], float(r["age"]))
    out.append(hb)
    return out


def _recent_latency_metric() -> Metric:
    fam = GaugeMetricFamily(
        "webhook_ack_recent_seconds", "Exact webhook ack percentiles over the recent window", labels=["quantile"]
    )
    for q in (0.5, 0.95, 0.99):
        value = RECENT.percentile(q)
        fam.add_metric([str(q)], value if value is not None else 0.0)
    return fam


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    state: AppState = request.app.state.app_state
    async with state.pool.connection() as conn:
        families = await collect_db_metrics(conn, state.settings.usd_inr)
    snapshot = CollectorRegistry()
    snapshot.register(_Snapshot([*families, _recent_latency_metric()]))
    body = generate_latest(REGISTRY) + generate_latest(snapshot)
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/metrics/conversations")
async def conversations(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    state: AppState = request.app.state.app_state
    async with state.pool.connection() as conn:
        rows = await _rows(conn, _CONVERSATION_USAGE_SQL, (max(1, min(limit, 5000)),))
    for r in rows:
        usd = float(r["cost_usd"])
        r["cost_usd"] = round(usd, 8)
        r["cost_inr"] = round(usd * state.settings.usd_inr, 6)
    return rows
