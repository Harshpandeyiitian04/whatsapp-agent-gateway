"""In-memory lab state: catalog, slots, bookings, and the double-booking detector."""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from app.clock import IST, Clock, today_ist

TESTS: list[dict[str, Any]] = [
    {"code": "CBC", "name": "Complete Blood Count", "price_inr": 350, "fasting": False},
    {"code": "LIPID", "name": "Lipid Profile", "price_inr": 600, "fasting": True},
    {"code": "TSH", "name": "Thyroid Stimulating Hormone", "price_inr": 400, "fasting": False},
    {"code": "HBA1C", "name": "HbA1c (3-month sugar)", "price_inr": 550, "fasting": False},
    {"code": "VITD", "name": "Vitamin D (25-OH)", "price_inr": 1200, "fasting": False},
]
TEST_CODES = {t["code"] for t in TESTS}
SLOT_DAYS = 7
SLOT_TIMES = [time(8, 0), time(8, 30), time(9, 0), time(9, 30), time(10, 0), time(10, 30), time(11, 0), time(11, 30)]


def slot_id_for(day: date, t: time) -> str:
    return f"S-{day:%Y%m%d}-{t:%H%M}"


@dataclass
class Booking:
    booking_id: str
    idempotency_key: str
    test_code: str
    slot_id: str
    patient_name: str
    phone: str
    created_at: str


@dataclass
class LabStore:
    slots: dict[str, datetime] = field(default_factory=dict)
    bookings: list[Booking] = field(default_factory=list)
    _seq: int = 0

    def reset(self, clock: Clock) -> None:
        """Slots for the next SLOT_DAYS days starting tomorrow (IST), so never in the past."""
        start = today_ist(clock) + timedelta(days=1)
        self.slots = {
            slot_id_for(day, t): datetime.combine(day, t, tzinfo=IST)
            for day in (start + timedelta(days=i) for i in range(SLOT_DAYS))
            for t in SLOT_TIMES
        }
        self.bookings = []
        self._seq = 0

    def booked_slot_ids(self) -> set[str]:
        return {b.slot_id for b in self.bookings}

    def available(self, day: date | None, part_of_day: str, limit: int) -> list[dict[str, str]]:
        taken = self.booked_slot_ids()
        out = []
        for sid, start in sorted(self.slots.items(), key=lambda kv: kv[1]):
            if sid in taken or (day is not None and start.date() != day):
                continue
            if part_of_day == "early" and start.time() >= time(10, 0):
                continue
            if part_of_day == "late" and start.time() < time(10, 0):
                continue
            out.append({"slot_id": sid, "start": start.isoformat()})
            if len(out) >= limit:
                break
        return out

    def find_by_key(self, key: str) -> list[Booking]:
        return [b for b in self.bookings if b.idempotency_key == key]

    def create(self, key: str, test_code: str, slot_id: str, patient_name: str, phone: str, now: datetime) -> Booking:
        self._seq += 1
        booking = Booking(f"LB-{self._seq:06d}", key, test_code, slot_id, patient_name, phone, now.isoformat())
        self.bookings.append(booking)
        return booking

    def double_bookings(self) -> dict[str, Any]:
        by_slot: dict[str, list[str]] = {}
        by_patient: dict[str, list[str]] = {}
        for b in self.bookings:
            by_slot.setdefault(b.slot_id, []).append(b.booking_id)
            by_patient.setdefault(f"{b.phone}|{b.test_code}", []).append(b.booking_id)
        return {
            "slot_violations": [{"slot_id": k, "booking_ids": v} for k, v in by_slot.items() if len(v) > 1],
            "patient_violations": [
                {"patient_test": k.split("|", 1)[1], "phone_suffix": k.split("|", 1)[0][-4:], "booking_ids": v}
                for k, v in by_patient.items()
                if len(v) > 1
            ],
            "total_bookings": len(self.bookings),
        }


def booking_json(b: Booking) -> dict[str, Any]:
    data = asdict(b)
    data["status"] = "confirmed"
    return data
