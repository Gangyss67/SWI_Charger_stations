"""Testy doménové logiky podle specifikace baseline v0.2 (docs/intent-and-change.md).

Každá operace OP-01..OP-05 má testy pojmenované podle příkladů ověření ze specifikace;
"teď" je 15.09.2026 08:00 (viz conftest.clock).
"""
import json
import sqlite3
import threading
from datetime import date, datetime

import pytest

from charging.db import connect
from charging.services import notifications
from charging.services.reservations import (
    CANCELLED,
    CANCELLED_BY_ADMIN,
    CONFIRMED,
    DRAFT,
    EXPIRED,
    PENDING_APPROVAL,
    REJECTED,
    UNAVAILABLE_COLLISION,
    UNAVAILABLE_PENDING,
    UNAVAILABLE_VEHICLE,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
    ReservationError,
    ReservationService,
)
from conftest import deliver, make_user

AC01, AC02, DC07, HPC08, HPC09 = 1, 2, 7, 8, 9
PLATE = "1AB 2345"


def at(hour, minute=0, second=0):
    return datetime(2026, 9, 15, hour, minute, second)


@pytest.fixture
def users(conn):
    return {
        "ev": make_user(conn, "ev", "BEV"),
        "ev2": make_user(conn, "ev2", "BEV"),
        "ev3": make_user(conn, "ev3", "BEV"),
        "phev": make_user(conn, "phev", "PHEV"),
        "admin": make_user(conn, "admin", "BEV", is_admin=True),
    }


def vehicle_of(who):
    return "PHEV" if who == "phev" else "BEV"


def draft(svc, users, who, station, start, slots):
    return svc.create(users[who], station, start, slots, vehicle_of(who), PLATE)


def confirmed(svc, users, who, station, start, slots):
    """AC stanice: create + confirm -> CONFIRMED; DC/HPC: create + confirm + approve."""
    rid = draft(svc, users, who, station, start, slots)
    if svc.confirm(users[who], rid) == PENDING_APPROVAL:
        svc.approve(users["admin"], rid)
    return rid


def pending(svc, users, who, station, start, slots):
    rid = draft(svc, users, who, station, start, slots)
    assert svc.confirm(users[who], rid) == PENDING_APPROVAL
    return rid


def state(conn, reservation_id):
    return conn.execute("SELECT state FROM reservations WHERE id = ?", (reservation_id,)).fetchone()["state"]


def check(svc, station, start, slots, vehicle="BEV"):
    return svc.search_availability(start, slots, vehicle, station_id=station)[0]


def messages_for(conn, user_id):
    deliver(conn)
    return [row["message"] for row in notifications.list_for_user(conn, user_id)]


# ------------------------------------------------------------------ seed data


def test_ten_stations_three_hpc_and_four_require_approval(svc):
    stations = svc.stations_overview()
    assert len(stations) == 10
    assert [s["name"] for s in stations if s["is_high_speed"]] == ["HPC 08", "HPC 09", "HPC 10"]
    assert [s["name"] for s in stations if s["requires_approval"]] == ["DC 07", "HPC 08", "HPC 09", "HPC 10"]


# ------------------------------------------------------------------ OP-01 Check Availability


def test_op01_examples_with_confirmed_on_hpc08(svc, users):
    confirmed(svc, users, "ev", HPC08, at(10), 2)  # existující CONFIRMED [10:00, 11:00)
    assert check(svc, HPC08, at(9), 2)["available"] is True  # [09:00, 10:00)
    r = check(svc, HPC08, at(10, 30), 2)  # [10:30, 11:30)
    assert r["available"] is False and r["reason"] == UNAVAILABLE_COLLISION
    assert check(svc, HPC08, at(11), 2)["available"] is True  # [11:00, 12:00) hrana nekoliduje
    r = check(svc, HPC08, at(11), 2, vehicle="PHEV")
    assert r["available"] is False and r["reason"] == UNAVAILABLE_VEHICLE


def test_op01_v02_pending_approval_blocks_station(svc, users):
    pending(svc, users, "ev", HPC08, at(10), 2)
    r = check(svc, HPC08, at(10, 30), 2)
    assert r["available"] is False and r["reason"] == UNAVAILABLE_PENDING


def test_op01_draft_does_not_block(svc, users):
    draft(svc, users, "ev", AC01, at(10), 2)
    assert check(svc, AC01, at(10), 2)["available"] is True


def test_op01_search_returns_all_stations_with_approval_flag(svc, users):
    result = svc.search_availability(at(10), 2, "PHEV")
    assert len(result) == 10
    by_name = {r["name"]: r for r in result}
    assert all(by_name[n]["reason"] == UNAVAILABLE_VEHICLE for n in ("HPC 08", "HPC 09", "HPC 10"))
    assert by_name["DC 07"]["available"] and by_name["DC 07"]["requires_approval"]
    assert by_name["AC 01"]["available"] and not by_name["AC 01"]["requires_approval"]


@pytest.mark.parametrize("start, slots", [(at(10, 15), 1), (at(7, 30), 1), (at(10), 0), (at(10), 9)])
def test_op01_rejects_invalid_interval(svc, start, slots):
    with pytest.raises(ReservationError):
        svc.search_availability(start, slots, "BEV")


def test_op01_requires_vehicle_type(svc):
    with pytest.raises(ReservationError):
        svc.search_availability(at(10), 1, None)


# ------------------------------------------------------------------ OP-02 Create Reservation


def test_op02_example_ac01_phev_creates_draft_with_5_minute_expiry(svc, conn, users):
    rid = draft(svc, users, "phev", AC01, at(10), 1)
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert (row["state"], row["start_time"], row["end_time"]) == (DRAFT, "2026-09-15 10:00", "2026-09-15 10:30")
    assert (row["vehicle_type"], row["license_plate"]) == ("PHEV", PLATE)
    assert row["draft_expires_at"] == "2026-09-15 08:05:00"
    assert row["approval_deadline"] is None


def test_op02_example_hpc08_bev_creates_draft(svc, conn, users):
    rid = draft(svc, users, "ev", HPC08, at(10), 2)
    assert state(conn, rid) == DRAFT


def test_op02_example_hpc09_phev_is_rejected(svc, users):
    with pytest.raises(ReservationError, match="BEV"):
        draft(svc, users, "phev", HPC09, at(10), 1)


def test_op02_example_ac02_not_aligned_is_rejected(svc, users):
    with pytest.raises(ReservationError):
        draft(svc, users, "ev", AC02, at(10, 15), 1)


@pytest.mark.parametrize("plate", ["", "   ", None, "ÁBC 123", "1234567890123"])
def test_op02_requires_valid_license_plate(svc, users, plate):
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC01, at(10), 1, "BEV", plate)


def test_op02_normalizes_license_plate(svc, conn, users):
    rid = svc.create(users["ev"], AC01, at(10), 1, "BEV", "  1ab   2345 ")
    assert conn.execute("SELECT license_plate FROM reservations WHERE id = ?", (rid,)).fetchone()[0] == "1AB 2345"


def test_op02_rejects_past_start_unknown_station_and_user(svc, users):
    with pytest.raises(ReservationError):
        draft(svc, users, "ev", AC01, at(8), 1)  # teď je 08:00 -> začátek není v budoucnu
    with pytest.raises(NotFoundError):
        draft(svc, users, "ev", 999, at(10), 1)
    with pytest.raises(NotFoundError):
        svc.create(999, AC01, at(10), 1, "BEV", PLATE)


def test_op02_does_not_check_collisions(svc, conn, users):
    confirmed(svc, users, "ev", AC01, at(10), 2)
    rid = draft(svc, users, "ev2", AC01, at(10), 2)  # kolize se ověří až v OP-03
    assert state(conn, rid) == DRAFT


def test_db_blocks_phev_on_hpc_and_non_draft_insert(conn, users):
    insert = (
        "INSERT INTO reservations (user_id, station_id, vehicle_type, license_plate, start_time, end_time, state,"
        " created_at, updated_at, draft_expires_at) VALUES (?, ?, ?, 'X', '2026-09-15 10:00', '2026-09-15 10:30',"
        " ?, 'x', 'x', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="HIGH_SPEED_BEV_ONLY"):
        conn.execute(insert, (users["phev"], HPC08, "PHEV", DRAFT))
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_INITIAL_STATE"):
        conn.execute(insert, (users["ev"], AC01, "BEV", CONFIRMED))


# ------------------------------------------------------------------ OP-03 Confirm Reservation


def test_op03_ac_draft_without_collision_is_confirmed_immediately(svc, conn, users):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    assert svc.confirm(users["ev"], rid) == CONFIRMED
    assert state(conn, rid) == CONFIRMED


@pytest.mark.parametrize("station", [DC07, HPC08])
def test_op03_v02_dc_hpc_draft_goes_to_pending_approval(svc, conn, users, station):
    rid = draft(svc, users, "ev", station, at(10), 2)
    assert svc.confirm(users["ev"], rid) == PENDING_APPROVAL
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert row["state"] == PENDING_APPROVAL and row["approval_deadline"] == "2026-09-15 09:45:00"


def test_op03_collision_with_confirmed_is_rejected_and_stays_draft(svc, conn, users):
    confirmed(svc, users, "ev", AC01, at(10), 2)
    rid = draft(svc, users, "ev2", AC01, at(10, 30), 2)
    with pytest.raises(ConflictError):
        svc.confirm(users["ev2"], rid)
    assert state(conn, rid) == DRAFT


def test_op03_v02_collision_with_pending_approval_is_rejected(svc, conn, users):
    pending(svc, users, "ev", HPC08, at(10), 2)
    rid = draft(svc, users, "ev2", HPC08, at(10, 30), 2)
    with pytest.raises(ConflictError):
        svc.confirm(users["ev2"], rid)
    assert state(conn, rid) == DRAFT


def test_op03_draft_older_than_5_minutes_is_expired(svc, conn, users, clock):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    clock.value = at(8, 5, 1)
    with pytest.raises(InvalidStateError, match="vypršel"):
        svc.confirm(users["ev"], rid)
    assert state(conn, rid) == EXPIRED  # propadnutí se zapíše, i když potvrzení selhalo


def test_op03_draft_exactly_5_minutes_old_can_be_confirmed(svc, users, clock):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    clock.value = at(8, 5)
    assert svc.confirm(users["ev"], rid) == CONFIRMED


def test_op03_v02_dc_confirm_less_than_15_minutes_before_start_is_rejected(svc, conn, users, clock):
    clock.value = at(9, 44)
    rid = draft(svc, users, "ev", DC07, at(10), 1)
    clock.value = at(9, 46)  # T_start − 15 min = 09:45 už minulo
    with pytest.raises(InvalidStateError, match="nezbývá čas"):
        svc.confirm(users["ev"], rid)
    assert state(conn, rid) == DRAFT


def test_op03_v02_dc_confirm_exactly_at_approval_deadline_is_allowed(svc, users, clock):
    clock.value = at(9, 44)
    rid = draft(svc, users, "ev", DC07, at(10), 1)
    clock.value = at(9, 45)
    assert svc.confirm(users["ev"], rid) == PENDING_APPROVAL


def test_op03_only_owner_and_only_from_draft(svc, users):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    with pytest.raises(PermissionDeniedError):
        svc.confirm(users["ev2"], rid)
    svc.confirm(users["ev"], rid)
    with pytest.raises(InvalidStateError):
        svc.confirm(users["ev"], rid)


@pytest.mark.parametrize("station, expected", [(AC01, CONFIRMED), (HPC08, PENDING_APPROVAL)])
def test_op03_req04_concurrent_confirms_allow_exactly_one(conn, db_path, clock, station, expected):
    """REQ-04 + future pressure Q: 10 souběžných potvrzení kolidujících DRAFT (každé ve vlastním
    připojení a vlákně) -> do blokujícího stavu přejde právě jedno."""
    setup = ReservationService(conn, clock)
    users = [make_user(conn, f"race{i}", "BEV") for i in range(10)]
    drafts = [setup.create(uid, station, at(10), 2, "BEV", PLATE) for uid in users]
    barrier = threading.Barrier(len(users))
    outcomes = []

    def worker(user_id, reservation_id):
        c = connect(db_path)
        try:
            svc = ReservationService(c, clock)
            barrier.wait()
            outcomes.append(svc.confirm(user_id, reservation_id))
        except ConflictError:
            outcomes.append("conflict")
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=pair) for pair in zip(users, drafts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == sorted([expected] + ["conflict"] * 9)
    states = [row[0] for row in conn.execute("SELECT state FROM reservations ORDER BY id")]
    assert states.count(expected) == 1 and states.count(DRAFT) == 9


def test_db_triggers_enforce_approval_policy_overlap_and_transitions(svc, conn, users):
    ac = draft(svc, users, "ev", AC01, at(10), 1)
    hpc = draft(svc, users, "ev", HPC08, at(10), 1)
    with pytest.raises(sqlite3.IntegrityError, match="APPROVAL_REQUIRED"):
        conn.execute("UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (hpc,))
    with pytest.raises(sqlite3.IntegrityError, match="APPROVAL_REQUIRED"):
        conn.execute("UPDATE reservations SET state = 'PENDING_APPROVAL', approval_deadline = 'x' WHERE id = ?", (ac,))

    svc.confirm(users["ev"], ac)
    other = draft(svc, users, "ev2", AC01, at(10), 1)
    with pytest.raises(sqlite3.IntegrityError, match="BLOCKING_OVERLAP"):
        conn.execute("UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (other,))

    svc.cancel(users["ev"], ac)
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_STATE_TRANSITION"):
        conn.execute("UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (ac,))


# ------------------------------------------------------------------ OP-04 Cancel Reservation


@pytest.fixture
def at_14(svc, users):
    """CONFIRMED rezervace AC 01 s T_start ve 14:00 (příklady ověření OP-04)."""
    return confirmed(svc, users, "ev", AC01, at(14), 2)


def test_op04_user_cancels_at_13_15_is_cancelled_and_slot_free(svc, conn, users, clock, at_14):
    clock.value = at(13, 15)
    assert svc.cancel(users["ev"], at_14) == CANCELLED
    assert state(conn, at_14) == CANCELLED
    assert check(svc, AC01, at(14), 2)["available"] is True


def test_op04_user_cancel_exactly_30_minutes_before_is_allowed(svc, users, clock, at_14):
    clock.value = at(13, 30)
    assert svc.cancel(users["ev"], at_14) == CANCELLED


def test_op04_user_cancels_at_13_40_is_rejected(svc, conn, users, clock, at_14):
    clock.value = at(13, 40)
    with pytest.raises(InvalidStateError, match="Storno limit"):
        svc.cancel(users["ev"], at_14)
    assert state(conn, at_14) == CONFIRMED


def test_op04_admin_cancels_at_13_50_is_cancelled_by_admin(svc, conn, users, clock, at_14):
    clock.value = at(13, 50)
    assert svc.cancel(users["admin"], at_14) == CANCELLED_BY_ADMIN
    assert check(svc, AC01, at(14), 2)["available"] is True
    assert any("zrušena administrátorem" in m for m in messages_for(conn, users["ev"]))


def test_op04_admin_can_cancel_running_but_not_finished_reservation(svc, users, clock, at_14):
    finished = confirmed(svc, users, "ev2", AC02, at(14), 1)  # [14:00, 14:30)
    clock.value = at(14, 30)
    assert svc.cancel(users["admin"], at_14) == CANCELLED_BY_ADMIN  # [14:00, 15:00) právě probíhá
    with pytest.raises(InvalidStateError, match="skončila"):
        svc.cancel(users["admin"], finished)


def test_op04_v02_user_withdraws_pending_request_without_30_minute_limit(svc, conn, users, clock):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    clock.value = at(9, 40)  # 20 min před začátkem, lhůta schválení (09:45) ještě běží
    assert svc.cancel(users["ev"], rid) == CANCELLED
    assert check(svc, HPC08, at(10), 2)["available"] is True


def test_op04_v02_admin_cancels_pending_request(svc, conn, users):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    assert svc.cancel(users["admin"], rid) == CANCELLED
    assert any("zrušena administrátorem" in m for m in messages_for(conn, users["ev"]))


def test_op04_rejects_draft_terminal_states_and_foreign_user(svc, users, clock):
    d = draft(svc, users, "ev", AC01, at(10), 1)
    with pytest.raises(InvalidStateError, match="DRAFT"):
        svc.cancel(users["ev"], d)
    c = confirmed(svc, users, "ev", AC02, at(10), 1)
    with pytest.raises(PermissionDeniedError):
        svc.cancel(users["ev2"], c)
    svc.cancel(users["ev"], c)
    with pytest.raises(InvalidStateError):
        svc.cancel(users["ev"], c)  # už zrušená
    r = pending(svc, users, "ev", HPC08, at(10), 1)
    svc.reject(users["admin"], r)
    with pytest.raises(InvalidStateError):
        svc.cancel(users["ev"], r)


def test_op04_v02_withdraw_and_admin_decision_see_state_left_by_the_other(svc, conn, users, clock):
    """Souběh OP-04 × OP-05 nad stejnou žádostí: operace proběhnou jedna po druhé
    a druhá se vyhodnotí podle stavu, který nechala první."""
    withdrawn = pending(svc, users, "ev", HPC08, at(10), 1)
    approved = pending(svc, users, "ev", HPC09, at(10), 1)
    clock.value = at(9, 40)
    # nejdřív stažení -> rozhodnutí admina se odmítne
    svc.cancel(users["ev"], withdrawn)
    with pytest.raises(InvalidStateError, match="PENDING_APPROVAL"):
        svc.approve(users["admin"], withdrawn)
    assert state(conn, withdrawn) == CANCELLED
    # nejdřív schválení -> zrušení už platí pro CONFIRMED (30 min předem)
    svc.approve(users["admin"], approved)
    with pytest.raises(InvalidStateError, match="Storno limit"):
        svc.cancel(users["ev"], approved)
    assert state(conn, approved) == CONFIRMED


# ------------------------------------------------------------------ OP-05 Approve / Reject


def test_op05_admin_approves_in_time_and_requester_is_notified(svc, conn, users, clock):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    clock.value = at(9)
    assert svc.approve(users["admin"], rid) == CONFIRMED
    assert state(conn, rid) == CONFIRMED
    assert check(svc, HPC08, at(10), 2)["reason"] == UNAVAILABLE_COLLISION
    assert any("byla schválena" in m for m in messages_for(conn, users["ev"]))


def test_op05_approve_exactly_at_deadline_is_allowed(svc, users, clock):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    clock.value = at(9, 45)
    assert svc.approve(users["admin"], rid) == CONFIRMED


def test_op05_approve_after_deadline_is_rejected_request_expired_and_slot_free(svc, conn, users, clock):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    clock.value = at(9, 50)
    with pytest.raises(InvalidStateError, match="propadla"):
        svc.approve(users["admin"], rid)
    assert state(conn, rid) == EXPIRED
    assert check(svc, HPC08, at(10), 2)["available"] is True


def test_op05_reject_releases_slot_and_notifies_requester(svc, conn, users):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    assert svc.reject(users["admin"], rid) == REJECTED
    assert state(conn, rid) == REJECTED
    assert check(svc, HPC08, at(10), 2)["available"] is True
    assert any("zamítnuta" in m for m in messages_for(conn, users["ev"]))


def test_op05_non_admin_cannot_approve_or_reject(svc, conn, users):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    with pytest.raises(PermissionDeniedError):
        svc.approve(users["ev"], rid)
    with pytest.raises(PermissionDeniedError):
        svc.reject(users["ev2"], rid)
    assert state(conn, rid) == PENDING_APPROVAL


def test_op05_only_pending_approval_can_be_decided(svc, users):
    d = draft(svc, users, "ev", HPC08, at(10), 1)
    with pytest.raises(InvalidStateError):
        svc.approve(users["admin"], d)
    c = confirmed(svc, users, "ev", AC01, at(10), 1)
    with pytest.raises(InvalidStateError):
        svc.reject(users["admin"], c)


# ------------------------------------------------------------------ BR-05 / BR-06 časové přechody


def test_br05_unconfirmed_draft_expires_without_any_operation(svc, users, clock):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    clock.value = at(8, 6)
    [res] = svc.list_for_user(users["ev"])
    assert res["id"] == rid and res["state"] == EXPIRED


def test_br06_undecided_request_expires_and_releases_slot(svc, users, clock):
    rid = pending(svc, users, "ev", HPC08, at(10), 2)
    clock.value = at(9, 45)
    assert check(svc, HPC08, at(10), 2)["available"] is False
    clock.value = at(9, 45, 1)
    assert check(svc, HPC08, at(10), 2)["available"] is True
    assert svc.get_reservation(rid)["state"] == EXPIRED


# ------------------------------------------------------------------ přehledy dostupnosti


def test_availability_grid_states(svc, users, clock):
    confirmed(svc, users, "ev", HPC08, at(10), 2)  # sloty 20, 21
    pending(svc, users, "ev2", HPC08, at(11), 1)  # slot 22
    slots = svc.availability(HPC08, date(2026, 9, 15), viewer_id=users["ev"])
    assert len(slots) == 48
    assert [s["state"] for s in slots[19:24]] == ["FREE", "RESERVED", "RESERVED", "PENDING", "FREE"]
    assert slots[20]["mine"] and not slots[22]["mine"]
    assert slots[15]["state"] == "PAST"

    clock.value = at(10, 15)
    slots = svc.availability(HPC08, date(2026, 9, 15))
    assert [s["state"] for s in slots[20:22]] == ["IN_USE", "RESERVED"]


def test_stations_overview_statuses(svc, users, clock):
    confirmed(svc, users, "ev", AC01, at(9), 2)
    confirmed(svc, users, "ev2", AC02, at(15), 1)
    pending(svc, users, "ev3", HPC08, at(16), 1)
    clock.value = at(9, 30)
    overview = {s["id"]: s for s in svc.stations_overview()}
    assert overview[AC01]["status"] == "IN_USE"
    assert overview[AC02]["status"] == "RESERVED"
    assert overview[HPC08]["status"] == "PENDING"
    assert overview[3]["status"] == "FREE"


# ------------------------------------------------------------------ Notification Service (boundary)


def test_released_hpc_slot_notifies_draft_owner_and_eligible_lower_power_users(svc, conn, users):
    a = confirmed(svc, users, "ev", HPC08, at(10), 2)
    waiting = draft(svc, users, "ev2", HPC08, at(10, 30), 2)  # potvrzení teď kolize odmítne
    confirmed(svc, users, "ev3", AC01, at(10), 1)
    confirmed(svc, users, "phev", AC02, at(10), 2)
    before = {u: len(messages_for(conn, uid)) for u, uid in users.items()}

    svc.cancel(users["admin"], a)

    new = {u: messages_for(conn, uid)[: len(messages_for(conn, uid)) - before[u]] for u, uid in users.items()}
    assert len(new["ev2"]) == 1 and f"návrh #{waiting} teď můžete potvrdit" in new["ev2"][0]
    assert len(new["ev3"]) == 1 and "výkonnější nabíječka HPC 08" in new["ev3"][0]
    assert new["phev"] == []  # PHEV nesmí na HPC -> neupozorňujeme
    assert len(new["ev"]) == 1 and "zrušena administrátorem" in new["ev"][0]
    assert svc.confirm(users["ev2"], waiting) == PENDING_APPROVAL


def test_released_dc_slot_notifies_phev_on_weaker_station(svc, conn, users):
    a = confirmed(svc, users, "ev", DC07, at(10), 2)
    confirmed(svc, users, "phev", AC01, at(10), 1)
    svc.cancel(users["ev"], a)
    assert any("DC 07 (50 kW)" in m for m in messages_for(conn, users["phev"]))


def outbox(conn):
    return conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()


class BrokenNotifier:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise TimeoutError("notification provider down")

        return fail


def test_adr01_event_is_written_in_same_transaction_and_delivered_later(svc, conn, users):
    rid = pending(svc, users, "ev", HPC08, at(10), 1)
    svc.approve(users["admin"], rid)
    [event] = outbox(conn)
    assert event["event"] == "reservation_approved" and event["sent_at"] is None
    assert json.loads(event["payload"])["reservation"]["id"] == rid
    assert notifications.list_for_user(conn, users["ev"]) == []  # ještě nedoručeno
    assert deliver(conn) == 1
    assert outbox(conn)[0]["sent_at"] is not None
    assert any("byla schválena" in m for m in messages_for(conn, users["ev"]))


def test_adr01_rolled_back_operation_writes_no_event(svc, conn, users):
    rid = pending(svc, users, "ev", HPC08, at(10), 1)
    with pytest.raises(PermissionDeniedError):
        svc.approve(users["ev2"], rid)  # ne-admin -> ROLLBACK
    other = draft(svc, users, "ev2", HPC08, at(10), 1)
    with pytest.raises(ConflictError):
        svc.confirm(users["ev2"], other)
    assert outbox(conn) == []


def test_adr01_notification_failure_keeps_event_for_retry(svc, conn, users):
    rid = pending(svc, users, "ev", HPC08, at(10), 1)
    svc.approve(users["admin"], rid)
    assert state(conn, rid) == CONFIRMED  # business změna platí i při výpadku Notification Service

    assert deliver(conn, BrokenNotifier()) == 0
    [event] = outbox(conn)
    assert event["sent_at"] is None and event["attempts"] == 1 and "TimeoutError" in event["last_error"]
    assert notifications.list_for_user(conn, users["ev"]) == []

    assert deliver(conn) == 1  # další pokus už projde
    assert any("byla schválena" in m for m in messages_for(conn, users["ev"]))
    assert deliver(conn) == 0  # nedoručuje se dvakrát
