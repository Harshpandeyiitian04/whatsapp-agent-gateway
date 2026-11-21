import asyncio
import contextlib


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, returning early when `stop` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
