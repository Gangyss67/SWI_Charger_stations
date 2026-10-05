"""Doménová logika rezervací nabíjecích stanic – specifikace baseline v0.2 (cv2.docx).

Operace a přechody stavů:
  OP-01 Check Availability   search_availability(), availability()  stav se nemění
  OP-02 Create Reservation   create()             [none] -> DRAFT (platnost 5 min, BR-05)
  OP-03 Confirm Reservation  confirm()            DRAFT -> CONFIRMED (AC) | PENDING_APPROVAL (DC/HPC, BR-06)
  OP-04 Cancel Reservation   cancel()             CONFIRMED -> CANCELLED | CANCELLED_BY_ADMIN (BR-03)
                                                  PENDING_APPROVAL -> CANCELLED
  OP-05 Approve / Reject     approve(), reject()  PENDING_APPROVAL -> CONFIRMED | REJECTED
  časové přechody            expire_overdue()     DRAFT | PENDING_APPROVAL -> EXPIRED (BR-05, BR-06)

Události pro Notification Service se zapisují do tabulky outbox ve stejné transakci jako změna
stavu (ADR-01). Doručuje je services/outbox.py – tato služba Notification Service vůbec nezná.
"""
from __future__ import annotations

import json
import re
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
    parse_ts,
)

# Technické limity vstupu z C01 – baseline je nedefinuje (viz evidence C02, zbývající předpoklad).
MAX_SLOTS = 8  # max. 4 hodiny
BOOKING_HORIZON_DAYS = 14

DRAFT_TTL = timedelta(minutes=5)  # BR-05
APPROVAL_LEAD = timedelta(minutes=15)  # BR-06: rozhodnout nejpozději T_start − 15 min
USER_CANCEL_LEAD = timedelta(minutes=30)  # BR-03: uživatel ruší nejpozději T_start − 30 min
APPROVAL_POWER_KW = 50  # BR-06: DC (>= 50 kW) a HPC vyžadují schválení

DRAFT = "DRAFT"
PENDING_APPROVAL = "PENDING_APPROVAL"
CONFIRMED = "CONFIRMED"
CANCELLED = "CANCELLED"
CANCELLED_BY_ADMIN = "CANCELLED_BY_ADMIN"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
STATES = (DRAFT, PENDING_APPROVAL, CONFIRMED, CANCELLED, CANCELLED_BY_ADMIN, REJECTED, EXPIRED)
BLOCKING_STATES = (CONFIRMED, PENDING_APPROVAL)  # BR-02 v0.2
_BLOCKING_SQL = "('CONFIRMED', 'PENDING_APPROVAL')"

VEHICLE_BEV = "BEV"
VEHICLE_PHEV = "PHEV"
VEHICLE_TYPES = (VEHICLE_BEV, VEHICLE_PHEV)

SLOT_FREE = "FREE"
SLOT_RESERVED = "RESERVED"
SLOT_PENDING = "PENDING"
SLOT_IN_USE = "IN_USE"
SLOT_PAST = "PAST"

# Důvod nedostupnosti stanice v OP-01.
UNAVAILABLE_COLLISION = "COLLISION"
UNAVAILABLE_PENDING = "PENDING_APPROVAL"
UNAVAILABLE_VEHICLE = "VEHICLE"

PLATE_MAX_LENGTH = 12
_PLATE_RE = re.compile(r"[A-Z0-9](?:[A-Z0-9 -]*[A-Z0-9])?")

HIGH_SPEED_MESSAGE = (
    "Vysokorychlostní nabíječku (HPC) může rezervovat pouze plně elektrický vůz (BEV), ne plug-in hybrid."
)
OVERLAP_MESSAGE = (
    "Termín se na této stanici překrývá s potvrzenou rezervací nebo se žádostí čekající na schválení."
)


class ReservationError(Exception):
    """Porušení business pravidla nebo neplatný vstup."""


class NotFoundError(ReservationError):
    pass


class PermissionDeniedError(ReservationError):
    pass


class ConflictError(ReservationError):
    """Interval koliduje s rezervací v blokujícím stavu (BR-02)."""


class InvalidStateError(ReservationError):
    """Operaci nelze provést v aktuálním stavu rezervace nebo už vypršela lhůta."""


_INTEGRITY_ERRORS = {
    "BLOCKING_OVERLAP": (ConflictError, OVERLAP_MESSAGE),
    "HIGH_SPEED_BEV_ONLY": (ReservationError, HIGH_SPEED_MESSAGE),
    "INVALID_STATE_TRANSITION": (InvalidStateError, "Tento přechod stavu rezervace není povolen."),
    "INVALID_INITIAL_STATE": (InvalidStateError, "Rezervace musí vzniknout ve stavu DRAFT."),
    "APPROVAL_REQUIRED": (InvalidStateError, "Rezervace DC/HPC stanice musí projít schválením administrátorem."),
}


def parse_slot_start(date_str: str | None, time_str: str | None) -> datetime:
    try:
        value = datetime.strptime(f"{(date_str or '').strip()} {(time_str or '').strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ReservationError("Neplatné datum nebo čas.") from None
    if value.minute % SLOT_MINUTES:
        raise ReservationError("Rezervace musí začínat na začátku 30minutového slotu (celá nebo půl hodina).")
    return value


def parse_vehicle_type(value) -> str:
    if value not in VEHICLE_TYPES:
        raise ReservationError("Typ vozu musí být BEV nebo PHEV.")
    return value


def normalize_license_plate(value) -> str:
    plate = " ".join(str(value or "").upper().split())
    if not plate:
        raise ReservationError("Vyplňte SPZ vozidla.")
    if len(plate) > PLATE_MAX_LENGTH or not _PLATE_RE.fullmatch(plate):
        raise ReservationError(
            f"SPZ smí obsahovat jen písmena A–Z, číslice, mezeru a pomlčku (max. {PLATE_MAX_LENGTH} znaků)."
        )
    return plate


def requires_approval(station) -> bool:
    """BR-06: DC (>= 50 kW) a HPC stanice vyžadují schválení administrátorem."""
    return station["power_kw"] >= APPROVAL_POWER_KW


def check_vehicle_allowed(station, vehicle_type: str) -> None:
    """BR-04: HPC jen pro BEV."""
    if station["is_high_speed"] and vehicle_type != VEHICLE_BEV:
        raise ReservationError(HIGH_SPEED_MESSAGE)


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Transakce, která překládá porušení DB triggerů na doménovou chybu."""
    try:
        with transaction(conn):
            yield
    except sqlite3.IntegrityError as exc:
        for code, (error_cls, message) in _INTEGRITY_ERRORS.items():
            if code in str(exc):
                raise error_cls(message) from exc
        raise


class ReservationService:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = datetime.now):
        self.conn = conn
        self.clock = clock

    def now(self) -> datetime:
        return self.clock()

    # ------------------------------------------------------- časové přechody

    def expire_overdue(self, now: datetime | None = None) -> int:
        """BR-05 + BR-06: DRAFT po 5 minutách a žádost, o které admin nerozhodl do T_start − 15 min,
        přejdou do EXPIRED a přestanou blokovat stanici.

        Baseline v0.2 nemá plánovač (architektonický driver pro C03), proto se přechod provádí
        „líně“: jedním samostatně commitovaným UPDATE na začátku každé operace a dotazu.
        Musí běžet mimo otevřenou transakci, aby ho neodvolal rollback odmítnuté operace.
        """
        ts = format_ts(now or self.now())
        cur = self.conn.execute(
            """
            UPDATE reservations SET state = 'EXPIRED', updated_at = ?
            WHERE (state = 'DRAFT' AND draft_expires_at < ?)
               OR (state = 'PENDING_APPROVAL' AND approval_deadline < ?)
            """,
            (ts, ts, ts),
        )
        return cur.rowcount

    # ------------------------------------------------------------------ dotazy

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

    def list_stations(self):
        return self.conn.execute("SELECT * FROM stations ORDER BY id").fetchall()

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
        self.expire_overdue()
        return self._query_reservations("WHERE r.user_id = ? ORDER BY r.start_time DESC, r.id DESC", (user_id,))

    def list_all(self, state: str | None = None) -> list[dict]:
        self.expire_overdue()
        if state == PENDING_APPROVAL:
            # fronta administrátora: nejdřív žádosti s nejbližším termínem rozhodnutí
            return self._query_reservations("WHERE r.state = ? ORDER BY r.approval_deadline, r.id", (state,))
        if state in STATES:
            return self._query_reservations("WHERE r.state = ? ORDER BY r.start_time, r.id", (state,))
        return self._query_reservations("ORDER BY r.start_time DESC, r.id DESC", ())

    def pending_approval_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM reservations WHERE state = 'PENDING_APPROVAL' AND approval_deadline >= ?",
            (format_ts(self.now()),),
        ).fetchone()[0]

    def _query_reservations(self, where_order: str, params: tuple) -> list[dict]:
        rows = self.conn.execute(
            f"""
            SELECT r.*, s.name AS station_name, s.power_kw, s.is_high_speed, u.username,
                   EXISTS (
                       SELECT 1 FROM reservations c
                       WHERE c.station_id = r.station_id AND c.state IN {_BLOCKING_SQL} AND c.id <> r.id
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
        """Doplní příznaky pro UI a API – kterou operaci lze nad rezervací právě teď provést."""
        start, end = parse_dt(res["start_time"]), parse_dt(res["end_time"])
        state = res["state"]
        res["has_conflict"] = bool(res["has_conflict"])
        res["is_high_speed"] = bool(res["is_high_speed"])
        res["requires_approval"] = res["power_kw"] >= APPROVAL_POWER_KW
        res["blocking"] = state in BLOCKING_STATES
        draft_open = state == DRAFT and now <= parse_ts(res["draft_expires_at"]) and start > now
        too_late_for_approval = res["requires_approval"] and now > start - APPROVAL_LEAD
        res["can_confirm"] = draft_open and not too_late_for_approval
        res["confirm_blocked"] = (
            "Na schválení už nezbývá čas (T_start − 15 min)." if draft_open and too_late_for_approval else ""
        )
        res["user_cancel_until"] = format_dt(start - USER_CANCEL_LEAD)
        res["user_can_cancel"] = state == PENDING_APPROVAL or (state == CONFIRMED and now <= start - USER_CANCEL_LEAD)
        res["admin_can_cancel"] = state == PENDING_APPROVAL or (state == CONFIRMED and now < end)
        res["can_decide"] = state == PENDING_APPROVAL
        if state == CONFIRMED and start <= now < end:
            res["usage"] = "Probíhá nabíjení"
        elif state == CONFIRMED and end <= now:
            res["usage"] = "Proběhla"
        else:
            res["usage"] = ""
        return res

    def _blocking_overlaps(self, station_id: int, start: datetime, end: datetime, exclude_id: int | None = None):
        return self.conn.execute(
            f"""
            SELECT id, user_id, state, start_time, end_time FROM reservations
            WHERE station_id = ? AND state IN {_BLOCKING_SQL}
              AND start_time < ? AND ? < end_time AND id <> ?
            """,
            (station_id, format_dt(end), format_dt(start), exclude_id if exclude_id is not None else -1),
        ).fetchall()

    # ------------------------------------------------------ OP-01 dostupnost

    def _validate_interval(self, start: datetime, slots, now: datetime) -> datetime:
        """BR-01 a předpoklady OP-01/OP-02: kladný interval zarovnaný na 30min bloky, začátek v budoucnu."""
        if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= MAX_SLOTS:
            raise ReservationError(f"Počet slotů musí být 1 až {MAX_SLOTS} (30 minut až {MAX_SLOTS // 2} hodiny).")
        if start.minute % SLOT_MINUTES or start.second or start.microsecond:
            raise ReservationError("Rezervace musí začínat na začátku 30minutového slotu (celá nebo půl hodina).")
        if start <= now:
            raise ReservationError("Začátek rezervace musí být v budoucnu.")
        if start.date() > now.date() + timedelta(days=BOOKING_HORIZON_DAYS):
            raise ReservationError(f"Rezervovat lze nejvýše {BOOKING_HORIZON_DAYS} dní dopředu.")
        return start + timedelta(minutes=SLOT_MINUTES * slots)

    def search_availability(self, start: datetime, slots, vehicle_type, station_id: int | None = None) -> list[dict]:
        """OP-01: které stanice jsou v intervalu volné a kompatibilní s typem vozu (REQ-01).
        Nedostupná je stanice s překryvem s CONFIRMED nebo PENDING_APPROVAL (BR-02 v0.2)
        a pro PHEV každá HPC stanice (BR-04)."""
        now = self.now()
        vehicle = parse_vehicle_type(vehicle_type)
        end = self._validate_interval(start, slots, now)
        stations = [self.get_station(station_id)] if station_id is not None else self.list_stations()
        self.expire_overdue(now)

        result = []
        for st in stations:
            reason, message = None, "Volno."
            if st["is_high_speed"] and vehicle != VEHICLE_BEV:
                reason, message = UNAVAILABLE_VEHICLE, HIGH_SPEED_MESSAGE
            else:
                blocking = self._blocking_overlaps(st["id"], start, end)
                if any(r["state"] == CONFIRMED for r in blocking):
                    reason, message = UNAVAILABLE_COLLISION, "Obsazeno potvrzenou rezervací."
                elif blocking:
                    reason, message = UNAVAILABLE_PENDING, "Blokováno žádostí, která čeká na schválení administrátorem."
            if reason is None and requires_approval(st):
                message = "Volno – potvrzení bude vyžadovat schválení administrátorem."
            result.append(
                {
                    "station_id": st["id"],
                    "name": st["name"],
                    "power_kw": st["power_kw"],
                    "is_high_speed": bool(st["is_high_speed"]),
                    "requires_approval": requires_approval(st),
                    "available": reason is None,
                    "reason": reason,
                    "message": message,
                }
            )
        return result

    def availability(self, station_id: int, day: date, viewer_id: int | None = None) -> list[dict]:
        """Denní přehled 30min slotů jedné stanice (pohled OP-01 po slotech)."""
        self.get_station(station_id)
        now = self.now()
        self.expire_overdue(now)
        day_start = datetime.combine(day, time.min)
        day_end = day_start + timedelta(days=1)
        rows = self.conn.execute(
            f"""
            SELECT id, user_id, start_time, end_time, state FROM reservations
            WHERE station_id = ? AND state IN {_BLOCKING_SQL}
              AND start_time < ? AND ? < end_time
            """,
            (station_id, format_dt(day_end), format_dt(day_start)),
        ).fetchall()

        slots = []
        for i in range(SLOTS_PER_DAY):
            s = day_start + timedelta(minutes=SLOT_MINUTES * i)
            e = s + timedelta(minutes=SLOT_MINUTES)
            s_str, e_str = format_dt(s), format_dt(e)
            blocking = next((r for r in rows if r["start_time"] < e_str and s_str < r["end_time"]), None)
            if blocking is not None and blocking["state"] == CONFIRMED:
                state = SLOT_IN_USE if s <= now < e else SLOT_RESERVED
            elif blocking is not None:
                state = SLOT_PENDING
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
                    "mine": blocking is not None and viewer_id is not None and blocking["user_id"] == viewer_id,
                }
            )
        return slots

    def stations_overview(self) -> list[dict]:
        """Aktuální stav stanic: IN_USE (běží potvrzená rezervace), RESERVED / PENDING (dnes čeká
        potvrzená rezervace / žádost ke schválení), jinak FREE."""
        now = self.now()
        self.expire_overdue(now)
        midnight = datetime.combine(now.date(), time.min) + timedelta(days=1)
        upcoming = self.conn.execute(
            f"""
            SELECT station_id, state, start_time, end_time FROM reservations
            WHERE state IN {_BLOCKING_SQL} AND end_time > ? AND start_time < ?
            ORDER BY start_time
            """,
            (format_dt(now), format_dt(midnight)),
        ).fetchall()
        result = []
        for st in self.list_stations():
            mine = [r for r in upcoming if r["station_id"] == st["id"]]
            current = next((r for r in mine if parse_dt(r["start_time"]) <= now and r["state"] == CONFIRMED), None)
            future = next((r for r in mine if parse_dt(r["start_time"]) > now), None)
            if current is not None:
                status, info = SLOT_IN_USE, f"do {current['end_time'][11:]}"
            elif future is not None and future["state"] == CONFIRMED:
                status, info = SLOT_RESERVED, f"dnes od {future['start_time'][11:]}"
            elif future is not None:
                status, info = SLOT_PENDING, f"dnes od {future['start_time'][11:]}"
            else:
                status, info = SLOT_FREE, ""
            result.append({**dict(st), "requires_approval": requires_approval(st), "status": status, "status_info": info})
        return result

    # ------------------------------------------------------------ operace

    def create(self, user_id: int, station_id: int, start: datetime, slots, vehicle_type, license_plate) -> int:
        """OP-02: zaznamená záměr jako DRAFT s platností 5 minut (BR-05). Kolize se zde
        neověřují – alokace vzniká až atomicky v OP-03."""
        now = self.now()
        with _atomic(self.conn):
            self.get_user(user_id)
            station = self.get_station(station_id)
            plate = normalize_license_plate(license_plate)
            vehicle = parse_vehicle_type(vehicle_type)
            end = self._validate_interval(start, slots, now)
            check_vehicle_allowed(station, vehicle)
            ts = format_ts(now)
            cur = self.conn.execute(
                "INSERT INTO reservations (user_id, station_id, vehicle_type, license_plate, start_time, end_time,"
                " state, created_at, updated_at, draft_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, station_id, vehicle, plate, format_dt(start), format_dt(end), DRAFT, ts, ts,
                 format_ts(now + DRAFT_TTL)),
            )
            reservation_id = cur.lastrowid
        return reservation_id

    def confirm(self, user_id: int, reservation_id: int) -> str:
        """OP-03: DRAFT -> CONFIRMED (AC) | PENDING_APPROVAL (DC/HPC). Kontrola kolize a zápis běží
        v jedné BEGIN IMMEDIATE transakci (REQ-04), DB trigger BLOCKING_OVERLAP je pojistka."""
        now = self.now()
        self.expire_overdue(now)
        with _atomic(self.conn):
            res = self._get_reservation_row(reservation_id)
            if res["user_id"] != user_id:
                raise PermissionDeniedError("Potvrdit lze pouze vlastní rezervaci.")
            if res["state"] == EXPIRED and res["approval_deadline"] is None:
                raise InvalidStateError("Návrh rezervace vypršel (platnost 5 minut) – vytvořte nový.")
            if res["state"] != DRAFT:
                raise InvalidStateError(f"Potvrdit lze pouze návrh ve stavu DRAFT (rezervace je {res['state']}).")
            start, end = parse_dt(res["start_time"]), parse_dt(res["end_time"])
            if start <= now:
                raise InvalidStateError("Termín návrhu už začal – vytvořte novou rezervaci.")
            station = self.get_station(res["station_id"])
            check_vehicle_allowed(station, res["vehicle_type"])
            needs_approval = requires_approval(station)
            deadline = start - APPROVAL_LEAD
            if needs_approval and now > deadline:
                raise InvalidStateError(
                    f"Stanice {station['name']} vyžaduje schválení administrátorem nejpozději 15 minut před začátkem "
                    f"(do {deadline:%H:%M}); na schválení už nezbývá čas. Zvolte pozdější termín nebo AC stanici."
                )
            if self._blocking_overlaps(res["station_id"], start, end, exclude_id=reservation_id):
                raise ConflictError(f"{OVERLAP_MESSAGE} Zvolte jiný čas nebo stanici.")
            if needs_approval:
                new_state = PENDING_APPROVAL
                self.conn.execute(
                    "UPDATE reservations SET state = ?, approval_deadline = ?, updated_at = ? WHERE id = ?",
                    (PENDING_APPROVAL, format_ts(deadline), format_ts(now), reservation_id),
                )
            else:
                new_state = CONFIRMED
                self._set_state(reservation_id, CONFIRMED, now)
        return new_state

    def cancel(self, actor_id: int, reservation_id: int) -> str:
        """OP-04 (BR-03): uživatel ruší CONFIRMED nejpozději T_start − 30 min -> CANCELLED,
        administrátor kdykoliv před T_end -> CANCELLED_BY_ADMIN; žádost PENDING_APPROVAL
        může stáhnout vlastník i administrátor -> CANCELLED. Uvolněný slot jde do Notification Service."""
        now = self.now()
        self.expire_overdue(now)
        with _atomic(self.conn):
            actor = self.get_user(actor_id)
            res = self._get_reservation_row(reservation_id)
            is_owner = res["user_id"] == actor["id"]
            is_admin = bool(actor["is_admin"])
            if not is_owner and not is_admin:
                raise PermissionDeniedError("Zrušit lze pouze vlastní rezervaci.")
            start, end = parse_dt(res["start_time"]), parse_dt(res["end_time"])
            state = res["state"]
            if state == PENDING_APPROVAL:
                new_state = CANCELLED
            elif state == CONFIRMED and is_admin:
                if now >= end:
                    raise InvalidStateError("Rezervace už skončila, nelze ji zrušit.")
                new_state = CANCELLED_BY_ADMIN
            elif state == CONFIRMED:
                limit = start - USER_CANCEL_LEAD
                if now > limit:
                    raise InvalidStateError(
                        f"Storno limit vypršel – zrušit lze nejpozději 30 minut před začátkem (do {limit:%d.%m. %H:%M})."
                    )
                new_state = CANCELLED
            elif state == DRAFT:
                raise InvalidStateError("Návrh (DRAFT) se neruší – pokud ho nepotvrdíte, do 5 minut sám propadne.")
            else:
                raise InvalidStateError(f"Rezervace je v koncovém stavu {state} – zrušení není povoleno.")
            self._set_state(reservation_id, new_state, now)
            if not is_owner:
                self._emit("reservation_cancelled_by_admin", reservation=self.get_reservation(reservation_id))
            self._emit_slot_released(res, now)
        return new_state

    def approve(self, admin_id: int, reservation_id: int) -> str:
        """OP-05 Approve: PENDING_APPROVAL -> CONFIRMED, jen admin a jen do T_start − 15 min (BR-06).
        Stanice je blokovaná už od PENDING_APPROVAL, schválení tedy alokaci jen potvrdí (BR-02)."""
        self._decide(admin_id, reservation_id, CONFIRMED)
        return CONFIRMED

    def reject(self, admin_id: int, reservation_id: int) -> str:
        """OP-05 Reject: PENDING_APPROVAL -> REJECTED, slot se okamžitě uvolní."""
        self._decide(admin_id, reservation_id, REJECTED)
        return REJECTED

    def _decide(self, admin_id: int, reservation_id: int, new_state: str):
        now = self.now()
        self.expire_overdue(now)
        with _atomic(self.conn):
            if not self.get_user(admin_id)["is_admin"]:
                raise PermissionDeniedError("Schvalovat a zamítat žádosti může pouze administrátor.")
            res = self._get_reservation_row(reservation_id)
            if res["state"] == EXPIRED and res["approval_deadline"] is not None:
                deadline = parse_ts(res["approval_deadline"])
                raise InvalidStateError(
                    f"Žádost propadla – rozhodnout bylo nutné nejpozději 15 minut před začátkem (do {deadline:%d.%m. %H:%M})."
                )
            if res["state"] != PENDING_APPROVAL:
                raise InvalidStateError(
                    f"Rozhodnout lze pouze o žádosti ve stavu PENDING_APPROVAL (rezervace je {res['state']})."
                )
            self._set_state(reservation_id, new_state, now)
            decided = self.get_reservation(reservation_id)
            if new_state == CONFIRMED:
                self._emit("reservation_approved", reservation=decided)
            else:
                self._emit("reservation_rejected", reservation=decided)
                self._emit_slot_released(res, now)

    def _set_state(self, reservation_id: int, state: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE reservations SET state = ?, updated_at = ? WHERE id = ?",
            (state, format_ts(now), reservation_id),
        )

    def _emit_slot_released(self, res, now: datetime) -> None:
        """REQ-06: zbývající část intervalu přestala být blokována -> událost o uvolnění slotu."""
        released_start = max(parse_dt(res["start_time"]), ceil_to_slot(now))
        released_end = parse_dt(res["end_time"])
        if released_start < released_end:
            self._emit(
                "slot_released",
                station_id=res["station_id"],
                start=format_dt(released_start),
                end=format_dt(released_end),
                exclude_user_id=res["user_id"],
            )

    def _emit(self, event: str, **payload) -> None:
        """ADR-01: událost se zapíše do outboxu ve stejné transakci jako změna stavu, takže se
        neztratí ani nevznikne pro operaci, která se vrátila (ROLLBACK). Doručí ji OutboxDispatcher."""
        self.conn.execute(
            "INSERT INTO outbox (event, payload, created_at) VALUES (?, ?, ?)",
            (event, json.dumps(payload, ensure_ascii=False), format_ts(self.now())),
        )
