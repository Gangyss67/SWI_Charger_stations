"""Testy doménové logiky podle specifikace baseline v0.2 (cv2.docx).

Každá operace OP-01..OP-05 má testy pojmenované podle příkladů ověření ze specifikace;
"teď" je 15.09.2026 08:00 (viz conftest.clock). Vozidla z katalogu (db.VEHICLE_MODELS):
Enyaq 135 kW, Fiat 500e 85 kW, Zoe 50 kW, e-up! 40 kW, Outlander PHEV 22 kW DC, RAV4 PHEV jen AC.
"""
import sqlite3
import threading
from datetime import date, datetime

import pytest

from charging.db import connect
from charging.services import notifications
from charging.services.reservations import (
    ADMISSION_AUTO,
    ADMISSION_EXCEPTION,
    CANCELLED,
    CANCELLED_BY_ADMIN,
    CONFIRMED,
    DRAFT,
    EXPIRED,
    PENDING_APPROVAL,
    REJECTED,
    UNAVAILABLE_COLLISION,
    UNAVAILABLE_NO_DC,
    UNAVAILABLE_PENDING,
    UNAVAILABLE_VEHICLE,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
    ReservationError,
    ReservationService,
    admission,
)
from conftest import make_user

AC01, AC02, DC07, HPC08, HPC09 = 1, 2, 7, 8, 9
ENYAQ, FIAT_500E, ZOE, E_UP, OUTLANDER, RAV4 = 1, 7, 8, 10, 14, 15
PLATE = "1AB 2345"
VEHICLE = {"ev": ENYAQ, "ev2": ENYAQ, "ev3": ENYAQ, "mesto": E_UP, "phev": OUTLANDER, "rav4": RAV4, "admin": ENYAQ}


def at(hour, minute=0, second=0):
    return datetime(2026, 9, 15, hour, minute, second)


@pytest.fixture
def users(conn):
    ids = {name: make_user(conn, name, VEHICLE[name]) for name in ("ev", "ev2", "ev3", "mesto", "phev", "rav4")}
    ids["admin"] = make_user(conn, "admin", ENYAQ, is_admin=True)
    return ids


def draft(svc, users, who, station, start, slots, model=None):
    return svc.create(users[who], station, start, slots, model or VEHICLE[who], PLATE)


def confirmed(svc, users, who, station, start, slots):
    """Návrh + potvrzení; výjimku rovnou schválí administrátor."""
    rid = draft(svc, users, who, station, start, slots)
    if svc.confirm(users[who], rid) == PENDING_APPROVAL:
        svc.approve(users["admin"], rid)
    return rid


def pending(svc, users, who, station, start, slots):
    """Žádost o výjimku – slabší vozidlo na rychlé stanici."""
    rid = draft(svc, users, who, station, start, slots)
    assert svc.confirm(users[who], rid) == PENDING_APPROVAL
    return rid


def state(conn, reservation_id):
    return conn.execute("SELECT state FROM reservations WHERE id = ?", (reservation_id,)).fetchone()["state"]


def check(svc, station, start, slots, model=ENYAQ):
    return svc.search_availability(start, slots, model, station_id=station)[0]


def messages_for(conn, user_id):
    return [row["message"] for row in notifications.list_for_user(conn, user_id)]


# ------------------------------------------------------------------ seed data a BR-06


def test_ten_stations_four_fast_and_vehicle_catalog(svc):
    stations = {s["name"]: s for s in svc.stations_overview()}
    assert len(stations) == 10
    assert [n for n, s in stations.items() if s["is_high_speed"]] == ["HPC 08", "HPC 09", "HPC 10"]
    assert {n: s["min_vehicle_kw"] for n, s in stations.items() if s["is_fast"]} == {
        "DC 07": 25, "HPC 08": 75, "HPC 09": 75, "HPC 10": 75,
    }
    models = svc.list_vehicle_models()
    assert len(models) == 16 and any(m["max_dc_kw"] == 0 for m in models)


@pytest.mark.parametrize(
    "power, high_speed, vehicle_kw, expected",
    [
        (11, 0, 0, ADMISSION_AUTO),  # AC stanice: každé vozidlo
        (50, 0, 25, ADMISSION_AUTO),  # DC 07: hranice poloviny výkonu
        (50, 0, 24, ADMISSION_EXCEPTION),
        (150, 1, 75, ADMISSION_AUTO),  # HPC: hranice poloviny výkonu
        (150, 1, 74, ADMISSION_EXCEPTION),
    ],
)
def test_br06_admission_by_half_station_power(power, high_speed, vehicle_kw, expected):
    station = {"power_kw": power, "is_high_speed": high_speed}
    assert admission(station, {"max_dc_kw": vehicle_kw}) == expected


# ------------------------------------------------------------------ OP-01 Check Availability


def test_op01_examples_with_confirmed_on_hpc08(svc, users):
    confirmed(svc, users, "ev", HPC08, at(10), 2)  # existující CONFIRMED [10:00, 11:00)
    assert check(svc, HPC08, at(9), 2)["available"] is True  # [09:00, 10:00)
    r = check(svc, HPC08, at(10, 30), 2)  # [10:30, 11:30)
    assert r["available"] is False and r["reason"] == UNAVAILABLE_COLLISION
    assert check(svc, HPC08, at(11), 2)["available"] is True  # [11:00, 12:00) hrana nekoliduje
    r = check(svc, HPC08, at(11), 2, model=OUTLANDER)  # PHEV
    assert r["available"] is False and r["reason"] == UNAVAILABLE_VEHICLE


def test_op01_v02_pending_approval_blocks_station(svc, users):
    pending(svc, users, "mesto", HPC08, at(10), 2)
    r = check(svc, HPC08, at(10, 30), 2)
    assert r["available"] is False and r["reason"] == UNAVAILABLE_PENDING


def test_op01_v02_reports_admission_for_selected_vehicle(svc):
    by_name = lambda model: {r["name"]: r for r in svc.search_availability(at(10), 2, model)}  # noqa: E731
    enyaq, e_up, rav4 = by_name(ENYAQ), by_name(E_UP), by_name(RAV4)
    assert enyaq["HPC 08"]["admission"] == ADMISSION_AUTO
    assert e_up["HPC 08"]["admission"] == ADMISSION_EXCEPTION and e_up["HPC 08"]["requires_approval"]
    assert e_up["DC 07"]["admission"] == ADMISSION_AUTO  # 40 kW >= 25 kW
    assert rav4["DC 07"]["reason"] == UNAVAILABLE_NO_DC
    assert rav4["HPC 08"]["reason"] == UNAVAILABLE_VEHICLE
    assert rav4["AC 01"]["admission"] == ADMISSION_AUTO


def test_op01_draft_does_not_block(svc, users):
    draft(svc, users, "ev", AC01, at(10), 2)
    assert check(svc, AC01, at(10), 2)["available"] is True


@pytest.mark.parametrize("start, slots", [(at(10, 15), 1), (at(7, 30), 1), (at(10), 0), (at(10), 9)])
def test_op01_rejects_invalid_interval(svc, start, slots):
    with pytest.raises(ReservationError):
        svc.search_availability(start, slots, ENYAQ)


def test_op01_requires_vehicle_from_catalog(svc):
    with pytest.raises(ReservationError):
        svc.search_availability(at(10), 1, None)
    with pytest.raises(NotFoundError):
        svc.search_availability(at(10), 1, 999)


# ------------------------------------------------------------------ OP-02 Create Reservation


def test_op02_example_ac01_phev_creates_draft_with_5_minute_expiry(svc, conn, users):
    rid = draft(svc, users, "phev", AC01, at(10), 1)
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert (row["state"], row["start_time"], row["end_time"]) == (DRAFT, "2026-09-15 10:00", "2026-09-15 10:30")
    assert (row["vehicle_model_id"], row["license_plate"]) == (OUTLANDER, PLATE)
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


def test_op02_v02_vehicle_without_dc_is_rejected_on_fast_station(svc, conn, users):
    with pytest.raises(ReservationError, match="stejnosměrně"):
        draft(svc, users, "rav4", DC07, at(10), 1)
    assert state(conn, draft(svc, users, "rav4", AC01, at(10), 1)) == DRAFT


def test_op02_requires_vehicle_from_catalog(svc, users):
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC01, at(10), 1, None, PLATE)
    with pytest.raises(NotFoundError):
        svc.create(users["ev"], AC01, at(10), 1, 999, PLATE)


@pytest.mark.parametrize("plate", ["", "   ", None, "ÁBC 123", "1234567890123"])
def test_op02_requires_valid_license_plate(svc, users, plate):
    with pytest.raises(ReservationError):
        svc.create(users["ev"], AC01, at(10), 1, ENYAQ, plate)


def test_op02_normalizes_license_plate(svc, conn, users):
    rid = svc.create(users["ev"], AC01, at(10), 1, ENYAQ, "  1ab   2345 ")
    assert conn.execute("SELECT license_plate FROM reservations WHERE id = ?", (rid,)).fetchone()[0] == "1AB 2345"


def test_op02_rejects_past_start_unknown_station_and_user(svc, users):
    with pytest.raises(ReservationError):
        draft(svc, users, "ev", AC01, at(8), 1)  # teď je 08:00 -> začátek není v budoucnu
    with pytest.raises(NotFoundError):
        draft(svc, users, "ev", 999, at(10), 1)
    with pytest.raises(NotFoundError):
        svc.create(999, AC01, at(10), 1, ENYAQ, PLATE)


def test_op02_does_not_check_collisions(svc, conn, users):
    confirmed(svc, users, "ev", AC01, at(10), 2)
    rid = draft(svc, users, "ev2", AC01, at(10), 2)  # kolize se ověří až v OP-03
    assert state(conn, rid) == DRAFT


def test_db_blocks_phev_on_hpc_vehicle_without_dc_and_non_draft_insert(conn, users):
    insert = (
        "INSERT INTO reservations (user_id, station_id, vehicle_model_id, license_plate, start_time, end_time, state,"
        " created_at, updated_at, draft_expires_at) VALUES (?, ?, ?, 'X', '2026-09-15 10:00', '2026-09-15 10:30',"
        " ?, 'x', 'x', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="HIGH_SPEED_BEV_ONLY"):
        conn.execute(insert, (users["phev"], HPC08, OUTLANDER, DRAFT))
    with pytest.raises(sqlite3.IntegrityError, match="DC_NOT_SUPPORTED"):
        conn.execute(insert, (users["rav4"], DC07, RAV4, DRAFT))
    with pytest.raises(sqlite3.IntegrityError, match="INVALID_INITIAL_STATE"):
        conn.execute(insert, (users["ev"], AC01, ENYAQ, CONFIRMED))


# ------------------------------------------------------------------ OP-03 Confirm Reservation


def test_op03_ac_draft_without_collision_is_confirmed_immediately(svc, conn, users):
    rid = draft(svc, users, "rav4", AC01, at(10), 1)
    assert svc.confirm(users["rav4"], rid) == CONFIRMED


@pytest.mark.parametrize("who, station", [("ev", HPC08), ("mesto", DC07)])
def test_op03_v02_strong_enough_vehicle_on_fast_station_is_confirmed_immediately(svc, conn, users, who, station):
    rid = draft(svc, users, who, station, at(10), 2)
    assert svc.confirm(users[who], rid) == CONFIRMED
    assert conn.execute("SELECT approval_deadline FROM reservations WHERE id = ?", (rid,)).fetchone()[0] is None


@pytest.mark.parametrize("who, station", [("mesto", HPC08), ("phev", DC07)])
def test_op03_v02_weaker_vehicle_on_fast_station_requests_exception(svc, conn, users, who, station):
    rid = draft(svc, users, who, station, at(10), 2)
    assert svc.confirm(users[who], rid) == PENDING_APPROVAL
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert row["state"] == PENDING_APPROVAL and row["approval_deadline"] == "2026-09-15 09:45:00"


def test_op03_collision_with_confirmed_is_rejected_and_stays_draft(svc, conn, users):
    confirmed(svc, users, "ev", AC01, at(10), 2)
    rid = draft(svc, users, "ev2", AC01, at(10, 30), 2)
    with pytest.raises(ConflictError):
        svc.confirm(users["ev2"], rid)
    assert state(conn, rid) == DRAFT


def test_op03_v02_collision_with_pending_approval_is_rejected(svc, conn, users):
    pending(svc, users, "mesto", HPC08, at(10), 2)
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


def test_op03_v02_exception_less_than_15_minutes_before_start_is_rejected(svc, conn, users, clock):
    clock.value = at(9, 44)
    weak = draft(svc, users, "mesto", HPC08, at(10), 1)
    strong = draft(svc, users, "ev", HPC09, at(10), 1)
    clock.value = at(9, 46)  # T_start − 15 min = 09:45 už minulo
    with pytest.raises(InvalidStateError, match="nezbývá čas"):
        svc.confirm(users["mesto"], weak)
    assert state(conn, weak) == DRAFT
    assert svc.confirm(users["ev"], strong) == CONFIRMED  # bez výjimky lhůta neplatí


def test_op03_v02_exception_exactly_at_approval_deadline_is_allowed(svc, users, clock):
    clock.value = at(9, 44)
    rid = draft(svc, users, "phev", DC07, at(10), 1)
    clock.value = at(9, 45)
    assert svc.confirm(users["phev"], rid) == PENDING_APPROVAL


def test_op03_only_owner_and_only_from_draft(svc, users):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    with pytest.raises(PermissionDeniedError):
        svc.confirm(users["ev2"], rid)
    svc.confirm(users["ev"], rid)
    with pytest.raises(InvalidStateError):
        svc.confirm(users["ev"], rid)


@pytest.mark.parametrize("station, model, expected", [(AC01, ENYAQ, CONFIRMED), (HPC08, E_UP, PENDING_APPROVAL)])
def test_op03_req04_concurrent_confirms_allow_exactly_one(conn, db_path, clock, station, model, expected):
    """REQ-04 + future pressure Q: 10 souběžných potvrzení kolidujících DRAFT (každé ve vlastním
    připojení a vlákně) -> do blokujícího stavu přejde právě jedno."""
    setup = ReservationService(conn, None, clock)
    users = [make_user(conn, f"race{i}", model) for i in range(10)]
    drafts = [setup.create(uid, station, at(10), 2, model, PLATE) for uid in users]
    barrier = threading.Barrier(len(users))
    outcomes = []

    def worker(user_id, reservation_id):
        c = connect(db_path)
        try:
            svc = ReservationService(c, None, clock)
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
    strong = draft(svc, users, "ev", HPC08, at(10), 1)
    weak = draft(svc, users, "mesto", HPC09, at(10), 1)
    with pytest.raises(sqlite3.IntegrityError, match="APPROVAL_REQUIRED"):
        conn.execute("UPDATE reservations SET state = 'PENDING_APPROVAL', approval_deadline = 'x' WHERE id = ?", (strong,))
    with pytest.raises(sqlite3.IntegrityError, match="APPROVAL_REQUIRED"):
        conn.execute("UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (weak,))

    ac = draft(svc, users, "ev", AC01, at(10), 1)
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
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    clock.value = at(9, 40)  # 20 min před začátkem, lhůta schválení (09:45) ještě běží
    assert svc.cancel(users["mesto"], rid) == CANCELLED
    assert check(svc, HPC08, at(10), 2)["available"] is True


def test_op04_v02_admin_cancels_pending_request(svc, conn, users):
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    assert svc.cancel(users["admin"], rid) == CANCELLED
    assert any("zrušena administrátorem" in m for m in messages_for(conn, users["mesto"]))


def test_op04_rejects_draft_terminal_states_and_foreign_user(svc, users):
    d = draft(svc, users, "ev", AC01, at(10), 1)
    with pytest.raises(InvalidStateError, match="DRAFT"):
        svc.cancel(users["ev"], d)
    c = confirmed(svc, users, "ev", AC02, at(10), 1)
    with pytest.raises(PermissionDeniedError):
        svc.cancel(users["ev2"], c)
    svc.cancel(users["ev"], c)
    with pytest.raises(InvalidStateError):
        svc.cancel(users["ev"], c)  # už zrušená
    r = pending(svc, users, "mesto", HPC08, at(10), 1)
    svc.reject(users["admin"], r)
    with pytest.raises(InvalidStateError):
        svc.cancel(users["mesto"], r)


# ------------------------------------------------------------------ OP-05 Approve / Reject


def test_op05_admin_approves_in_time_and_requester_is_notified(svc, conn, users, clock):
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    clock.value = at(9)
    assert svc.approve(users["admin"], rid) == CONFIRMED
    assert state(conn, rid) == CONFIRMED
    assert check(svc, HPC08, at(10), 2)["reason"] == UNAVAILABLE_COLLISION
    assert any("byla schválena" in m for m in messages_for(conn, users["mesto"]))


def test_op05_approve_exactly_at_deadline_is_allowed(svc, users, clock):
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    clock.value = at(9, 45)
    assert svc.approve(users["admin"], rid) == CONFIRMED


def test_op05_approve_after_deadline_is_rejected_request_expired_and_slot_free(svc, conn, users, clock):
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    clock.value = at(9, 50)
    with pytest.raises(InvalidStateError, match="propadla"):
        svc.approve(users["admin"], rid)
    assert state(conn, rid) == EXPIRED
    assert check(svc, HPC08, at(10), 2)["available"] is True


def test_op05_reject_releases_slot_and_notifies_requester(svc, conn, users):
    rid = pending(svc, users, "phev", DC07, at(10), 2)
    assert svc.reject(users["admin"], rid) == REJECTED
    assert state(conn, rid) == REJECTED
    assert check(svc, DC07, at(10), 2)["available"] is True
    assert any("zamítnuta" in m for m in messages_for(conn, users["phev"]))


def test_op05_non_admin_cannot_approve_or_reject(svc, conn, users):
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    with pytest.raises(PermissionDeniedError):
        svc.approve(users["mesto"], rid)
    with pytest.raises(PermissionDeniedError):
        svc.reject(users["ev2"], rid)
    assert state(conn, rid) == PENDING_APPROVAL


def test_op05_only_pending_approval_can_be_decided(svc, users):
    d = draft(svc, users, "mesto", HPC08, at(10), 1)
    with pytest.raises(InvalidStateError):
        svc.approve(users["admin"], d)
    c = confirmed(svc, users, "ev", HPC09, at(10), 1)  # automaticky připuštěné vozidlo
    with pytest.raises(InvalidStateError):
        svc.reject(users["admin"], c)


# ------------------------------------------------------------------ BR-05 / BR-06 časové přechody


def test_br05_unconfirmed_draft_expires_without_any_operation(svc, users, clock):
    rid = draft(svc, users, "ev", AC01, at(10), 1)
    clock.value = at(8, 6)
    [res] = svc.list_for_user(users["ev"])
    assert res["id"] == rid and res["state"] == EXPIRED


def test_br06_undecided_request_expires_and_releases_slot(svc, users, clock):
    rid = pending(svc, users, "mesto", HPC08, at(10), 2)
    clock.value = at(9, 45)
    assert check(svc, HPC08, at(10), 2)["available"] is False
    clock.value = at(9, 45, 1)
    assert check(svc, HPC08, at(10), 2)["available"] is True
    assert svc.get_reservation(rid)["state"] == EXPIRED


# ------------------------------------------------------------------ přehledy dostupnosti


def test_availability_grid_states(svc, users, clock):
    confirmed(svc, users, "ev", HPC08, at(10), 2)  # sloty 20, 21
    pending(svc, users, "mesto", HPC08, at(11), 1)  # slot 22
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
    pending(svc, users, "mesto", HPC08, at(16), 1)
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
    assert svc.confirm(users["ev2"], waiting) == CONFIRMED


def test_released_dc_slot_notifies_only_vehicles_with_dc(svc, conn, users):
    a = confirmed(svc, users, "ev", DC07, at(10), 2)
    confirmed(svc, users, "phev", AC01, at(10), 1)  # Outlander umí DC 22 kW
    confirmed(svc, users, "rav4", AC02, at(10), 1)  # RAV4 jen AC
    svc.cancel(users["ev"], a)
    assert any("DC 07 (50 kW)" in m for m in messages_for(conn, users["phev"]))
    assert messages_for(conn, users["rav4"]) == []


def test_notification_failure_does_not_break_reservation(conn, users, clock):
    class BrokenNotifier:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise TimeoutError("notification provider down")

            return fail

    svc = ReservationService(conn, BrokenNotifier(), clock)
    rid = pending(svc, users, "mesto", HPC08, at(10), 1)
    svc.approve(users["admin"], rid)
    svc.cancel(users["mesto"], rid)
    assert state(conn, rid) == CANCELLED
