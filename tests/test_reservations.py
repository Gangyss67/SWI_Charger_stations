"""Testy doménové logiky: stavy, operace, common rule, domain rule, notifikace."""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from charging.services import notifications
from charging.services.reservations import (
    CANCELLED,
    CONFIRMED,
    DRAFT,
    PENDING,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ReservationError,
    ReservationService,
)
from conftest import make_user

TOMORROW_10 = datetime(2026, 9, 15, 10, 0)
AC_11KW, AC_11KW_B, DC_50KW, HPC = 1, 2, 7, 8


@pytest.fixture
def users(conn):
    return {
        "ev": make_user(conn, "ev", "BEV"),
        "ev2": make_user(conn, "ev2", "BEV"),
        "ev3": make_user(conn, "ev3", "BEV"),
        "phev": make_user(conn, "phev", "PHEV"),
        "admin": make_user(conn, "admin", "BEV", is_admin=True),
    }


def state(conn, reservation_id):
    return conn.execute("SELECT state FROM reservations WHERE id = ?", (reservation_id,)).fetchone()["state"]


def confirmed(svc, users, user, station, start, slots):
    rid = svc.create(users[user], station, start, slots)
    svc.submit(users[user], rid)
    svc.approve(users["admin"], rid)
    return rid


def messages_for(conn, user_id):
    return [row["message"] for row in notifications.list_for_user(conn, user_id)]


# ------------------------------------------------------------------ seed data


def test_ten_stations_three_high_speed(conn):
    rows = conn.execute("SELECT * FROM stations").fetchall()
    assert len(rows) == 10
    assert sum(r["is_high_speed"] for r in rows) == 3


# ------------------------------------------------------------------ create


def test_create_reservation_is_draft_with_30_minute_slots(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 3)
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert row["state"] == DRAFT
    assert row["start_time"] == "2026-09-15 10:00"
    assert row["end_time"] == "2026-09-15 11:30"


@pytest.mark.parametrize("start", [datetime(2026, 9, 15, 10, 15), datetime(2026, 9, 15, 10, 0, 30)])
def test_create_rejects_start_not_aligned_to_slot(svc, users, start):
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC_11KW, start, 1)


def test_create_rejects_past_and_already_started_slot(svc, users):
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC_11KW, datetime(2026, 9, 14, 8, 0), 1)  # teď je 08:10
    assert svc.create(users["ev"], AC_11KW, datetime(2026, 9, 14, 8, 30), 1)


@pytest.mark.parametrize("slots", [0, 9, -1, "2", None, True, 1.5])
def test_create_rejects_invalid_slot_count(svc, users, slots):
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC_11KW, TOMORROW_10, slots)


def test_create_rejects_beyond_booking_horizon(svc, users):
    assert svc.create(users["ev"], AC_11KW, datetime(2026, 9, 28, 23, 30), 1)
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC_11KW, datetime(2026, 9, 29, 0, 0), 1)


def test_create_unknown_station_or_user(svc, users):
    with pytest.raises(NotFoundError):
        svc.create(users["ev"], 999, TOMORROW_10, 1)
    with pytest.raises(NotFoundError):
        svc.create(999, AC_11KW, TOMORROW_10, 1)


def test_reservation_may_cross_midnight(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, datetime(2026, 9, 15, 23, 30), 2)
    row = conn.execute("SELECT end_time FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert row["end_time"] == "2026-09-16 00:30"


# ------------------------------------------------------------------ domain rule


def test_phev_cannot_reserve_high_speed_station(svc, users):
    with pytest.raises(ReservationError, match="BEV"):
        svc.create(users["phev"], HPC, TOMORROW_10, 1)


def test_phev_can_reserve_normal_station_and_bev_can_reserve_high_speed(svc, users):
    assert svc.create(users["phev"], DC_50KW, TOMORROW_10, 1)
    assert svc.create(users["ev"], HPC, TOMORROW_10, 1)


def test_db_trigger_blocks_phev_on_high_speed_even_without_service(conn, users):
    with pytest.raises(sqlite3.IntegrityError, match="HIGH_SPEED_BEV_ONLY"):
        conn.execute(
            "INSERT INTO reservations (user_id, station_id, start_time, end_time, state, created_at, updated_at)"
            " VALUES (?, ?, '2026-09-15 10:00', '2026-09-15 10:30', 'DRAFT', 'x', 'x')",
            (users["phev"], HPC),
        )


# ------------------------------------------------------------------ state flow


def test_full_flow_draft_pending_confirmed(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 2)
    assert state(conn, rid) == DRAFT
    svc.submit(users["ev"], rid)
    assert state(conn, rid) == PENDING
    svc.approve(users["admin"], rid)
    assert state(conn, rid) == CONFIRMED


def test_only_owner_can_submit(svc, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    with pytest.raises(PermissionDeniedError):
        svc.submit(users["ev2"], rid)


def test_submit_only_from_draft(svc, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    svc.submit(users["ev"], rid)
    with pytest.raises(ReservationError):
        svc.submit(users["ev"], rid)


def test_cannot_submit_draft_whose_start_has_passed(svc, users, clock):
    rid = svc.create(users["ev"], AC_11KW, datetime(2026, 9, 14, 9, 0), 1)
    clock.value = datetime(2026, 9, 14, 9, 5)
    with pytest.raises(ReservationError):
        svc.submit(users["ev"], rid)


def test_only_admin_can_approve(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    svc.submit(users["ev"], rid)
    with pytest.raises(PermissionDeniedError):
        svc.approve(users["ev"], rid)
    assert state(conn, rid) == PENDING


def test_approve_requires_pending(svc, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    with pytest.raises(ReservationError):
        svc.approve(users["admin"], rid)


def test_cannot_approve_finished_reservation(svc, users, clock):
    rid = svc.create(users["ev"], AC_11KW, datetime(2026, 9, 14, 9, 0), 1)
    svc.submit(users["ev"], rid)
    clock.value = datetime(2026, 9, 14, 9, 30)
    with pytest.raises(ReservationError):
        svc.approve(users["admin"], rid)


def test_db_trigger_blocks_invalid_state_transition(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    svc.cancel(users["ev"], rid)
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_STATE_TRANSITION"):
        conn.execute("UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_STATE_TRANSITION"):
        rid2 = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
        conn.execute("UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (rid2,))  # DRAFT -> CONFIRMED


# ------------------------------------------------------------------ cancel


@pytest.mark.parametrize("stage", [DRAFT, PENDING, CONFIRMED])
def test_owner_can_cancel_from_any_active_state(svc, conn, users, stage):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    if stage in (PENDING, CONFIRMED):
        svc.submit(users["ev"], rid)
    if stage == CONFIRMED:
        svc.approve(users["admin"], rid)
    svc.cancel(users["ev"], rid)
    assert state(conn, rid) == CANCELLED


def test_other_user_cannot_cancel_but_admin_can(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    with pytest.raises(PermissionDeniedError):
        svc.cancel(users["ev2"], rid)
    svc.cancel(users["admin"], rid)
    assert state(conn, rid) == CANCELLED
    assert any("zrušena administrátorem" in m for m in messages_for(conn, users["ev"]))


def test_cannot_cancel_twice(svc, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    svc.cancel(users["ev"], rid)
    with pytest.raises(ReservationError):
        svc.cancel(users["ev"], rid)


def test_cannot_cancel_finished_confirmed_reservation(svc, users, clock):
    rid = confirmed(svc, users, "ev", AC_11KW, datetime(2026, 9, 14, 9, 0), 1)
    clock.value = datetime(2026, 9, 14, 10, 0)
    with pytest.raises(ReservationError):
        svc.cancel(users["ev"], rid)


# ------------------------------------------------------------------ common rule: no overlap


def test_cannot_create_over_confirmed_reservation(svc, users):
    confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)  # 10:00–11:00
    with pytest.raises(ConflictError):
        svc.create(users["ev2"], AC_11KW, datetime(2026, 9, 15, 10, 30), 2)
    with pytest.raises(ConflictError):
        svc.create(users["ev2"], AC_11KW, datetime(2026, 9, 15, 9, 30), 4)  # obaluje celou rezervaci


def test_two_pending_may_overlap_but_only_one_can_be_confirmed(svc, conn, users):
    a = svc.create(users["ev"], AC_11KW, TOMORROW_10, 2)
    b = svc.create(users["ev2"], AC_11KW, datetime(2026, 9, 15, 10, 30), 2)
    svc.submit(users["ev"], a)
    svc.submit(users["ev2"], b)
    svc.approve(users["admin"], a)
    with pytest.raises(ConflictError):
        svc.approve(users["admin"], b)
    assert state(conn, a) == CONFIRMED
    assert state(conn, b) == PENDING
    assert svc.get_reservation(b)["has_conflict"] is True
    assert svc.get_reservation(b)["can_approve"] is False


def test_adjacent_reservations_do_not_overlap(svc, conn, users):
    a = confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)  # 10:00–11:00
    b = confirmed(svc, users, "ev2", AC_11KW, datetime(2026, 9, 15, 11, 0), 2)  # 11:00–12:00
    assert state(conn, a) == state(conn, b) == CONFIRMED


def test_same_time_on_different_station_is_allowed(svc, conn, users):
    confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)
    b = confirmed(svc, users, "ev2", AC_11KW_B, TOMORROW_10, 2)
    assert state(conn, b) == CONFIRMED


def test_cancelled_reservation_frees_the_slot(svc, conn, users):
    a = confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)
    svc.cancel(users["ev"], a)
    b = confirmed(svc, users, "ev2", AC_11KW, TOMORROW_10, 2)
    assert state(conn, b) == CONFIRMED


def test_db_trigger_blocks_overlapping_confirmed_even_without_service(svc, conn, users):
    confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)
    with pytest.raises(sqlite3.IntegrityError, match="CONFIRMED_OVERLAP"):
        conn.execute(
            "INSERT INTO reservations (user_id, station_id, start_time, end_time, state, created_at, updated_at)"
            " VALUES (?, ?, '2026-09-15 10:30', '2026-09-15 11:30', 'CONFIRMED', 'x', 'x')",
            (users["ev2"], AC_11KW),
        )


def test_overlap_rule_holds_across_separate_connections(svc, conn, users, clock, tmp_path):
    """Dvě nezávislá připojení (jako dva souběžné requesty) schvalují kolidující rezervace."""
    from charging.db import connect

    a = svc.create(users["ev"], AC_11KW, TOMORROW_10, 2)
    b = svc.create(users["ev2"], AC_11KW, TOMORROW_10, 2)
    svc.submit(users["ev"], a)
    svc.submit(users["ev2"], b)
    other = connect(str(tmp_path / "test.db"))
    try:
        svc_other = ReservationService(other, None, clock)
        svc.approve(users["admin"], a)
        with pytest.raises(ConflictError):
            svc_other.approve(users["admin"], b)
    finally:
        other.close()
    count = conn.execute("SELECT COUNT(*) FROM reservations WHERE state = 'CONFIRMED'").fetchone()[0]
    assert count == 1


# ------------------------------------------------------------------ check availability


def test_check_availability(svc, users):
    ok, _ = svc.check_availability(AC_11KW, TOMORROW_10, 2, users["ev"])
    assert ok
    confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)
    ok, message = svc.check_availability(AC_11KW, datetime(2026, 9, 15, 10, 30), 1, users["ev2"])
    assert not ok and "obsazený" in message
    ok, message = svc.check_availability(HPC, TOMORROW_10, 1, users["phev"])
    assert not ok and "BEV" in message


def test_availability_grid_states(svc, users, clock):
    confirmed(svc, users, "ev", AC_11KW, TOMORROW_10, 2)  # sloty 20 a 21
    pending = svc.create(users["ev2"], AC_11KW, datetime(2026, 9, 15, 11, 0), 1)
    svc.submit(users["ev2"], pending)

    slots = svc.availability(AC_11KW, date(2026, 9, 15), viewer_id=users["ev"])
    assert len(slots) == 48
    assert [s["state"] for s in slots[19:23]] == ["FREE", "RESERVED", "RESERVED", "FREE"]
    assert slots[20]["mine"] and not slots[19]["mine"]
    assert slots[22]["pending_count"] == 1

    clock.value = datetime(2026, 9, 15, 10, 15)
    slots = svc.availability(AC_11KW, date(2026, 9, 15))
    assert [s["state"] for s in slots[19:23]] == ["PAST", "IN_USE", "RESERVED", "FREE"]


def test_stations_overview_statuses(svc, users, clock):
    confirmed(svc, users, "ev", AC_11KW, datetime(2026, 9, 14, 9, 0), 2)
    confirmed(svc, users, "ev2", AC_11KW_B, datetime(2026, 9, 14, 15, 0), 1)
    clock.value = datetime(2026, 9, 14, 9, 30)
    overview = {s["id"]: s for s in svc.stations_overview()}
    assert overview[AC_11KW]["status"] == "IN_USE"
    assert overview[AC_11KW_B]["status"] == "RESERVED"
    assert overview[3]["status"] == "FREE"


# ------------------------------------------------------------------ notification boundary


def test_approve_and_submit_send_notifications(svc, conn, users):
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    svc.submit(users["ev"], rid)
    assert any("čeká na schválení" in m.lower() for m in messages_for(conn, users["admin"]))
    svc.approve(users["admin"], rid)
    assert any("potvrzena" in m for m in messages_for(conn, users["ev"]))


def test_released_high_speed_slot_notifies_pending_and_eligible_lower_power_users(svc, conn, users):
    # ev: potvrzená HPC 10:00–11:00; ev2: čekající HPC 10:30–11:30 (koliduje)
    a = svc.create(users["ev"], HPC, TOMORROW_10, 2)
    b = svc.create(users["ev2"], HPC, datetime(2026, 9, 15, 10, 30), 2)
    svc.submit(users["ev"], a)
    svc.submit(users["ev2"], b)
    svc.approve(users["admin"], a)
    # ev3 (BEV) na AC 11 kW, phev na AC 11 kW ve stejném čase
    confirmed(svc, users, "ev3", AC_11KW, TOMORROW_10, 1)
    confirmed(svc, users, "phev", AC_11KW_B, TOMORROW_10, 2)
    before = {u: len(messages_for(conn, uid)) for u, uid in users.items()}

    svc.cancel(users["ev"], a)

    new = {u: messages_for(conn, uid)[: len(messages_for(conn, uid)) - before[u]] for u, uid in users.items()}
    assert len(new["ev2"]) == 1 and "může být potvrzena" in new["ev2"][0]
    assert len(new["ev3"]) == 1 and "výkonnější nabíječka HPC 08" in new["ev3"][0]
    assert new["phev"] == []  # PHEV nesmí na HPC -> neupozorňujeme
    assert new["ev"] == []
    svc.approve(users["admin"], b)
    assert state(conn, b) == CONFIRMED


def test_released_normal_station_notifies_phev_on_weaker_station(svc, conn, users):
    a = confirmed(svc, users, "ev", DC_50KW, TOMORROW_10, 2)
    confirmed(svc, users, "phev", AC_11KW, TOMORROW_10, 1)
    svc.cancel(users["ev"], a)
    assert any("DC 07 (50 kW)" in m for m in messages_for(conn, users["phev"]))


def test_no_release_notification_when_cancelling_pending(svc, conn, users):
    confirmed(svc, users, "ev3", AC_11KW, TOMORROW_10, 1)
    b = svc.create(users["ev"], HPC, TOMORROW_10, 1)
    svc.submit(users["ev"], b)
    svc.cancel(users["ev"], b)
    assert not any("výkonnější" in m for m in messages_for(conn, users["ev3"]))


def test_notification_failure_does_not_break_reservation(conn, users, clock):
    class BrokenNotifier:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise TimeoutError("notification provider down")

            return fail

    svc = ReservationService(conn, BrokenNotifier(), clock)
    rid = svc.create(users["ev"], AC_11KW, TOMORROW_10, 1)
    svc.submit(users["ev"], rid)
    svc.approve(users["admin"], rid)
    svc.cancel(users["ev"], rid)
    assert state(conn, rid) == CANCELLED
