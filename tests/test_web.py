"""Testy webové vrstvy: přihlášení, CSRF, formuláře, oprávnění, JSON API (CP1 walking skeleton)."""
from charging.db import connect

TOKEN = "test-csrf-token"


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


def db_rows(app, sql, params=()):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def post_json_reservation(client, payload):
    return client.post("/reservations", json=payload, headers={"X-CSRF-Token": TOKEN})


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
    login(client, "ridic_ev", "ev123456")
    r = client.post("/reservations", data={"station_id": 1, "date": "2026-09-15", "time": "10:00", "slots": 1})
    assert r.status_code == 400


def test_reservation_pages_require_login(client):
    assert client.get("/reservations").status_code == 302
    set_csrf(client)
    r = post_json_reservation(client, {"station_id": 1, "date": "2026-09-15", "time": "10:00", "slots": 1})
    assert r.status_code == 401


def test_admin_page_forbidden_for_normal_user(client):
    login(client, "ridic_ev", "ev123456")
    assert client.get("/admin/reservations").status_code == 403


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/stations/8?date=2026-09-15").status_code == 200
    assert client.get("/stations/999").status_code == 404
    login(client, "admin", "admin1234")
    for url in ("/", "/stations/1", "/reservations", "/admin/reservations", "/admin/reservations?state=ALL", "/notifications"):
        assert client.get(url).status_code == 200, url


# ------------------------------------------------------------------ CP1 walking skeleton


def test_cp1_walking_skeleton_post_reservation_returns_id_and_persists(client, app):
    login(client, "ridic_ev", "ev123456")
    r = post_json_reservation(client, {"station_id": 8, "date": "2026-09-15", "time": "10:00", "slots": 2})
    assert r.status_code == 201
    body = r.get_json()
    assert isinstance(body["id"], int) and body["state"] == "DRAFT"

    row = db_rows(app, "SELECT * FROM reservations WHERE id = ?", (body["id"],))[0]
    assert (row["station_id"], row["start_time"], row["end_time"], row["state"]) == (
        8, "2026-09-15 10:00", "2026-09-15 11:00", "DRAFT",
    )


def test_cp1_validation_errors(client):
    login(client, "ridic_phev", "phev1234")
    assert post_json_reservation(client, {"station_id": 8, "date": "2026-09-15", "time": "10:00", "slots": 1}).status_code == 400
    assert post_json_reservation(client, {"station_id": 1, "date": "2026-09-15", "time": "10:10", "slots": 1}).status_code == 400
    assert post_json_reservation(client, {"station_id": 1, "date": "2026-09-15", "time": "10:00", "slots": 20}).status_code == 400
    assert post_json_reservation(client, {"station_id": "1", "date": "2026-09-15", "time": "10:00", "slots": 1}).status_code == 400
    assert post_json_reservation(client, {"station_id": 999, "date": "2026-09-15", "time": "10:00", "slots": 1}).status_code == 404
    r = client.post("/reservations", data="[]", content_type="application/json", headers={"X-CSRF-Token": TOKEN})
    assert r.status_code == 400


# ------------------------------------------------------------------ form flow + admin


def test_form_flow_create_submit_approve_and_conflict(client, app):
    login(client, "ridic_ev", "ev123456")
    r = client.post(
        "/reservations",
        data={"station_id": 1, "date": "2026-09-15", "time": "10:00", "slots": 2, "action": "submit", "csrf_token": TOKEN},
    )
    assert r.status_code == 302
    rid = db_rows(app, "SELECT id, state FROM reservations")[0]
    assert rid["state"] == "PENDING"

    login(client, "admin", "admin1234")
    assert f"<td>{rid['id']}</td>" in client.get("/admin/reservations").get_data(as_text=True)
    client.post(f"/reservations/{rid['id']}/approve", data={"csrf_token": TOKEN, "next": "admin"})
    assert db_rows(app, "SELECT state FROM reservations WHERE id = ?", (rid["id"],))[0]["state"] == "CONFIRMED"

    login(client, "ridic_phev", "phev1234")
    r = client.post(
        "/reservations",
        data={"station_id": 1, "date": "2026-09-15", "time": "10:30", "slots": 1, "action": "draft", "csrf_token": TOKEN},
        follow_redirects=True,
    )
    assert "obsazený" in r.get_data(as_text=True)
    assert len(db_rows(app, "SELECT id FROM reservations")) == 1

    # ev dostal notifikaci o potvrzení
    login(client, "ridic_ev", "ev123456")
    assert "byla potvrzena" in client.get("/notifications").get_data(as_text=True)


def test_phev_form_on_high_speed_station_is_rejected(client, app):
    login(client, "ridic_phev", "phev1234")
    assert "vyhrazená pouze pro plně elektrické" in client.get("/stations/8").get_data(as_text=True)
    r = client.post(
        "/reservations",
        data={"station_id": 8, "date": "2026-09-15", "time": "10:00", "slots": 1, "csrf_token": TOKEN},
        follow_redirects=True,
    )
    assert "BEV" in r.get_data(as_text=True)
    assert db_rows(app, "SELECT id FROM reservations") == []


def test_user_cannot_cancel_or_approve_foreign_reservation(client, app):
    login(client, "ridic_ev", "ev123456")
    rid = post_json_reservation(client, {"station_id": 1, "date": "2026-09-15", "time": "10:00", "slots": 1}).get_json()["id"]
    login(client, "ridic_phev", "phev1234")
    assert client.post(f"/reservations/{rid}/cancel", data={"csrf_token": TOKEN}).status_code == 403
    assert client.post(f"/reservations/{rid}/approve", data={"csrf_token": TOKEN}).status_code == 403
    assert client.post("/reservations/999/cancel", data={"csrf_token": TOKEN}).status_code == 404
    assert db_rows(app, "SELECT state FROM reservations WHERE id = ?", (rid,))[0]["state"] == "DRAFT"


# ------------------------------------------------------------------ availability API


def test_availability_api(client):
    r = client.get("/api/stations/1/availability?date=2026-09-15")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["slots"]) == 48 and body["slots"][0] == {"start": "00:00", "end": "00:30", "state": "FREE"}
    assert client.get("/api/stations/1/availability?date=bad").status_code == 400
    assert client.get("/api/stations/999/availability?date=2026-09-15").status_code == 404

    r = client.get("/api/availability/check?station_id=1&date=2026-09-15&time=10:00&slots=2")
    assert r.get_json()["available"] is True
    r = client.get("/api/availability/check?station_id=1&date=2026-09-15&time=10:07&slots=2")
    assert r.get_json()["available"] is False

    login(client, "ridic_phev", "phev1234")
    r = client.get("/api/availability/check?station_id=8&date=2026-09-15&time=10:00&slots=2")
    assert r.get_json()["available"] is False
