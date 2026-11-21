"""Staff/ops endpoints. All require `Authorization: Bearer <ADMIN_TOKEN>`."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import require_admin
from app.api.state import AppState
from app.api.timing import RECENT

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# Operational tables wiped by the load-test reset. llm_spend_ledger is deliberately absent.
RESETTABLE_TABLES = (
    "events, handoffs, llm_usage, outbound_messages, lab_calls, bookings, rate_limits, "
    "duplicate_deliveries, inbound_messages, conversations"
)


def _state(request: Request) -> AppState:
    state: AppState = request.app.state.app_state
    return state


@router.get("/handoffs")
async def list_handoffs(request: Request, open_only: bool = True) -> list[dict[str, Any]]:
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, conversation_id, reason, summary, created_at, released_at FROM handoffs "
            "WHERE (NOT %s OR released_at IS NULL) ORDER BY id DESC LIMIT 200",
            (open_only,),
        )
        return list(await cur.fetchall())


@router.post("/conversations/{conversation_id}/release")
async def release(request: Request, conversation_id: int) -> dict[str, Any]:
    async with _state(request).pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE conversations SET state='COLLECTING', updated_at=now() "
            "WHERE id=%s AND state='HANDOFF' RETURNING id",
            (conversation_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="conversation not in HANDOFF")
        await conn.execute(
            "UPDATE handoffs SET released_at=now() WHERE conversation_id=%s AND released_at IS NULL",
            (conversation_id,),
        )
        held = await conn.execute(
            "UPDATE inbound_messages SET status='done', processed_at=now() WHERE conversation_id=%s AND status='held'",
            (conversation_id,),
        )
    return {"released": conversation_id, "held_messages_closed": held.rowcount}


@router.get("/dlq")
async def list_dlq(request: Request) -> list[dict[str, Any]]:
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, wa_message_id, conversation_id, attempts, last_error, received_at "
            "FROM inbound_messages WHERE status='dead' ORDER BY id LIMIT 500"
        )
        return list(await cur.fetchall())


@router.post("/dlq/{message_id}/requeue")
async def requeue(request: Request, message_id: int) -> dict[str, Any]:
    async with _state(request).pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "UPDATE inbound_messages SET status='queued', attempts=0, last_error=NULL "
            "WHERE id=%s AND status='dead' RETURNING conversation_id",
            (message_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="message not in DLQ")
        await conn.execute(
            "UPDATE conversations SET has_pending=true, pending_since=coalesce(pending_since, now()), "
            "next_attempt_at='-infinity', state=CASE WHEN state='HANDOFF' THEN 'COLLECTING' ELSE state END "
            "WHERE id=%s",
            (row["conversation_id"],),
        )
    return {"requeued": message_id}


@router.post("/reset-mock-data")
async def reset_mock_data(request: Request) -> dict[str, Any]:
    state = _state(request)
    if not state.settings.allow_reset:
        raise HTTPException(status_code=403, detail="ALLOW_RESET is false")
    async with state.pool.connection() as conn:
        await conn.execute(f"TRUNCATE {RESETTABLE_TABLES} RESTART IDENTITY")
    RECENT.clear()
    return {"reset": True, "llm_spend_ledger": "kept"}
