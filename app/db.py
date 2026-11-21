"""Connection pool helpers (psycopg 3, async). Rows are dicts."""

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

Pool = AsyncConnectionPool[AsyncConnection[DictRow]]


async def open_pool(dsn: str, min_size: int, max_size: int) -> Pool:
    pool = Pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        connection_class=AsyncConnection[DictRow],
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    return pool
