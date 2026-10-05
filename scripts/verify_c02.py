"""C02 – ověření změněných příkladů chování (specifikace baseline v0.2) přes HTTP rozhraní aplikace.

Skript založí čistou DB v instance/verify_c02.db, aplikaci spustí s řiditelnými hodinami
("teď" = 15.09.2026 08:00, aby šly ověřit i lhůty 5 min / T_start − 15 min) a příklady
provede přes stejné HTTP endpointy jako prohlížeč (přihlášení, CSRF token, JSON).

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

    def create(self, station_id: int, time: str, slots: int, vehicle_type: str = "BEV") -> int:
        r = self.post("/reservations", {"station_id": station_id, "date": DAY, "time": time, "slots": slots,
                                        "vehicle_type": vehicle_type, "license_plate": PLATE})
        assert r.status_code == 201, r.get_json()
        return r.get_json()["id"]

    def state(self, reservation_id: int) -> str:
        return self.client.get(f"/api/reservations/{reservation_id}").get_json()["state"]

    def notifications(self) -> str:
        return self.client.get("/notifications").get_data(as_text=True)


def register(app, username: str, password: str, vehicle_type: str) -> None:
    client = app.test_client()
    token = _CSRF_RE.search(client.get("/register").get_data(as_text=True)).group(1)
    client.post("/register", data={"username": username, "password": password, "password_again": password,
                                   "vehicle_type": vehicle_type, "csrf_token": token})


def availability(app, station_id: int, time: str, slots: int, vehicle_type: str = "BEV") -> dict:
    url = f"/api/availability?station_id={station_id}&date={DAY}&time={time}&slots={slots}&vehicle_type={vehicle_type}"
    return app.test_client().get(url).get_json()["stations"][0]


def outcome(response) -> str:
    body = response.get_json()
    return body["state"] if response.status_code == 200 else f"{response.status_code} {body['error']}"


def main() -> int:
    db_path = ROOT / "instance" / "verify_c02.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.unlink(missing_ok=True)
    clock = Clock(datetime(2026, 9, 15, 8, 0))
    app = create_app({"DATABASE": str(db_path), "CLOCK": clock, "SECRET_KEY": "verify-c02"})
    register(app, "ridic_ev2", "ev2heslo1", "BEV")
    ev, ev2 = Session(app, "ridic_ev", "ev123456"), Session(app, "ridic_ev2", "ev2heslo1")
    phev, admin = Session(app, "ridic_phev", "phev1234"), Session(app, "admin", "admin1234")
    results = []

    def example(code: str, description: str, expected: str, actual: str, ok: bool | None = None) -> None:
        passed = (expected in actual) if ok is None else ok
        results.append(passed)
        print(f"[{'OK  ' if passed else 'FAIL'}] {code} {description}\n        očekáváno: {expected}\n        skutečně:  {actual}")

    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    v1 = ev.create(8, "10:00", 2)
    r = ev.post(f"/reservations/{v1}/confirm")
    example("V1", f"OP-03 ridic_ev potvrdí HPC 08 [10:00, 11:00) (#{v1})", "PENDING_APPROVAL",
            f"{outcome(r)}, rozhodnout do {r.get_json().get('approval_deadline')}")

    st = availability(app, 8, "10:30", 2)
    example("V2", "OP-01 dotaz BEV na HPC 08 [10:30, 11:30)", "UNAVAILABLE (PENDING_APPROVAL)",
            f"{'AVAILABLE' if st['available'] else 'UNAVAILABLE'} ({st['reason']})")
    other = ev2.create(8, "10:30", 2)
    example("V2b", f"OP-03 ridic_ev2 potvrdí kolidující návrh HPC 08 [10:30, 11:30) (#{other})", "409",
            outcome(ev2.post(f"/reservations/{other}/confirm")) + f" -> stav {ev2.state(other)}")

    v3 = phev.create(1, "10:00", 1, "PHEV")
    example("V3", f"OP-03 ridic_phev potvrdí AC 01 [10:00, 10:30) (#{v3}) – bez schválení", "CONFIRMED",
            outcome(phev.post(f"/reservations/{v3}/confirm")))

    clock.set(8, 30)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    v5 = ev2.create(7, "12:00", 2)
    ev2.post(f"/reservations/{v5}/confirm")
    example("V5", f"OP-05 admin zamítne žádost DC 07 [12:00, 13:00) (#{v5})", "REJECTED",
            outcome(admin.post(f"/reservations/{v5}/reject")))
    st = availability(app, 7, "12:00", 2)
    example("V5b", "OP-01 dotaz na DC 07 [12:00, 13:00) po zamítnutí", "AVAILABLE",
            f"{'AVAILABLE' if st['available'] else 'UNAVAILABLE'} – {st['message']}")
    example("V5c", "Notification Service: ridic_ev2 dostal výsledek", "zamítnuta administrátorem",
            "zamítnuta administrátorem" if "zamítnuta administrátorem" in ev2.notifications() else "žádná zpráva")
    r = ev.post(f"/reservations/{v1}/approve")
    example("V9", f"OP-05 ne-admin (ridic_ev) schvaluje #{v1}", "403", f"{outcome(r)} -> stav {ev.state(v1)}")

    v6 = ev2.create(9, "10:00", 1)
    ev2.post(f"/reservations/{v6}/confirm")
    v7 = ev2.create(10, "10:00", 1)
    ev2.post(f"/reservations/{v7}/confirm")
    print(f"        (ridic_ev2: žádosti #{v6} HPC 09 a #{v7} HPC 10 [10:00, 10:30) -> {ev2.state(v6)}, {ev2.state(v7)})")

    clock.set(9, 0)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    example("V4", f"OP-05 admin schválí #{v1} včas (lhůta 09:45)", "CONFIRMED",
            outcome(admin.post(f"/reservations/{v1}/approve")))
    example("V4b", "Notification Service: ridic_ev dostal výsledek", "byla schválena",
            "byla schválena" if "byla schválena" in ev.notifications() else "žádná zpráva")

    clock.set(9, 40)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    example("V7", f"OP-04 ridic_ev2 stáhne žádost #{v7} 20 min před začátkem", "CANCELLED",
            outcome(ev2.post(f"/reservations/{v7}/cancel")))

    clock.set(9, 46)
    print(f"== teď {clock.value:%d.%m.%Y %H:%M}")
    st = availability(app, 9, "10:00", 1)
    example("V6", f"BR-06 o #{v6} nikdo nerozhodl do 09:45 -> OP-01 HPC 09 [10:00, 10:30)", "AVAILABLE",
            f"{'AVAILABLE' if st['available'] else 'UNAVAILABLE'}, stav #{v6} = {ev2.state(v6)}",
            ok=st["available"] and ev2.state(v6) == "EXPIRED")
    example("V6b", f"OP-05 admin schvaluje propadlou žádost #{v6}", "409",
            outcome(admin.post(f"/reservations/{v6}/approve")))
    v8 = ev2.create(7, "10:00", 1)
    r = ev2.post(f"/reservations/{v8}/confirm")
    example("V8", f"OP-03 potvrzení DC 07 [10:00, 10:30) v 09:46 (#{v8})", "409",
            f"{outcome(r)} -> stav {ev2.state(v8)}")

    passed = sum(results)
    print(f"== {passed}/{len(results)} příkladů odpovídá specifikaci baseline v0.2")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
