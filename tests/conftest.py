from collections.abc import AsyncIterator
from datetime import UTC, datetime

import psycopg
import pytest

from app.clock import FixedClock
from app.db import Pool, open_pool
from app.migrate import migrate
from tests.helpers import TEST_DSN

OPERATIONAL_TABLES = (
    "events, handoffs, llm_usage, outbound_messages, lab_calls, bookings, rate_limits, "
    "duplicate_deliveries, inbound_messages, conversations, worker_heartbeats"
)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 20, 6, 0, tzinfo=UTC))


@pytest.fixture(scope="session")
async def db_pool() -> AsyncIterator[Pool]:
    admin_dsn = TEST_DSN.rsplit("/", 1)[0] + "/postgres"
    db_name = TEST_DSN.rsplit("/", 1)[1]
    async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as conn:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    await migrate(TEST_DSN)
    pool = await open_pool(TEST_DSN, min_size=2, max_size=40)
    yield pool
    await pool.close()


@pytest.fixture
async def pool(db_pool: Pool) -> Pool:
    async with db_pool.connection() as conn:
        await conn.execute(f"TRUNCATE {OPERATIONAL_TABLES} RESTART IDENTITY")
    return db_pool
