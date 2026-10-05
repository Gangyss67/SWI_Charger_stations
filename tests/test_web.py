"""Testy webové vrstvy: přihlášení, CSRF, formuláře, role, JSON API (CP1 walking skeleton) – baseline v0.2."""
from datetime import datetime

from charging.db import connect

TOKEN = "test-csrf-token"
DAY = "2026-09-15"  # "teď" = 15.09.2026 08:00 (conftest.clock)
PLATE = "1AB 2345"


def set_csrf(client):
    with client.session_transaction() as session:
        session["_csrf_token"] = TOKEN
    return TOKEN


def login(client, username, password):
    set_csrf(client)
    client.post("/logout", data={"csrf_token": TOKEN})  # přihlášený uživatel by byl z /login jen přesměrován
    set_csrf(client)
    response = client.post("/login", data={"username": username, "password": password, "csrf_token": TOKEN})
    set_csrf(client)  # login maže session (ochrana proti session fixation)
    return response


def as_admin(client):
    login(client, "admin", "admin1234")


def as_ev(client):
    login(client, "ridic_ev", "ev123456")


def as_phev(client):
    login(client, "ridic_phev", "phev1234")


def db_rows(app, sql, params=()):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def api_post(client, url, payload=None):
    return client.post(url, json=payload or {}, headers={"X-CSRF-Token": TOKEN})


def create(client, station_id, time="10:00", slots=2, **extra):
    payload = {"station_id": station_id, "date": DAY, "time": time, "slots": slots, "license_plate": PLATE, **extra}
    return api_post(client, "/reservations", payload)


def availability(client, station_id, time="10:00", slots=2, vehicle_type="BEV"):
    url = f"/api/availability?station_id={station_id}&date={DAY}&time={time}&slots={slots}&vehicle_type={vehicle_type}"
    return client.get(url).get_json()["stations"][0]


# ------------------------------------------------------------------ auth


def test_register_login_logout(client, app):
    set_csrf(client)
    r = client.post(
        "/register",
        data={"username": "novak", "password": "tajneheslo", "password_again": "tajneheslo", "vehicle_type": "PHEV", "csrf_token": TOKEN},
    )
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    row = db_rows(app, "SELECT * FROM users WHERE username = 'novak'")[0]
    assert row["vehicle_type"] == "PHEV" and row["password_hash"] != "tajneheslo"

    r = login(client, "novak", "tajneheslo")
    assert r.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "novak" in page and "Odhlásit" in page

    client.post("/logout", data={"csrf_token": TOKEN})
    assert "Přihlásit" in client.get("/").get_data(as_text=True)


def test_register_rejects_duplicate_and_short_password(client):
    set_csrf(client)
    r = client.post(
        "/register",
        data={"username": "ridic_ev", "password": "12345678", "password_again": "12345678", "vehicle_type": "BEV", "csrf_token": TOKEN},
    )
    assert "obsazené" in r.get_data(as_text=True)
    r = client.post(
        "/register",
        data={"username": "karel", "password": "123", "password_again": "123", "vehicle_type": "BEV", "csrf_token": TOKEN},
    )
    assert "alespoň 8" in r.get_data(as_text=True)


def test_login_wrong_password(client):
    r = login(client, "ridic_ev", "spatneheslo")
    assert r.status_code == 200 and "Nesprávné" in r.get_data(as_text=True)
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_post_without_csrf_token_is_rejected(client):
    as_ev(client)
    r = client.post("/reservations", data={"station_id": 1, "date": DAY, "time": "10:00", "slots": 1})
    assert r.status_code == 400


def test_reservation_pages_require_login(client):
    assert client.get("/reservations").status_code == 302
    set_csrf(client)
    assert create(client, 1).status_code == 401
    assert api_post(client, "/reservations/1/approve").status_code == 401


def test_admin_page_forbidden_for_normal_user(client):
    as_ev(client)
    assert client.get("/admin/reservations").status_code == 403


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get(f"/?date={DAY}&time=10:00&slots=2&vehicle_type=PHEV").status_code == 200
    assert client.get(f"/stations/8?date={DAY}").status_code == 200
    assert client.get("/stations/999").status_code == 404
    as_admin(client)
    urls = ["/", "/stations/1", "/stations/8", "/reservations", "/notifications"]
    urls += [f"/admin/reservations?state={s}" for s in ("PENDING_APPROVAL", "CONFIRMED", "EXPIRED", "ALL")]
    for url in urls:
        assert client.get(url).status_code == 200, url


# ------------------------------------------------------------------ CP1 walking skeleton


def test_cp1_walking_skeleton_post_reservation_returns_id_and_persists(client, app):
    as_ev(client)
    r = create(client, 8, vehicle_type="BEV")
    assert r.status_code == 201
    body = r.get_json()
    assert isinstance(body["id"], int) and body["state"] == "DRAFT"
    assert body["draft_expires_at"] == "2026-09-15 08:05:00"

    row = db_rows(app, "SELECT * FROM reservations WHERE id = ?", (body["id"],))[0]
    assert (row["station_id"], row["start_time"], row["end_time"], row["state"], row["license_plate"]) == (
        8, "2026-09-15 10:00", "2026-09-15 11:00", "DRAFT", PLATE,
    )


def test_cp1_validation_errors(client):
    as_phev(client)
    assert create(client, 8).status_code == 400  # PHEV z profilu na HPC (BR-04)
    assert create(client, 8, vehicle_type="PHEV").status_code == 400
    assert create(client, 1, time="10:10").status_code == 400  # BR-01
    assert create(client, 1, slots=20).status_code == 400
    assert create(client, 1, license_plate="").status_code == 400  # chybějící SPZ
    assert create(client, "1").status_code == 400
    assert create(client, 999).status_code == 404
    r = client.post("/reservations", data="[]", content_type="application/json", headers={"X-CSRF-Token": TOKEN})
    assert r.status_code == 400


# ------------------------------------------------------------------ OP-03 + OP-05 přes HTTP


def test_form_flow_ac_station_is_confirmed_immediately(client, app):
    as_phev(client)
    r = client.post(
        "/reservations",
        data={"station_id": 1, "date": DAY, "time": "10:00", "slots": 1, "vehicle_type": "PHEV",
              "license_plate": PLATE, "action": "confirm", "csrf_token": TOKEN},
        follow_redirects=True,
    )
    assert "je potvrzena (CONFIRMED)" in r.get_data(as_text=True)
    assert db_rows(app, "SELECT state FROM reservations")[0]["state"] == "CONFIRMED"


def test_form_flow_hpc_request_blocks_slot_and_admin_approves(client, app):
    as_ev(client)
    r = client.post(
        "/reservations",
        data={"station_id": 8, "date": DAY, "time": "10:00", "slots": 2, "vehicle_type": "BEV",
              "license_plate": PLATE, "action": "confirm", "csrf_token": TOKEN},
        follow_redirects=True,
    )
    assert "PENDING_APPROVAL" in r.get_data(as_text=True)
    rid = db_rows(app, "SELECT id FROM reservations")[0]["id"]

    as_admin(client)  # jiný uživatel: slot je v OP-01 nedostupný
    assert availability(client, 8, time="10:30")["reason"] == "PENDING_APPROVAL"
    page = client.get("/admin/reservations").get_data(as_text=True)
    assert f"<td>{rid}</td>" in page and "Rozhodnout do 2026-09-15 09:45" in page
    client.post(f"/reservations/{rid}/approve", data={"csrf_token": TOKEN, "next": "admin"})
    assert db_rows(app, "SELECT state FROM reservations WHERE id = ?", (rid,))[0]["state"] == "CONFIRMED"

    as_ev(client)
    assert "byla schválena" in client.get("/notifications").get_data(as_text=True)


def test_json_actions_and_status_codes(client, clock):
    as_ev(client)
    rid = create(client, 8).get_json()["id"]
    other = create(client, 8, time="10:30").get_json()["id"]
    r = api_post(client, f"/reservations/{rid}/confirm")
    assert r.status_code == 200 and r.get_json()["state"] == "PENDING_APPROVAL"
    assert api_post(client, f"/reservations/{other}/confirm").status_code == 409  # kolize s PENDING_APPROVAL
    assert api_post(client, f"/reservations/{rid}/approve").status_code == 403  # ne-admin
    assert api_post(client, "/reservations/999/confirm").status_code == 404

    as_admin(client)
    r = api_post(client, f"/reservations/{rid}/reject")
    assert r.status_code == 200 and r.get_json()["state"] == "REJECTED"
    assert api_post(client, f"/reservations/{rid}/approve").status_code == 409  # už rozhodnuto
    assert availability(client, 8)["available"] is True


def test_approve_after_deadline_over_http_marks_request_expired(client, clock):
    as_ev(client)
    rid = create(client, 7, time="10:00", slots=1).get_json()["id"]
    api_post(client, f"/reservations/{rid}/confirm")
    clock.value = datetime(2026, 9, 15, 9, 50)
    as_admin(client)
    r = api_post(client, f"/reservations/{rid}/approve")
    assert r.status_code == 409 and "propadla" in r.get_json()["error"]
    assert client.get(f"/api/reservations/{rid}").get_json()["state"] == "EXPIRED"


def test_user_cannot_cancel_foreign_reservation_but_can_withdraw_own_request(client, app):
    as_ev(client)
    rid = create(client, 8).get_json()["id"]
    api_post(client, f"/reservations/{rid}/confirm")
    as_phev(client)
    assert client.post(f"/reservations/{rid}/cancel", data={"csrf_token": TOKEN}).status_code == 403
    assert client.get(f"/api/reservations/{rid}").status_code == 403
    as_ev(client)
    r = api_post(client, f"/reservations/{rid}/cancel")
    assert r.status_code == 200 and r.get_json()["state"] == "CANCELLED"


# ------------------------------------------------------------------ OP-01 API


def test_availability_api(client):
    r = client.get(f"/api/availability?date={DAY}&time=10:00&slots=2&vehicle_type=PHEV")
    assert r.status_code == 200
    stations = r.get_json()["stations"]
    assert len(stations) == 10
    assert [s["name"] for s in stations if not s["available"]] == ["HPC 08", "HPC 09", "HPC 10"]
    assert client.get(f"/api/availability?date={DAY}&time=10:07&slots=2&vehicle_type=BEV").status_code == 400
    assert client.get(f"/api/availability?date={DAY}&time=10:00&slots=2").status_code == 400  # typ vozu chybí
    assert client.get(f"/api/availability?station_id=999&date={DAY}&time=10:00&slots=2&vehicle_type=BEV").status_code == 404

    r = client.get(f"/api/stations/1/availability?date={DAY}")
    body = r.get_json()
    assert len(body["slots"]) == 48 and body["slots"][20] == {"start": "10:00", "end": "10:30", "state": "FREE"}
    assert client.get("/api/stations/1/availability?date=bad").status_code == 400
    assert client.get(f"/api/stations/999/availability?date={DAY}").status_code == 404
