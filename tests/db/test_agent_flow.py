"""Full agent turns with the mock LLM against the scripted fake lab."""

from app.agent.llm.mock import MockLLM
from app.agent.loop import Agent
from app.agent.tools import ToolExecutor
from app.clock import FixedClock
from app.db import Pool
from app.lab.booking import BookingService
from tests.fakelab import FakeLab, lab_client, no_sleep
from tests.helpers import make_settings

PHONE = "919999900001"


async def _agent(pool: Pool, fake: FakeLab, clock: FixedClock) -> Agent:
    settings = make_settings()
    lab = lab_client(fake, settings, pool)
    bookings = BookingService(pool, lab, settings, sleep=no_sleep)
    async with pool.connection() as conn:
        await conn.execute("INSERT INTO conversations (phone) VALUES (%s)", (PHONE,))
    return Agent(MockLLM("claude-haiku-5-5"), ToolExecutor(lab, bookings, 3), pool, settings, clock)


async def test_check_then_book(pool: Pool, clock: FixedClock) -> None:
    fake = FakeLab()
    agent = await _agent(pool, fake, clock)
    first = await agent.run_turn(1, PHONE, "NEW", {}, [], "Hi, I want a CBC test tomorrow")
    assert first.state == "SLOTS_OFFERED" and "1) Sat 21 Nov, 08:00" in first.reply
    second = await agent.run_turn(1, PHONE, first.state, first.context, first.history, "Slot 2, my name is Asha Rao")
    assert second.state == "BOOKED" and "Booked!" in second.reply
    assert second.context["booking"]["slot_id"] == "S-20261121-0830"
    assert len(second.history) == 4
    assert [b["slot_id"] for b in fake.bookings] == ["S-20261121-0830"]


async def test_coalesced_messages_book_in_one_turn(pool: Pool, clock: FixedClock) -> None:
    fake = FakeLab()
    agent = await _agent(pool, fake, clock)
    text = "Book CBC tomorrow\nBook slot S-20261121-0800, my name is Ravi K"
    result = await agent.run_turn(1, PHONE, "NEW", {}, [], text)
    assert result.state == "BOOKED" and result.llm_calls == 3  # check_slots, book_test, final text


async def test_lab_outage_on_check_slots_hands_off(pool: Pool, clock: FixedClock) -> None:
    fake = FakeLab()
    fake.slots_script.extend(["error"] * 4)
    agent = await _agent(pool, fake, clock)
    result = await agent.run_turn(1, PHONE, "NEW", {}, [], "CBC tomorrow please")
    assert result.state == "HANDOFF" and result.effects.handoff is not None
    assert result.effects.handoff[0] == "lab_outage"


async def test_medical_question_hands_off_without_lab_calls(pool: Pool, clock: FixedClock) -> None:
    fake = FakeLab()
    agent = await _agent(pool, fake, clock)
    result = await agent.run_turn(1, PHONE, "NEW", {}, [], "I have fever and pain, what medicine should I take?")
    assert result.effects.handoff is not None and result.effects.handoff[0] == "medical_question"
    assert fake.posts == 0
