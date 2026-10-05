"""Práce s 30minutovými sloty a formáty časů."""
from __future__ import annotations

from datetime import datetime, timedelta

SLOT_MINUTES = 30
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES
DT_FMT = "%Y-%m-%d %H:%M"
TS_FMT = "%Y-%m-%d %H:%M:%S"


def format_dt(value: datetime) -> str:
    return value.strftime(DT_FMT)


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, DT_FMT)


def format_ts(value: datetime) -> str:
    return value.strftime(TS_FMT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FMT)


def floor_to_slot(value: datetime) -> datetime:
    return value.replace(minute=value.minute - value.minute % SLOT_MINUTES, second=0, microsecond=0)


def ceil_to_slot(value: datetime) -> datetime:
    floored = floor_to_slot(value)
    return floored if floored == value else floored + timedelta(minutes=SLOT_MINUTES)


def time_options() -> list[str]:
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(0, 24 * 60, SLOT_MINUTES)]


def format_range(start: str, end: str) -> str:
    """'15.09.2026 10:00–11:30' (u přechodu přes půlnoc i datum konce)."""
    s, e = parse_dt(start), parse_dt(end)
    if e.date() == s.date() or (e == datetime.combine(s.date(), datetime.min.time()) + timedelta(days=1)):
        return f"{s:%d.%m.%Y %H:%M}–{e:%H:%M}"
    return f"{s:%d.%m.%Y %H:%M} – {e:%d.%m.%Y %H:%M}"
