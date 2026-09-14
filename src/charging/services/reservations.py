"""Doménová logika rezervací nabíjecích stanic.

Stavy rezervace:  DRAFT -> PENDING -> CONFIRMED, z libovolného aktivního stavu -> CANCELLED.
Operace: create, submit (DRAFT -> PENDING), approve (PENDING -> CONFIRMED), cancel, check availability.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Callable

from ..db import transaction
from ..timeutil import (
    SLOT_MINUTES,
    SLOTS_PER_DAY,
    ceil_to_slot,
    format_dt,
    format_ts,
    parse_dt,
)

logger = logging.getLogger(__name__)

MAX_SLOTS = 8  # max. 4 hodiny
BOOKING_HORIZON_DAYS = 14

DRAFT = "DRAFT"
PENDING = "PENDING"
CONFIRMED = "CONFIRMED"
CANCELLED = "CANCELLED"

VEHICLE_BEV = "BEV"
VEHICLE_PHEV = "PHEV"

SLOT_FREE = "FREE"
SLOT_RESERVED = "RESERVED"
SLOT_IN_USE = "IN_USE"
SLOT_PAST = "PAST"

HIGH_SPEED_MESSAGE = (
    "Vysokorychlostní nabíječku může rezervovat pouze plně elektrický vůz (BEV), ne plug-in hybrid."
)

_INTEGRITY_MESSAGES = {
    "CONFIRMED_OVERLAP": "Termín se překrývá s jinou potvrzenou rezervací této stanice.",
    "HIGH_SPEED_BEV_ONLY": HIGH_SPEED_MESSAGE,
    "INVALID_STATE_TRANSITION": "Tento přechod stavu rezervace není povolen.",
}


class ReservationError(Exception):
    """Porušení business pravidla nebo neplatný požadavek."""


class NotFoundError(ReservationError):
    pass


class PermissionDeniedError(ReservationError):
    pass


class ConflictError(ReservationError):
    """Termín se překrývá s potvrzenou rezervací stejné stanice."""


def parse_slot_start(date_str: str | None, time_str: str | None) -> datetime:
    try:
        value = datetime.strptime(f"{(date_str or '').strip()} {(time_str or '').strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ReservationError("Neplatné datum nebo čas.") from None
    if value.minute % SLOT_MINUTES:
        raise ReservationError("Rezervace musí začínat na začátku 30minutového slotu (celá nebo půl hodina).")
    return value


def check_vehicle_allowed(station, user) -> None:
    if station["is_high_speed"] and user["vehicle_type"] != VEHICLE_BEV:
        raise ReservationError(HIGH_SPEED_MESSAGE)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Transakce, která překládá porušení DB triggerů na doménovou chybu."""
    try:
        with transaction(conn):
            yield
    except sqlite3.IntegrityError as exc:
        message = next((text for code, text in _INTEGRITY_MESSAGES.items() if code in str(exc)), None)
        if message is None:
            raise
        raise ReservationError(message) from exc


class ReservationService:
    def __init__(self, conn: sqlite3.Connection, notifier=None, clock: Callable[[], datetime] = datetime.now):
        self.conn = conn
        self.notifier = notifier
        self.clock = clock

    # ------------------------------------------------------------------ dotazy

    def now(self) -> datetime:
        return self.clock()

    def get_user(self, user_id: int):
        row = self.conn.execute(
            "SELECT id, username, vehicle_type, is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("Uživatel neexistuje.")
        return row

    def get_station(self, station_id: int):
        row = self.conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        if row is None:
            raise NotFoundError("Nabíjecí stanice neexistuje.")
        return row

    def _get_reservation_row(self, reservation_id: int):
        row = self.conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        if row is None:
            raise NotFoundError("Rezervace neexistuje.")
        return row

    def get_reservation(self, reservation_id: int) -> dict:
        rows = self._query_reservations("WHERE r.id = ?", (reservation_id,))
        if not rows:
            raise NotFoundError("Rezervace neexistuje.")
        return rows[0]

    def list_for_user(self, user_id: int) -> list[dict]:
        return self._query_reservations("WHERE r.user_id = ? ORDER BY r.start_time DESC, r.id DESC", (user_id,))

    def list_all(self, state: str | None = None) -> list[dict]:
        if state in (DRAFT, PENDING, CONFIRMED, CANCELLED):
            return self._query_reservations("WHERE r.state = ? ORDER BY r.start_time, r.id", (state,))
        return self._query_reservations("ORDER BY r.start_time DESC, r.id DESC", ())

    def _query_reservations(self, where_order: str, params: tuple) -> list[dict]:
        rows = self.conn.execute(
            f"""
            SELECT r.*, s.name AS station_name, s.power_kw, s.is_high_speed,
                   u.username, u.vehicle_type,
                   EXISTS (
                       SELECT 1 FROM reservations c
                       WHERE c.station_id = r.station_id AND c.state = 'CONFIRMED' AND c.id <> r.id
                         AND c.start_time < r.end_time AND r.start_time < c.end_time
                   ) AS has_conflict
            FROM reservations r
            JOIN stations s ON s.id = r.station_id
            JOIN users u ON u.id = r.user_id
            {where_order}
            """,
            params,
        ).fetchall()
        now = self.now()
        return [self._decorate(dict(row), now) for row in rows]

    @staticmethod
    def _decorate(res: dict, now: datetime) -> dict:
        start, end = parse_dt(res["start_time"]), parse_dt(res["end_time"])
        state = res["state"]
        res["has_conflict"] = bool(res["has_conflict"])
        res["can_submit"] = state == DRAFT and start >= now
        res["can_approve"] = state == PENDING and end > now and not res["has_conflict"]
        res["can_cancel"] = state in (DRAFT, PENDING) or (state == CONFIRMED and end > now)
        if state == CONFIRMED and start <= now < end:
            res["usage"] = "Probíhá nabíjení"
        elif state == CONFIRMED and end <= now:
            res["usage"] = "Proběhla"
        else:
            res["usage"] = ""
        return res

    def find_confirmed_conflicts(self, station_id: int, start: datetime, end: datetime, exclude_id: int | None = None):
        return self.conn.execute(
            """
            SELECT id, start_time, end_time FROM reservations
            WHERE station_id = ? AND state = 'CONFIRMED'
              AND start_time < ? AND ? < end_time AND id <> ?
            """,
            (station_id, format_dt(end), format_dt(start), exclude_id if exclude_id is not None else -1),
        ).fetchall()

    # ------------------------------------------------------ check availability

    def _validate_request(self, user, station, start: datetime, slots, now: datetime) -> datetime:
        if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= MAX_SLOTS:
            raise ReservationError(f"Počet slotů musí být 1 až {MAX_SLOTS} (30 minut až {MAX_SLOTS // 2} hodiny).")
        if start.minute % SLOT_MINUTES or start.second or start.microsecond:
            raise ReservationError("Rezervace musí začínat na začátku 30minutového slotu (celá nebo půl hodina).")
        if start < now:
            raise ReservationError("Nelze rezervovat termín, který už začal nebo proběhl.")
        if start.date() > now.date() + timedelta(days=BOOKING_HORIZON_DAYS):
            raise ReservationError(f"Rezervovat lze nejvýše {BOOKING_HORIZON_DAYS} dní dopředu.")
        if user is not None:
            check_vehicle_allowed(station, user)
        end = start + timedelta(minutes=SLOT_MINUTES * slots)
        if self.find_confirmed_conflicts(station["id"], start, end):
            raise ConflictError("Termín je obsazený – překrývá se s potvrzenou rezervací.")
        return end

    def check_availability(self, station_id: int, start: datetime, slots, user_id: int | None = None) -> tuple[bool, str]:
        station = self.get_station(station_id)
        user = self.get_user(user_id) if user_id is not None else None
        try:
            self._validate_request(user, station, start, slots, self.now())
        except ReservationError as exc:
            return False, str(exc)
        return True, "Termín je volný."

    def availability(self, station_id: int, day: date, viewer_id: int | None = None) -> list[dict]:
        self.get_station(station_id)
        now = self.now()
        day_start = datetime.combine(day, time.min)
        day_end = day_start + timedelta(days=1)
        rows = self.conn.execute(
            """
            SELECT id, user_id, start_time, end_time, state FROM reservations
            WHERE station_id = ? AND state IN ('PENDING', 'CONFIRMED')
              AND start_time < ? AND ? < end_time
            """,
            (station_id, format_dt(day_end), format_dt(day_start)),
        ).fetchall()

        slots = []
        for i in range(SLOTS_PER_DAY):
            s = day_start + timedelta(minutes=SLOT_MINUTES * i)
            e = s + timedelta(minutes=SLOT_MINUTES)
            s_str, e_str = format_dt(s), format_dt(e)
            overlapping = [r for r in rows if r["start_time"] < e_str and s_str < r["end_time"]]
            confirmed = next((r for r in overlapping if r["state"] == CONFIRMED), None)
            if confirmed is not None:
                state = SLOT_IN_USE if s <= now < e else SLOT_RESERVED
            elif s < now:
                state = SLOT_PAST
            else:
                state = SLOT_FREE
            slots.append(
                {
                    "start": s,
                    "end": e,
                    "time": f"{s:%H:%M}",
                    "state": state,
                    "pending_count": sum(1 for r in overlapping if r["state"] == PENDING),
                    "mine": confirmed is not None and viewer_id is not None and confirmed["user_id"] == viewer_id,
                }
            )
        return slots

    def stations_overview(self) -> list[dict]:
        """Aktuální stav stanic: IN_USE (běží potvrzená rezervace), RESERVED (potvrzená
        rezervace ještě dnes), jinak FREE."""
        now = self.now()
        midnight = datetime.combine(now.date(), time.min) + timedelta(days=1)
        stations = self.conn.execute("SELECT * FROM stations ORDER BY id").fetchall()
        upcoming = self.conn.execute(
            """
            SELECT station_id, start_time, end_time FROM reservations
            WHERE state = 'CONFIRMED' AND end_time > ? AND start_time < ?
            ORDER BY start_time
            """,
            (format_dt(now), format_dt(midnight)),
        ).fetchall()
        result = []
        for st in stations:
            mine = [r for r in upcoming if r["station_id"] == st["id"]]
            current = next((r for r in mine if parse_dt(r["start_time"]) <= now < parse_dt(r["end_time"])), None)
            future = next((r for r in mine if parse_dt(r["start_time"]) > now), None)
            if current is not None:
                status, info = SLOT_IN_USE, f"do {current['end_time'][11:]}"
            elif future is not None:
                status, info = SLOT_RESERVED, f"dnes od {future['start_time'][11:]}"
            else:
                status, info = SLOT_FREE, ""
            result.append({**dict(st), "status": status, "status_info": info})
        return result

    # ------------------------------------------------------------ operace

    def create(self, user_id: int, station_id: int, start: datetime, slots) -> int:
        """Create reservation -> stav DRAFT."""
        now = self.now()
        with _atomic(self.conn):
            user = self.get_user(user_id)
            station = self.get_station(station_id)
            end = self._validate_request(user, station, start, slots, now)
            ts = format_ts(now)
            cur = self.conn.execute(
                "INSERT INTO reservations (user_id, station_id, start_time, end_time, state, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, station_id, format_dt(start), format_dt(end), DRAFT, ts, ts),
            )
            reservation_id = cur.lastrowid
        return reservation_id

    def submit(self, user_id: int, reservation_id: int) -> None:
        """DRAFT -> PENDING (odeslání ke schválení)."""
        now = self.now()
        with _atomic(self.conn):
            res = self._get_reservation_row(reservation_id)
            if res["user_id"] != user_id:
                raise PermissionDeniedError("Ke schválení lze odeslat pouze vlastní rezervaci.")
            if res["state"] != DRAFT:
                raise ReservationError("Ke schválení lze odeslat pouze rezervaci ve stavu DRAFT.")
            start, end = parse_dt(res["start_time"]), parse_dt(res["end_time"])
            if start < now:
                raise ReservationError("Termín konceptu už začal – zrušte ho a vytvořte novou rezervaci.")
            check_vehicle_allowed(self.get_station(res["station_id"]), self.get_user(res["user_id"]))
            if self.find_confirmed_conflicts(res["station_id"], start, end, exclude_id=reservation_id):
                raise ConflictError("Termín je mezitím obsazený – překrývá se s potvrzenou rezervací.")
            self._set_state(reservation_id, PENDING, now)
        self._notify("reservation_submitted", self.get_reservation(reservation_id))

    def approve(self, admin_id: int, reservation_id: int) -> None:
        """PENDING -> CONFIRMED (schválení administrátorem)."""
        now = self.now()
        with _atomic(self.conn):
            admin = self.get_user(admin_id)
            if not admin["is_admin"]:
                raise PermissionDeniedError("Schvalovat rezervace může pouze administrátor.")
            res = self._get_reservation_row(reservation_id)
            if res["state"] != PENDING:
                raise ReservationError("Potvrdit lze pouze rezervaci ve stavu PENDING.")
            start, end = parse_dt(res["start_time"]), parse_dt(res["end_time"])
            if end <= now:
                raise ReservationError("Rezervace už proběhla, nelze ji potvrdit.")
            check_vehicle_allowed(self.get_station(res["station_id"]), self.get_user(res["user_id"]))
            if self.find_confirmed_conflicts(res["station_id"], start, end, exclude_id=reservation_id):
                raise ConflictError("Rezervaci nelze potvrdit – překrývá se s jinou potvrzenou rezervací této stanice.")
            self._set_state(reservation_id, CONFIRMED, now)
        self._notify("reservation_confirmed", self.get_reservation(reservation_id))

    def cancel(self, actor_id: int, reservation_id: int) -> None:
        """DRAFT | PENDING | CONFIRMED -> CANCELLED."""
        now = self.now()
        with _atomic(self.conn):
            actor = self.get_user(actor_id)
            res = self._get_reservation_row(reservation_id)
            is_owner = res["user_id"] == actor["id"]
            if not is_owner and not actor["is_admin"]:
                raise PermissionDeniedError("Zrušit lze pouze vlastní rezervaci.")
            if res["state"] == CANCELLED:
                raise ReservationError("Rezervace už je zrušená.")
            if res["state"] == CONFIRMED and parse_dt(res["end_time"]) <= now:
                raise ReservationError("Proběhlou rezervaci nelze zrušit.")
            self._set_state(reservation_id, CANCELLED, now)

        if not is_owner:
            self._notify("reservation_cancelled_by_admin", self.get_reservation(reservation_id))
        if res["state"] == CONFIRMED:
            released_start = max(parse_dt(res["start_time"]), ceil_to_slot(now))
            released_end = parse_dt(res["end_time"])
            if released_start < released_end:
                self._notify(
                    "slot_released",
                    station_id=res["station_id"],
                    start=released_start,
                    end=released_end,
                    exclude_user_id=res["user_id"],
                )

    def _set_state(self, reservation_id: int, state: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE reservations SET state = ?, updated_at = ? WHERE id = ?",
            (state, format_ts(now), reservation_id),
        )

    def _notify(self, event: str, *args, **kwargs) -> None:
        """Hranice systému: selhání notifikace nesmí zvrátit už potvrzenou operaci."""
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, event)(*args, **kwargs)
        except Exception:
            logger.exception("Notification event %s failed", event)
