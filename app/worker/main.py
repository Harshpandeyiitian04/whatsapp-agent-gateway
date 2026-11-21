"""Worker process: WORKER_CONCURRENCY claim loops + heartbeat + leader duties. Run: python -m app.worker.main"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys

import httpx

from app.agent.llm.factory import build_llm
from app.agent.loop import Agent
from app.agent.tools import ToolExecutor
from app.aio import sleep_or_stop
from app.clock import SystemClock
from app.config import Settings, get_settings
from app.db import open_pool
from app.lab.booking import BookingService
from app.migrate import migrate
from app.lab.circuit import CircuitBreaker
from app.lab.client import LabClient
from app.obs.logs import setup_logging
from app.whatsapp.sender import GraphSender, MockSender, Sender
from app.worker.leader import Leader
from app.worker.processor import Processor

log = logging.getLogger("worker")
SHUTDOWN_GRACE_S = 25


async def _health_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One-line HTTP reply so a free Render web service can health-check this worker."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(reader.read(1024), timeout=5)
    body = b'{"status":"ok"}'
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    with contextlib.suppress(Exception):
        await writer.drain()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def health_server(stop: asyncio.Event) -> None:
    port = int(os.environ["PORT"])
    server = await asyncio.start_server(_health_client, "0.0.0.0", port)
    log.info("worker health listening", extra={"port": port})
    async with server:
        await stop.wait()


async def claim_loop(n: int, processor: Processor, stop: asyncio.Event, settings: Settings) -> None:
    while not stop.is_set():
        try:
            worked = await processor.process_one()
        except Exception:
            log.exception("claim loop error", extra={"loop": n})
            worked = False
        if not worked:
            await sleep_or_stop(stop, settings.poll_interval_s)


async def heartbeat_loop(worker_id: str, leader: Leader, processor: Processor, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with processor.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO worker_heartbeats (worker_id, last_seen, is_leader) VALUES (%s, now(), %s) "
                    "ON CONFLICT (worker_id) DO UPDATE SET last_seen=now(), is_leader=EXCLUDED.is_leader",
                    (worker_id, leader.is_leader),
                )
        except Exception:
            log.exception("heartbeat failed")
        await sleep_or_stop(stop, 5)


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    # Render free plans have no background worker. When PORT is set, this process is a free
    # web service: the booking loops keep running, and /health answers Render's check.
    early_stop = asyncio.Event()
    health_task = asyncio.create_task(health_server(early_stop)) if os.environ.get("PORT") else None
    await migrate(settings.database_url)
    llm = build_llm(settings)  # fails fast on missing key / unpriced model in real mode
    pool = await open_pool(settings.database_url, min_size=2, max_size=settings.worker_concurrency + 4)
    lab_http = httpx.AsyncClient(
        base_url=settings.lab_base_url, limits=httpx.Limits(max_connections=settings.worker_concurrency * 2)
    )
    out_http = httpx.AsyncClient()
    breaker = CircuitBreaker(settings.cb_failure_threshold, settings.cb_open_seconds)
    lab = LabClient(lab_http, breaker, pool, settings)
    bookings = BookingService(pool, lab, settings)
    agent = Agent(llm, ToolExecutor(lab, bookings, settings.max_failed_bookings), pool, settings, SystemClock())
    processor = Processor(pool, agent, settings)
    sender: Sender = (
        GraphSender(out_http, settings.whatsapp_access_token, settings.whatsapp_phone_number_id)
        if settings.whatsapp_mode == "graph"
        else MockSender()
    )

    stop = early_stop
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    leader = Leader(settings, pool, sender, bookings, stop)
    tasks = [asyncio.create_task(claim_loop(i, processor, stop, settings)) for i in range(settings.worker_concurrency)]
    tasks += [
        asyncio.create_task(leader.run()),
        asyncio.create_task(heartbeat_loop(worker_id, leader, processor, stop)),
    ]
    if health_task is not None:
        tasks.append(health_task)
    log.info(
        "worker started",
        extra={
            "worker_id": worker_id,
            "concurrency": settings.worker_concurrency,
            "llm_mode": settings.llm_mode,
            "llm_model": llm.model,
        },
    )

    await stop.wait()
    log.info("shutting down: finishing in-flight turns", extra={"grace_s": SHUTDOWN_GRACE_S})
    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
    for task in pending:  # unfinished turns are safe: their leases expire and another worker redoes them
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await lab_http.aclose()
    await out_http.aclose()
    await pool.close()
    log.info("worker stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except Exception:
        logging.getLogger("worker").exception("worker crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
