"""Apply numbered SQL migrations in order, once each. Run: python -m app.migrate"""

import asyncio
import logging
import sys
from pathlib import Path

import psycopg

from app.config import get_settings
from app.obs.logs import setup_logging

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
log = logging.getLogger("migrate")


async def migrate(dsn: str) -> list[str]:
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        # Serialise concurrent migrators (e.g. tests and compose starting together).
        await conn.execute("SELECT pg_advisory_lock(727000)")
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {row[0] for row in await cur.fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727000)")
    return applied


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    applied = asyncio.run(migrate(settings.database_url))
    log.info("migrations applied", extra={"applied": applied})
    sys.exit(0)


if __name__ == "__main__":
    main()
