"""Claim a conversation, run one agent turn over its queued messages, commit or record failure."""

import asyncio
import contextlib
import logging
from typing import Any
from uuid import UUID, uuid4

from app.agent.loop import Agent
from app.config import Settings
from app.db import Pool
from app.lab.retry import backoff_delay
from app.obs.logs import mask_phone
from app.worker import queue
from app.worker.queue import Outbound

log = logging.getLogger("worker")

NON_TEXT_REPLY = "Sorry, I can only read text messages. Please type your request."
APOLOGY = "Sorry, something went wrong on our side. A staff member will follow up with you here."


class Processor:
    def __init__(self, pool: Pool, agent: Agent, settings: Settings) -> None:
        self.pool = pool
        self.agent = agent
        self.settings = settings

    async def process_one(self) -> bool:
        """Returns True if a conversation was claimed (so the caller polls again immediately)."""
        token = uuid4()
        conv = await queue.claim(self.pool, token, self.settings.lease_seconds)
        if conv is None:
            return False
        hb = asyncio.create_task(self._heartbeat(conv["id"], token))
        try:
            await self._handle(conv, token)
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        return True

    async def _heartbeat(self, conversation_id: int, token: UUID) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            if not await queue.heartbeat(self.pool, conversation_id, token, self.settings.lease_seconds):
                log.warning("lease lost during turn", extra={"conversation_id": conversation_id})
                return

    async def _handle(self, conv: dict[str, Any], token: UUID) -> None:
        cid = int(conv["id"])
        phone = str(conv["phone"])
        msgs = await queue.load_queued(self.pool, cid)
        if not msgs:
            await queue.release_empty(self.pool, cid, token)
            return
        ids = [int(m["id"]) for m in msgs]
        texts = [m["body"] for m in msgs if m["msg_type"] == "text" and m["body"].strip()]
        log_extra = {"conversation_id": cid, "phone": mask_phone(phone), "messages": len(ids)}
        try:
            if not texts:
                committed = await queue.commit_turn(
                    self.pool,
                    cid,
                    phone,
                    token,
                    ids,
                    str(conv["state"]),
                    dict(conv["context"]),
                    list(conv["history"]),
                    [Outbound(NON_TEXT_REPLY, f"reply:{cid}:{max(ids)}")],
                    None,
                    [],
                )
            else:
                result = await self.agent.run_turn(
                    cid, phone, str(conv["state"]), dict(conv["context"]), list(conv["history"]), "\n".join(texts)
                )
                committed = await queue.commit_turn(
                    self.pool,
                    cid,
                    phone,
                    token,
                    ids,
                    result.state,
                    result.context,
                    result.history,
                    [Outbound(result.reply, f"reply:{cid}:{max(ids)}")],
                    result.effects.handoff,
                    result.effects.notes,
                )
                log.info(
                    "turn done",
                    extra={
                        **log_extra,
                        "state": result.state,
                        "llm_calls": result.llm_calls,
                        "handoff": result.effects.handoff[0] if result.effects.handoff else None,
                    },
                )
            if not committed:
                log.warning("lease lost before commit; another worker will redo the turn", extra=log_extra)
                await self._event_lease_lost(cid)
        except Exception as exc:
            attempts = max(int(m["attempts"]) for m in msgs) + 1
            delay = backoff_delay(attempts, 1.0, 30.0)
            outcome = await queue.fail_turn(
                self.pool,
                cid,
                phone,
                token,
                ids,
                f"{type(exc).__name__}: {exc}",
                self.settings.max_message_attempts,
                delay,
                APOLOGY,
            )
            log.exception("turn failed", extra={**log_extra, "outcome": outcome, "attempt": attempts})

    async def _event_lease_lost(self, cid: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO events (kind, conversation_id) VALUES ('lease_lost', %s)", (cid,))
