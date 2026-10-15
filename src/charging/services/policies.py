"""C05 Detailed Design – jednotka „Confirmation decision“ (OP-03) uvnitř Reservation Management.

AvailabilityPolicy   smí vůz v daném intervalu na stanici? (BR-04 kompatibilita, BR-02 konflikt)
ConfirmationPolicy   co se má stát s návrhem při potvrzení (vlastník, BR-05 platnost, stav, BR-06 schválení)

Obě třídy jsou čisté: nečtou databázi ani hodiny, všechno dostanou v parametrech a nic nemění.
Transakci, načtení dat a uložení výsledku dělá ReservationService.confirm().
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..timeutil import parse_dt, parse_ts
from .domain import (
    APPROVAL_LEAD,
    CONFIRMED,
    DRAFT,
    EXPIRED,
    PENDING_APPROVAL,
    requires_approval,
    vehicle_allowed,
)

# Výsledky AvailabilityPolicy.evaluate()
AVAILABLE = "AVAILABLE"
VEHICLE_NOT_ALLOWED = "VEHICLE_NOT_ALLOWED"
CONFLICT_CONFIRMED = "CONFLICT_CONFIRMED"
CONFLICT_PENDING = "CONFLICT_PENDING"

# Výsledky ConfirmationPolicy.decide()
CONFIRM = "CONFIRM"
REQUEST_APPROVAL = "REQUEST_APPROVAL"
REJECT = "REJECT"

# Důvody odmítnutí (ConfirmationDecision.reason)
NOT_OWNER = "NOT_OWNER"
DRAFT_EXPIRED = "DRAFT_EXPIRED"
NOT_DRAFT = "NOT_DRAFT"
ALREADY_STARTED = "ALREADY_STARTED"
TOO_LATE_FOR_APPROVAL = "TOO_LATE_FOR_APPROVAL"


class AvailabilityPolicy:
    def evaluate(self, station, vehicle_type: str, start: datetime, end: datetime, blocking) -> str:
        """Precondition: start < end; `blocking` jsou rezervace téže stanice ve stavu CONFIRMED
        nebo PENDING_APPROVAL (načte je volající).
        Postcondition: vrátí právě jeden výsledek, v pořadí priority
        VEHICLE_NOT_ALLOWED > CONFLICT_CONFIRMED > CONFLICT_PENDING > AVAILABLE.
        Překryv je polouzavřený [start, end) – sousední intervaly nekolidují (BR-01)."""
        if not vehicle_allowed(station, vehicle_type):
            return VEHICLE_NOT_ALLOWED
        overlapping = [r for r in blocking if parse_dt(r["start_time"]) < end and start < parse_dt(r["end_time"])]
        if any(r["state"] == CONFIRMED for r in overlapping):
            return CONFLICT_CONFIRMED
        if overlapping:
            return CONFLICT_PENDING
        return AVAILABLE


@dataclass(frozen=True)
class ConfirmationDecision:
    outcome: str  # CONFIRM | REQUEST_APPROVAL | REJECT
    target_state: str | None = None  # CONFIRMED | PENDING_APPROVAL
    approval_deadline: datetime | None = None  # u REQUEST_APPROVAL (a TOO_LATE_FOR_APPROVAL kvůli hlášce)
    reason: str | None = None  # u REJECT


class ConfirmationPolicy:
    def decide(self, reservation, station, requester_id: int, now: datetime) -> ConfirmationDecision:
        """Precondition: `reservation` je aktuální řádek rezervace a `station` její stanice.
        Postcondition: CONFIRM → target CONFIRMED; REQUEST_APPROVAL → target PENDING_APPROVAL
        a approval_deadline = T_start − 15 min; REJECT → reason. Side effects: žádné.
        Konflikt s jinými rezervacemi neřeší – to je AvailabilityPolicy."""
        if reservation["user_id"] != requester_id:
            return ConfirmationDecision(REJECT, reason=NOT_OWNER)
        state = reservation["state"]
        if state == EXPIRED and reservation["approval_deadline"] is None:
            return ConfirmationDecision(REJECT, reason=DRAFT_EXPIRED)
        if state != DRAFT:
            return ConfirmationDecision(REJECT, reason=NOT_DRAFT)
        if now > parse_ts(reservation["draft_expires_at"]):
            return ConfirmationDecision(REJECT, reason=DRAFT_EXPIRED)
        start = parse_dt(reservation["start_time"])
        if start <= now:
            return ConfirmationDecision(REJECT, reason=ALREADY_STARTED)
        if requires_approval(station):
            deadline = start - APPROVAL_LEAD
            if now > deadline:
                return ConfirmationDecision(REJECT, approval_deadline=deadline, reason=TOO_LATE_FOR_APPROVAL)
            return ConfirmationDecision(REQUEST_APPROVAL, PENDING_APPROVAL, deadline)
        return ConfirmationDecision(CONFIRM, CONFIRMED)
