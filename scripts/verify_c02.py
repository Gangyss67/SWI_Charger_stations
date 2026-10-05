"""C02 – ověření změněných příkladů chování (specifikace baseline v0.2) přes HTTP rozhraní aplikace.

Skript založí čistou DB v instance/verify_c02.db, aplikaci spustí s řiditelnými hodinami
("teď" = 15.09.2026 08:00, aby šly ověřit i lhůty 5 min / T_start − 15 min) a příklady
provede přes stejné HTTP endpointy jako prohlížeč (přihlášení, CSRF token, JSON).

Vozidla z katalogu: Škoda Enyaq 85 (135 kW DC), VW e-up! (40 kW), Mitsubishi Outlander PHEV
(22 kW), Toyota RAV4 Plug-in (jen AC). Rychlá stanice připustí automaticky vozidlo od poloviny
svého výkonu (HPC 150 kW -> 75 kW, DC 07 50 kW -> 25 kW), slabší jen jako výjimku (BR-06).

Spuštění:  python scripts/verify_c02.py      exit code 0 = všechny příklady odpovídají specifikaci
"""
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from charging import create_app  # noqa: E402

DAY = "2026-09-15"
PLATE = "1AB 2345"
ENYAQ, E_UP, OUTLANDER, RAV4 = 1, 10, 14, 15
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class Clock:
    def __init__(self, now: datetime):
        self.value = now

    def __call__(self) -> datetime:
        return self.value

    def set(self, hour: int, minute: int) -> None:
        self.value = datetime(2026, 9, 15, hour, minute)


class Session:
    """Přihlášený klient: stejné cookies a CSRF token jako v prohlížeči."""

    def __init__(self, app, username: str, password: str):
        self.client = app.test_client()
        token = self._token("/login")
        self.client.post("/login", data={"username": username, "password": password, "csrf_token": token})
        self.token = self._token("/")  # přihlášení obnovuje session -> nový token

    def _token(self, url: str) -> str:
        return _CSRF_RE.search(self.client.get(url).get_data(as_text=True)).group(1)

    def post(self, url: str, payload: dict | None = None):
        return self.client.post(url, json=payload or {}, headers={"X-CSRF-Token": self.token})

    def create_raw(self, station_id: int, time: str, slots: int, vehicle_model_id: int):
        return self.post("/reservations", {"station_id": station_id, "date": DAY, "time": time, "slots": slots,
                                           "vehicle_model_id": vehicle_model_id, "license_plate": PLATE})

    def create(self, station_id: int, time: str, slots: int, vehicle_model_id: int) -> int:
        r = self.create_raw(station_id, time, slots, vehicle_model_id)
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]

    def state(self, reservation_id: int) -> str:
        return self.client.get(f"/api/reservations/{reservation_id}").get_json()["state"]

    def notifications(self) -> str:
        return self.client.get("/notifications").get_data(as_text=True)


def register(app, username: str, password: str, vehicle_model_id: int) -> None:
    client = app.test_client()
    token = _CSRF_RE.search(client.get("/register").get_data(as_text=True)).group(1)
    client.post("/register", data={"username": username, "password": password, "password_again": password,
                                   "vehicle_model_id": vehicle_model_id, "csrf_token": token})


def availability(app, station_id: int, time: str, slots: int, vehicle_model_id: int) -> dict:
    url = (f"/api/availability?station_id={station_id}&date={DAY}&time={time}&slots={slots}"
           f"&vehicle_model_id={vehicle_model_id}")
    return app.test_client().get(url).get_json()["stations"][0]


def verdict(st: dict) -> str:
    if not st["available"]:
        return f"UNAVAILABLE ({st['reason']})"
    return f"AVAILABLE ({st['admission']})"


def outcome(response) -> str:
    body = response.get_json()
    if response.status_code in (200, 201):
        return body["state"]
    return f"{response.status_code} {body['error']}"


def main() -> int:
    db_path = ROOT / "instance" / "verify_c02.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.unlink(missing_ok=True)
    clock = Clock(datetime(2026, 9, 15, 8, 0))
    app = create_app({"DATABASE": str(db_path), "CLOCK": clock, "SECRET_KEY": "verify-c02"})
    register(app, "ridic_ev2", "ev2heslo1", ENYAQ)
    register(app, "ridic_rav4", "rav4heslo", RAV4)
    ev, ev2 = Session(app, "ridic_ev", "ev123456"), Session(app, "ridic_ev2", "ev2heslo1")
    mesto, phev = Session(app, "ridic_mesto", "mesto1234"), Session(app, "ridic_phev", "phev1234")
    rav4, admin = Session(app, "ridic_rav4", "rav4heslo"), Session(app, "admin", "admin1234")
    results = []

    def example(code: str, description: str, expected: str, actual: str, ok: bool | None = None) -> None:
        passed = (expected in actual) if ok is None else ok
        results.append(passed)
        print(f"[{'OK  ' if passed else 'FAIL'}] {code} {description}\n        očekáváno: {expected}\n        skutečně:  {actual}")

    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    v1 = ev.create(8, "10:00", 2, ENYAQ)
    example("V1", f"OP-03 Enyaq (135 kW) potvrdí HPC 08 [10:00, 11:00) (#{v1})", "CONFIRMED",
            outcome(ev.post(f"/reservations/{v1}/confirm")))

    v2 = mesto.create(9, "10:00", 2, E_UP)
    r = mesto.post(f"/reservations/{v2}/confirm")
    example("V2", f"OP-03 e-up! (40 kW < 75 kW) potvrdí HPC 09 [10:00, 11:00) (#{v2})", "PENDING_APPROVAL",
            f"{outcome(r)}, rozhodnout do {r.get_json().get('approval_deadline')}")
    example("V2b", "OP-01 Enyaq na HPC 09 [10:30, 11:30)", "UNAVAILABLE (PENDING_APPROVAL)",
            verdict(availability(app, 9, "10:30", 2, ENYAQ)))
    other = ev2.create(9, "10:30", 2, ENYAQ)
    example("V2c", f"OP-03 kolidující návrh jiného uživatele na HPC 09 (#{other})", "409",
            outcome(ev2.post(f"/reservations/{other}/confirm")) + f" -> stav {ev2.state(other)}")

    v3 = phev.create(7, "12:00", 2, OUTLANDER)
    example("V3", f"OP-03 Outlander PHEV (22 kW < 25 kW) potvrdí DC 07 [12:00, 13:00) (#{v3})", "PENDING_APPROVAL",
            outcome(phev.post(f"/reservations/{v3}/confirm")))
    example("V3b", "OP-01 RAV4 Plug-in (jen AC) na DC 07 / HPC 10 / AC 01",
            "UNAVAILABLE (NO_DC) / UNAVAILABLE (VEHICLE) / AVAILABLE (AUTO)",
            " / ".join(verdict(availability(app, s, "14:00", 1, RAV4)) for s in (7, 10, 1)))
    example("V3c", "OP-02 RAV4 Plug-in vytvoří návrh na DC 07", "400",
            outcome(rav4.create_raw(7, "14:00", 1, RAV4)))

    clock.set(8, 30)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    example("V5", f"OP-05 admin zamítne výjimku DC 07 (#{v3})", "REJECTED", outcome(admin.post(f"/reservations/{v3}/reject")))
    example("V5b", "OP-01 DC 07 [12:00, 13:00) po zamítnutí pro Enyaq", "AVAILABLE",
            verdict(availability(app, 7, "12:00", 2, ENYAQ)))
    example("V5c", "Notification Service: ridic_phev dostal výsledek", "zamítnuta administrátorem",
            "zamítnuta administrátorem" if "zamítnuta administrátorem" in phev.notifications() else "žádná zpráva")
    r = mesto.post(f"/reservations/{v2}/approve")
    example("V9", f"OP-05 ne-admin (ridic_mesto) schvaluje #{v2}", "403", f"{outcome(r)} -> stav {mesto.state(v2)}")

    v6 = mesto.create(10, "10:00", 1, E_UP)
    mesto.post(f"/reservations/{v6}/confirm")
    v7 = mesto.create(10, "11:00", 1, E_UP)
    mesto.post(f"/reservations/{v7}/confirm")
    print(f"        (ridic_mesto: výjimky #{v6} HPC 10 [10:00, 10:30) a #{v7} HPC 10 [11:00, 11:30) -> "
          f"{mesto.state(v6)}, {mesto.state(v7)})")

    clock.set(9, 0)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    example("V4", f"OP-05 admin schválí výjimku #{v2} včas (lhůta 09:45)", "CONFIRMED",
            outcome(admin.post(f"/reservations/{v2}/approve")))
    example("V4b", "Notification Service: ridic_mesto dostal výsledek", "byla schválena",
            "byla schválena" if "byla schválena" in mesto.notifications() else "žádná zpráva")

    clock.set(9, 40)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    example("V7", f"OP-04 ridic_mesto stáhne žádost #{v7} (začátek 11:00)", "CANCELLED",
            outcome(mesto.post(f"/reservations/{v7}/cancel")))

    clock.set(9, 46)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    st = availability(app, 10, "10:00", 1, ENYAQ)
    example("V6", f"BR-06 o #{v6} nikdo nerozhodl do 09:45 -> OP-01 HPC 10 [10:00, 10:30)", "AVAILABLE",
            f"{verdict(st)}, stav #{v6} = {mesto.state(v6)}", ok=st["available"] and mesto.state(v6) == "EXPIRED")
    example("V6b", f"OP-05 admin schvaluje propadlou žádost #{v6}", "409",
            outcome(admin.post(f"/reservations/{v6}/approve")))
    v8 = mesto.create(10, "10:00", 1, E_UP)
    example("V8", f"OP-03 e-up! potvrdí HPC 10 [10:00, 10:30) v 09:46 (#{v8})", "409",
            outcome(mesto.post(f"/reservations/{v8}/confirm")) + f" -> stav {mesto.state(v8)}")
    v8b = ev2.create(10, "10:00", 1, ENYAQ)
    example("V8b", f"OP-03 Enyaq potvrdí totéž v 09:46 (#{v8b}), výjimka není potřeba", "CONFIRMED",
            outcome(ev2.post(f"/reservations/{v8b}/confirm")))

    passed = sum(results)
    print(f"== {passed}/{len(results)} příkladů odpovídá specifikaci baseline v0.2")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
