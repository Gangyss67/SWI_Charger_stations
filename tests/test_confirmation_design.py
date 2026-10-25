"""C05 – testy odvozené z Detailed Design jednotky „Confirmation decision“ (OP-03).

Unit testy pracují jen s policies (bez databáze), komponentové testy ověřují spolupráci
ReservationService ↔ policies ↔ SQLite. Tabulka DD element → test je v docs/evidence-and-evolution.md (C05).
"""
from datetime import datetime, timedelta

import pytest

from charging.services.policies import (
    ALREADY_STARTED,
    AVAILABLE,
    CONFIRM,
    CONFLICT_CONFIRMED,
    CONFLICT_PENDING,
    DRAFT_EXPIRED,
    NOT_DRAFT,
    NOT_OWNER,
    REJECT,
    REQUEST_APPROVAL,
    TOO_LATE_FOR_APPROVAL,
    VEHICLE_NOT_ALLOWED,
    AvailabilityPolicy,
    ConfirmationDecision,
    ConfirmationPolicy,
)
from charging.services.reservations import ConflictError, PermissionDeniedError, ReservationService
from conftest import make_user

NOW = datetime(2026, 9, 15, 8, 0)
AC = {"id": 1, "name": "AC 01", "power_kw": 11, "is_high_speed": 0}
DC = {"id": 7, "name": "DC 07", "power_kw": 50, "is_high_speed": 0}
HPC = {"id": 8, "name": "HPC 08", "power_kw": 150, "is_high_speed": 1}


def draft(user_id=1, start="2026-09-15 10:00", state="DRAFT", expires="2026-09-15 08:05:00", deadline=None):
    return {"user_id": user_id, "state": state, "start_time": start, "end_time": "2026-09-15 11:00",
            "draft_expires_at": expires, "approval_deadline": deadline}


def blocking(state, start, end):
    return {"state": state, "start_time": f"2026-09-15 {start}", "end_time": f"2026-09-15 {end}"}


# ------------------------------------------------------------------ ConfirmationPolicy (unit)


def test_contract_ac_draft_is_confirmed_without_deadline():
    decision = ConfirmationPolicy().decide(draft(), AC, requester_id=1, now=NOW)
    assert decision == ConfirmationDecision(CONFIRM, "CONFIRMED")


@pytest.mark.parametrize("station", [DC, HPC])
def test_branch_dc_hpc_requests_approval_with_deadline_start_minus_15(station):
    decision = ConfirmationPolicy().decide(draft(), station, requester_id=1, now=NOW)
    assert decision.outcome == REQUEST_APPROVAL and decision.target_state == "PENDING_APPROVAL"
    assert decision.approval_deadline == datetime(2026, 9, 15, 9, 45)


@pytest.mark.parametrize(
    "now, outcome",
    [(datetime(2026, 9, 15, 9, 45), REQUEST_APPROVAL), (datetime(2026, 9, 15, 9, 45, 1), REJECT)],
)
def test_branch_approval_deadline_boundary(now, outcome):
    row = draft(expires="2026-09-15 09:50:00")
    decision = ConfirmationPolicy().decide(row, HPC, requester_id=1, now=now)
    assert decision.outcome == outcome
    if outcome == REJECT:
        assert decision.reason == TOO_LATE_FOR_APPROVAL and decision.approval_deadline == datetime(2026, 9, 15, 9, 45)


@pytest.mark.parametrize(
    "row, requester, now, reason",
    [
        (draft(user_id=2), 1, NOW, NOT_OWNER),
        (draft(state="EXPIRED"), 1, NOW, DRAFT_EXPIRED),
        (draft(), 1, NOW + timedelta(minutes=5, seconds=1), DRAFT_EXPIRED),  # DRAFT, ale po 5 min (BR-05)
        (draft(state="CONFIRMED"), 1, NOW, NOT_DRAFT),
        (draft(state="PENDING_APPROVAL", deadline="2026-09-15 09:45:00"), 1, NOW, NOT_DRAFT),
        (draft(start="2026-09-15 08:00", expires="2026-09-15 08:05:00"), 1, datetime(2026, 9, 15, 8, 2), ALREADY_STARTED),
        (draft(start="2026-09-15 08:00", expires="2026-09-15 08:05:00"), 1, NOW, ALREADY_STARTED),  # hranice: start == teď (C06, D4)
        (draft(user_id=2, state="CONFIRMED"), 1, NOW, NOT_OWNER),  # pořadí: vlastník se kontroluje první
    ],
)
def test_failure_behavior_returns_defined_reason(row, requester, now, reason):
    decision = ConfirmationPolicy().decide(row, AC, requester_id=requester, now=now)
    assert decision.outcome == REJECT and decision.reason == reason and decision.target_state is None


# ------------------------------------------------------------------ AvailabilityPolicy (unit, rozhodovací tabulka)


@pytest.mark.parametrize(
    "station, vehicle, existing, expected",
    [
        (AC, "BEV", [], AVAILABLE),
        (AC, "BEV", [blocking("CONFIRMED", "10:30", "11:30")], CONFLICT_CONFIRMED),
        (AC, "BEV", [blocking("PENDING_APPROVAL", "09:30", "10:30")], CONFLICT_PENDING),
        (AC, "BEV", [blocking("PENDING_APPROVAL", "10:00", "10:30"), blocking("CONFIRMED", "10:30", "11:00")], CONFLICT_CONFIRMED),
        (AC, "BEV", [blocking("CONFIRMED", "11:00", "12:00"), blocking("CONFIRMED", "09:00", "10:00")], AVAILABLE),  # [start, end)
        (HPC, "PHEV", [blocking("CONFIRMED", "10:00", "11:00")], VEHICLE_NOT_ALLOWED),  # priorita BR-04
        (DC, "PHEV", [], AVAILABLE),
    ],
)
def test_availability_decision_table(station, vehicle, existing, expected):
    status = AvailabilityPolicy().evaluate(station, vehicle, datetime(2026, 9, 15, 10), datetime(2026, 9, 15, 11), existing)
    assert status == expected


# ------------------------------------------------------------------ ReservationService ↔ policies (komponentové)


@pytest.fixture
def ev(conn):
    return make_user(conn, "ev", "BEV")


def test_state_transition_service_persists_policy_decision(svc, conn, ev):
    rid = svc.create(ev, 8, datetime(2026, 9, 15, 10), 2, "BEV", "1AB 2345")
    assert svc.confirm(ev, rid) == "PENDING_APPROVAL"
    row = conn.execute("SELECT state, approval_deadline FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert (row["state"], row["approval_deadline"]) == ("PENDING_APPROVAL", "2026-09-15 09:45:00")


def test_collaboration_service_uses_injected_policies(conn, clock, ev):
    class AlwaysNotOwner(ConfirmationPolicy):
        def decide(self, reservation, station, requester_id, now):
            return ConfirmationDecision(REJECT, reason=NOT_OWNER)

    class AlwaysPendingConflict(AvailabilityPolicy):
        def evaluate(self, station, vehicle_type, start, end, blocking):
            return CONFLICT_PENDING

    rid = ReservationService(conn, clock).create(ev, 1, datetime(2026, 9, 15, 10), 1, "BEV", "1AB 2345")
    with pytest.raises(PermissionDeniedError):
        ReservationService(conn, clock, confirmation_policy=AlwaysNotOwner()).confirm(ev, rid)
    with pytest.raises(ConflictError):
        ReservationService(conn, clock, availability_policy=AlwaysPendingConflict()).confirm(ev, rid)
    assert conn.execute("SELECT state FROM reservations WHERE id = ?", (rid,)).fetchone()[0] == "DRAFT"


def test_collaboration_op01_and_op03_decide_the_same_way(svc, conn, ev):
    """OP-01 i OP-03 používají stejnou AvailabilityPolicy – co hledání hlásí jako obsazené, potvrzení odmítne."""
    other = make_user(conn, "ev2", "BEV")
    first = svc.create(ev, 8, datetime(2026, 9, 15, 10), 2, "BEV", "1AB 2345")
    svc.confirm(ev, first)  # HPC 08 [10:00, 11:00) -> PENDING_APPROVAL
    for start, slots in ((datetime(2026, 9, 15, 10, 30), 1), (datetime(2026, 9, 15, 11), 1), (datetime(2026, 9, 15, 9), 2)):
        [found] = svc.search_availability(start, slots, "BEV", station_id=8)
        rid = svc.create(other, 8, start, slots, "BEV", "2AB 2345")
        if found["available"]:
            assert svc.confirm(other, rid) == "PENDING_APPROVAL"
            svc.cancel(other, rid)  # uvolnit pro další případ
        else:
            assert found["reason"] == "PENDING_APPROVAL"
            with pytest.raises(ConflictError):
                svc.confirm(other, rid)
