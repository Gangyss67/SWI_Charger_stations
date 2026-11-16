"""Vylepšení po C10: kontrola SPZ podle státu, profil vozidel, notifikace na telefon (ntfy), migrace DB v5 -> v6."""
import http.server
import logging
import threading
from datetime import datetime

import pytest

from charging import create_app
from charging.db import SCHEMA_VERSION, connect, init_schema, retire_outdated_database
from charging.services import notifications
from charging.services.notifications import NotificationService, PushChannel, build_channel
from charging.services.outbox import OutboxDispatcher
from charging.services.plates import PlateError, normalize_plate
from charging.services.reservations import NotFoundError, ReservationError, ReservationService
from charging.services.vehicles import VehicleService
from conftest import FakeClock, make_user
from test_web import DAY, TOKEN, api_post, as_admin, as_ev, db_rows, login, set_csrf

AC01, HPC08 = 1, 8


class FakeNtfy:
    """Místo skutečného ntfy: zapamatuje si odeslané zprávy, umí i „spadnout“."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, url, text):
        if self.fail:
            raise TimeoutError("ntfy neodpovídá")
        self.sent.append((url, text))


@pytest.fixture
def ntfy():
    return FakeNtfy()


@pytest.fixture
def app(tmp_path, clock, ntfy):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "app.db"), "SECRET_KEY": "test-secret",
                       "CLOCK": clock, "NTFY_URL": "https://ntfy.example", "PUSH_SENDER": ntfy, "PUSH_BACKGROUND": False})


def at(hour, minute=0):
    return datetime(2026, 9, 15, hour, minute)


# ------------------------------------------------------------------ SPZ


@pytest.mark.parametrize("raw, country, expected", [
    ("1ab 2345", "CZ", "1AB 2345"),
    ("  1AB2345 ", "CZ", "1AB 2345"),
    ("5T2-1234", "CZ", "5T2 1234"),
    ("el 123ab", "CZ", "EL 123AB"),
    ("TESLA3", "CZ", "TESLA3"),
    ("ba-123xy", "SK", "BA-123XY"),
    ("m ab 1234", "D", "M AB 1234"),
])
def test_plate_valid_formats_are_normalized(raw, country, expected):
    assert normalize_plate(raw, country) == expected


@pytest.mark.parametrize("raw, country, message", [
    ("1DB 2345", "CZ", "Kraj „D“"),
    ("0AB 1234", "CZ", "nezačíná nulou"),
    ("TESLA", "CZ", "Neplatná SPZ"),       # na přání musí mít aspoň jednu číslici
    ("ÁBC 123", "CZ", "Neplatná SPZ"),
    ("1AB 2345 678", "CZ", "Neplatná SPZ"),
    ("", "CZ", "Vyplňte SPZ"),
    ("1AB 2345", "US", "sousedních států"),
    ("B@ 123", "SK", "jen písmena"),
])
def test_plate_invalid_formats_explain_why(raw, country, message):
    with pytest.raises(PlateError, match=message):
        normalize_plate(raw, country)


def test_reservation_keeps_plate_country(conn, clock):
    uid = make_user(conn, "ridic", "BEV")
    rid = ReservationService(conn, clock).create(uid, AC01, at(10), 1, "BEV", "ba-123xy", "SK")
    row = conn.execute("SELECT license_plate, plate_country FROM reservations WHERE id = ?", (rid,)).fetchone()
    assert tuple(row) == ("BA-123XY", "SK")


# ------------------------------------------------------------------ profil vozidel


def test_vehicles_first_is_default_and_default_moves_on_delete(conn):
    uid = make_user(conn, "ridic", "BEV")
    vehicles = VehicleService(conn)
    tesla = vehicles.add(uid, "Tesla Model 3", "BEV", "1ab 2345")
    octavia = vehicles.add(uid, "Octavia iV", "PHEV", "EL 123AB")
    assert [v["name"] for v in vehicles.list(uid)] == ["Tesla Model 3", "Octavia iV"]
    assert vehicles.default(uid)["id"] == tesla
    vehicles.set_default(uid, octavia)
    assert vehicles.default(uid)["id"] == octavia
    vehicles.delete(uid, octavia)
    assert vehicles.default(uid)["id"] == tesla


def test_vehicles_validate_input_and_ownership(conn):
    uid, other = make_user(conn, "ridic", "BEV"), make_user(conn, "cizi", "BEV")
    vehicles = VehicleService(conn)
    vid = vehicles.add(uid, "Tesla", "BEV", "1AB 2345")
    with pytest.raises(ReservationError, match="už máte uložené"):
        vehicles.add(uid, "Znovu", "BEV", "1ab2345")
    with pytest.raises(ReservationError, match="Pojmenujte"):
        vehicles.add(uid, "   ", "BEV", "2AB 2345")
    with pytest.raises(ReservationError, match="Kraj"):
        vehicles.add(uid, "Špatná SPZ", "BEV", "1DB 2345")
    with pytest.raises(NotFoundError):
        vehicles.get(other, vid)  # cizí vozidlo
    with pytest.raises(NotFoundError):
        vehicles.delete(other, vid)
    assert vehicles.add(other, "Tesla", "BEV", "1AB 2345")  # stejnou SPZ může mít jiný účet (třeba služební auto)


def test_web_profile_add_vehicle_and_reserve_with_it(client, app):
    as_ev(client)
    page = client.post("/profile/vehicles", data={"name": "Tesla Model 3", "vehicle_type": "BEV", "plate_country": "CZ",
                                                  "license_plate": "1ab2345", "csrf_token": TOKEN}, follow_redirects=True)
    assert "Tesla Model 3" in page.get_data(as_text=True) and "1AB 2345" in page.get_data(as_text=True)
    [vehicle] = db_rows(app, "SELECT id FROM vehicles")

    index = client.get("/").get_data(as_text=True)
    assert 'name="vehicle_id"' in index and "Tesla Model 3 · 1AB 2345" in index  # předvybrané vozidlo

    r = api_post(client, "/reservations", {"station_id": HPC08, "date": DAY, "time": "10:00", "slots": 2,
                                           "vehicle_id": vehicle["id"]})
    assert r.status_code == 201
    assert (r.get_json()["vehicle_type"], r.get_json()["license_plate"], r.get_json()["plate_country"]) == ("BEV", "1AB 2345", "CZ")

    form = client.post("/reservations", data={"station_id": AC01, "date": DAY, "time": "12:00", "slots": 1,
                                              "vehicle_id": vehicle["id"], "csrf_token": TOKEN})
    assert form.status_code == 302
    assert db_rows(app, "SELECT COUNT(*) FROM reservations WHERE license_plate = '1AB 2345'")[0][0] == 2


def test_web_invalid_plate_keeps_form_and_foreign_vehicle_is_404(client, app):
    as_ev(client)
    r = client.post("/profile/vehicles", data={"name": "Leaf", "vehicle_type": "BEV", "plate_country": "CZ",
                                               "license_plate": "1DB 2345", "csrf_token": TOKEN})
    assert r.status_code == 400
    page = r.get_data(as_text=True)
    assert "Kraj" in page and 'value="Leaf"' in page  # hláška a vyplněné údaje zůstanou
    login(client, "ridic_phev", "phev1234")
    client.post("/profile/vehicles", data={"name": "Outlander", "vehicle_type": "PHEV", "plate_country": "CZ",
                                           "license_plate": "2AB 3456", "csrf_token": TOKEN})
    [phev_vehicle] = db_rows(app, "SELECT id FROM vehicles")
    as_ev(client)
    r = api_post(client, "/reservations", {"station_id": AC01, "date": DAY, "time": "10:00", "slots": 1,
                                           "vehicle_id": phev_vehicle["id"]})
    assert r.status_code == 404
    assert client.post(f"/profile/vehicles/{phev_vehicle['id']}/delete", data={"csrf_token": TOKEN}).status_code == 404


def test_phev_vehicle_from_profile_still_cannot_reserve_hpc(client, app):
    login(client, "ridic_phev", "phev1234")
    client.post("/profile/vehicles", data={"name": "Outlander", "vehicle_type": "PHEV", "plate_country": "CZ",
                                           "license_plate": "2AB 3456", "csrf_token": TOKEN})
    [vehicle] = db_rows(app, "SELECT id FROM vehicles")
    r = api_post(client, "/reservations", {"station_id": HPC08, "date": DAY, "time": "10:00", "slots": 1,
                                           "vehicle_id": vehicle["id"]})
    assert r.status_code == 400 and "BEV" in r.get_json()["error"]  # BR-04 platí dál


# ------------------------------------------------------------------ notifikace na telefon


def pending_request(svc, users, start=at(10)):
    rid = svc.create(users["ev"], HPC08, start, 1, "BEV", "1AB 2345")
    svc.confirm(users["ev"], rid)
    return rid


@pytest.fixture
def users(conn):
    return {"ev": make_user(conn, "ev", "BEV"), "admin": make_user(conn, "admin", "BEV", is_admin=True)}


def dispatch(conn, sender):
    channel = build_channel(conn, FakeClock(at(8)), {"NTFY_URL": "https://ntfy.example", "PUSH_SENDER": sender,
                                                      "PUSH_BACKGROUND": False})
    return OutboxDispatcher(conn, NotificationService(conn, channel)).dispatch()


def test_push_goes_to_users_topic_without_license_plate(conn, users, ntfy):
    svc = ReservationService(conn, FakeClock(at(8)))
    rid = pending_request(svc, users)
    topic = notifications.enable_push(conn, users["ev"])
    svc.approve(users["admin"], rid)
    assert dispatch(conn, ntfy) == 1
    [(url, text)] = ntfy.sent
    assert url == f"https://ntfy.example/{topic}" and topic.startswith("voltbook-")
    assert f"#{rid}" in text and "schválena" in text and "SPZ" not in text and "1AB 2345" not in text
    assert any("SPZ 1AB 2345" in m["message"] for m in notifications.list_for_user(conn, users["ev"]))  # v aplikaci SPZ je


def test_push_failure_is_logged_and_in_app_message_still_delivered(conn, users, ntfy, caplog):
    svc = ReservationService(conn, FakeClock(at(8)))
    rid = pending_request(svc, users)
    notifications.enable_push(conn, users["ev"])
    svc.approve(users["admin"], rid)
    ntfy.fail = True
    with caplog.at_level(logging.WARNING, logger="charging.services.notifications"):
        assert dispatch(conn, ntfy) == 1  # událost je doručená do aplikace
    assert "TimeoutError" in caplog.text
    assert len(notifications.list_for_user(conn, users["ev"])) == 1
    assert conn.execute("SELECT sent_at FROM outbox").fetchone()[0] is not None


def test_ntfy_send_makes_real_http_post(tmp_path):
    """Skutečné HTTP volání proti lokálnímu serveru místo ntfy.sh: POST na /<téma>, text v UTF-8, titulek."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
            received.append((self.path, self.headers["Title"], body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    notifications.ntfy_send(f"http://127.0.0.1:{server.server_port}/voltbook-abc", "Žádost #3 byla schválena.")
    thread.join(5)
    server.server_close()
    assert received == [("/voltbook-abc", "VoltBook", "Žádost #3 byla schválena.")]


def test_no_push_without_topic_or_without_server(conn, users, ntfy):
    svc = ReservationService(conn, FakeClock(at(8)))
    rid = pending_request(svc, users)
    svc.approve(users["admin"], rid)
    assert dispatch(conn, ntfy) == 1 and ntfy.sent == []  # uživatel push nezapnul
    notifications.enable_push(conn, users["ev"])
    PushChannel(conn, "", ntfy, background=False).send(users["ev"], "zpráva")
    assert ntfy.sent == []  # server bez NTFY_URL
    notifications.disable_push(conn, users["ev"])
    assert notifications.get_push_topic(conn, users["ev"]) is None


def test_web_enable_push_test_message_and_approval_reaches_phone(client, app, ntfy):
    as_ev(client)
    client.post("/profile/push/enable", data={"csrf_token": TOKEN})
    [user] = db_rows(app, "SELECT push_topic FROM users WHERE username = 'ridic_ev'")
    page = client.get("/profile").get_data(as_text=True)
    assert user["push_topic"] in page and "Zapnuto" in page
    client.post("/profile/push/test", data={"csrf_token": TOKEN})
    assert ntfy.sent[-1] == (f"https://ntfy.example/{user['push_topic']}", "Zkušební notifikace z VoltBooku. "
                             "Takhle vám přijde výsledek schválení nebo zpráva o uvolněném stání.")

    rid = api_post(client, "/reservations", {"station_id": HPC08, "date": DAY, "time": "10:00", "slots": 2,
                                             "vehicle_type": "BEV", "license_plate": "1AB 2345"}).get_json()["id"]
    api_post(client, f"/reservations/{rid}/confirm")
    as_admin(client)
    assert api_post(client, f"/reservations/{rid}/approve").get_json()["state"] == "CONFIRMED"
    assert "schválena" in ntfy.sent[-1][1]  # doručeno po POST (ADR-01) i na telefon

    as_ev(client)
    client.post("/profile/push/disable", data={"csrf_token": TOKEN})
    assert db_rows(app, "SELECT push_topic FROM users WHERE username = 'ridic_ev'")[0][0] is None


def test_web_push_disabled_on_server(tmp_path, clock):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "off.db"), "SECRET_KEY": "x", "CLOCK": clock, "NTFY_URL": ""})
    client = app.test_client()
    as_ev(client)
    page = client.post("/profile/push/enable", data={"csrf_token": TOKEN}, follow_redirects=True).get_data(as_text=True)
    assert "nejsou na tomto serveru zapnuté" in page
    assert db_rows(app, "SELECT push_topic FROM users WHERE username = 'ridic_ev'")[0][0] is None


def test_profile_requires_login(client):
    set_csrf(client)
    assert client.get("/profile").status_code == 302


# ------------------------------------------------------------------ migrace DB


def test_migration_from_v5_keeps_data(tmp_path, clock):
    path = str(tmp_path / "old.db")
    conn = connect(path)
    init_schema(conn, seed_demo_users=False)
    uid = make_user(conn, "stary", "BEV")
    rid = ReservationService(conn, clock).create(uid, AC01, at(10), 1, "BEV", "1AB 2345")
    # DB vrátíme do tvaru verze 5 (před vylepšením)
    conn.executescript("""
        DROP INDEX idx_users_push_topic; DROP TABLE vehicles;
        ALTER TABLE users DROP COLUMN push_topic; ALTER TABLE reservations DROP COLUMN plate_country;
        PRAGMA user_version = 5;""")
    conn.close()

    assert retire_outdated_database(path) is None  # v5 se nezálohuje, ale převede
    conn = connect(path)
    init_schema(conn, seed_demo_users=False)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
    row = conn.execute("SELECT id, license_plate, plate_country FROM reservations").fetchone()
    assert tuple(row) == (rid, "1AB 2345", "CZ")
    assert VehicleService(conn).add(uid, "Tesla", "BEV", "1AB 2345")
    assert notifications.get_push_topic(conn, uid) is None
    conn.close()
