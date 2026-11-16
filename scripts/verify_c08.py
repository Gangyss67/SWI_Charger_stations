"""C08 – krokované test casy TC-C08-01 a TC-C08-02 (BR-02, OP-03 / OP-05) přes HTTP rozhraní,
od C09 i TC-C08-03 pro změnu CR-08 (zpráva žadateli o propadlé žádosti).

Každý běh si založí novou izolovanou SQLite DB v dočasném adresáři (= reset), aplikaci spustí
s řiditelnými hodinami ("teď" = 15.09.2026 08:00) a operace volá přes stejné endpointy jako
prohlížeč (přihlášení, CSRF, JSON). Stav po každém kroku čte zvlášť – novým spojením přímo do DB,
ne z odpovědi aplikace. Každý test case běží dvakrát (krok 5: reset a zopakování).

Spuštění:  python scripts/verify_c08.py      exit code 0 = všechny kroky odpovídají očekávání
"""
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from verify_c02 import Clock, Session, register  # stejné přihlášení a CSRF jako v C02

from charging import create_app

HPC08, HPC09 = 8, 9
BLOCKING = "('CONFIRMED', 'PENDING_APPROVAL')"


class Env:
    """Čistá DB + aplikace + přihlášení uživatelé (krok 1 „reset“)."""

    def __init__(self, workdir: Path, name: str):
        self.db_path = workdir / f"{name}.db"
        self.clock = Clock(datetime(2026, 9, 15, 8, 0))
        self.app = create_app({"DATABASE": str(self.db_path), "CLOCK": self.clock, "SECRET_KEY": "verify-c08"})
        register(self.app, "ridic_ev2", "ev2heslo1", "BEV")
        self.ev, self.ev2 = Session(self.app, "ridic_ev", "ev123456"), Session(self.app, "ridic_ev2", "ev2heslo1")
        self.admin = Session(self.app, "admin", "admin1234")

    def read(self, sql: str, params=()) -> list:
        """Nezávislé čtení: nové spojení, nic z aplikace."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def station(self, station_id: int) -> list:
        return self.read("SELECT id, start_time, end_time, state FROM reservations WHERE station_id = ? ORDER BY start_time",
                         (station_id,))

    def overlaps(self) -> int:
        """BR-02 oracle: dvojice rezervací v blokujícím stavu na stejné stanici, které se překrývají."""
        return self.read(f"""
            SELECT COUNT(*) FROM reservations a JOIN reservations b
              ON a.id < b.id AND a.station_id = b.station_id
             AND a.start_time < b.end_time AND b.start_time < a.end_time
            WHERE a.state IN {BLOCKING} AND b.state IN {BLOCKING}""")[0][0]


def fmt(rows) -> str:
    return ", ".join(f"#{r[0]} [{r[1][11:]}, {r[2][11:]}) {r[3]}" for r in rows) or "nic"


class Report:
    def __init__(self):
        self.results = []

    def step(self, no, action: str, expected: str, actual: str, ok: bool) -> None:
        self.results.append(ok)
        print(f"  {no}. {action}\n     očekáváno: {expected}\n     skutečně:  {actual}  [{'OK' if ok else 'FAIL'}]")


def tc01(workdir: Path, run: int, rep: Report) -> str:
    """TC-C08-01 – pozitivní: navazující rezervace na HPC 08, schválení B2 adminem projde."""
    env = Env(workdir, f"tc01_{run}")
    b1 = env.ev.create(HPC08, "10:00", 2)
    env.ev.post(f"/reservations/{b1}/confirm")
    env.admin.post(f"/reservations/{b1}/approve")
    b2 = env.ev2.create(HPC08, "11:00", 2)
    env.ev2.post(f"/reservations/{b2}/confirm")
    s1, s2 = env.ev.state(b1), env.ev2.state(b2)
    rep.step(1, f"reset + příprava B1 #{b1} HPC 08 [10:00, 11:00), B2 #{b2} [11:00, 12:00)",
             "B1 CONFIRMED, B2 PENDING_APPROVAL", f"B1 {s1}, B2 {s2}", (s1, s2) == ("CONFIRMED", "PENDING_APPROVAL"))
    rows = env.station(HPC08)
    rep.step(2, "nezávislé čtení z DB", "[10:00, 11:00) CONFIRMED, [11:00, 12:00) PENDING_APPROVAL, nic jiného",
             fmt(rows), [r[1:] for r in rows] == [("2026-09-15 10:00", "2026-09-15 11:00", "CONFIRMED"),
                                                  ("2026-09-15 11:00", "2026-09-15 12:00", "PENDING_APPROVAL")])
    env.clock.value = datetime(2026, 9, 15, 9, 0)
    r = env.admin.post(f"/reservations/{b2}/approve")
    got = f"{r.status_code} {r.get_json().get('state', r.get_json().get('error'))}"
    rep.step(3, f"09:00 admin POST /reservations/{b2}/approve", "200 CONFIRMED", got, got == "200 CONFIRMED")
    rows, n = env.station(HPC08), env.overlaps()
    rep.step(4, "nezávislé čtení po commitu", "obě CONFIRMED, 0 překryvů v blokujícím stavu",
             f"{fmt(rows)}; překryvů {n}", [r[3] for r in rows] == ["CONFIRMED", "CONFIRMED"] and n == 0)
    return f"{s1}/{s2} -> {got}, překryvů {n}"


def tc02(workdir: Path, run: int, rep: Report) -> str:
    """TC-C08-02 – negativní: potvrzení návrhu, který se překrývá s CONFIRMED, se odmítne."""
    env = Env(workdir, f"tc02_{run}")
    b1 = env.ev.create(HPC08, "10:00", 2)
    env.ev.post(f"/reservations/{b1}/confirm")
    env.admin.post(f"/reservations/{b1}/approve")
    b2 = env.ev2.create(HPC08, "10:30", 2)
    s1, s2 = env.ev.state(b1), env.ev2.state(b2)
    rep.step(1, f"reset + příprava B1 #{b1} HPC 08 [10:00, 11:00), B2 #{b2} [10:30, 11:30)",
             "jen B1 CONFIRMED, B2 DRAFT", f"B1 {s1}, B2 {s2}", (s1, s2) == ("CONFIRMED", "DRAFT"))
    before = env.read("SELECT updated_at FROM reservations WHERE id = ?", (b1,))[0][0]
    rows = env.station(HPC08)
    rep.step(2, "nezávislé čtení z DB", "časový překryv existuje, ale blokující je jen B1",
             f"{fmt(rows)}; překryvů v blokujícím stavu {env.overlaps()}",
             [r[3] for r in rows] == ["CONFIRMED", "DRAFT"] and env.overlaps() == 0)
    r = env.ev2.post(f"/reservations/{b2}/confirm")
    got = f"{r.status_code} {r.get_json().get('state', r.get_json().get('error'))}"
    rep.step(3, f"ridic_ev2 POST /reservations/{b2}/confirm", "409 (kolize podle BR-02)", got, r.status_code == 409)
    rows = env.station(HPC08)
    after = env.read("SELECT updated_at FROM reservations WHERE id = ?", (b1,))[0][0]
    events = env.read("SELECT COUNT(*) FROM outbox WHERE payload LIKE ?", (f'%"id": {b2},%',))[0][0]
    rep.step(4, "nezávislé čtení po rollbacku", "B1 beze změny CONFIRMED, B2 pořád DRAFT, 0 překryvů, žádná událost pro B2",
             f"{fmt(rows)}; B1 updated_at {'beze změny' if after == before else 'ZMĚNĚNO'}; "
             f"překryvů {env.overlaps()}; událostí pro B2 {events}",
             [r[3] for r in rows] == ["CONFIRMED", "DRAFT"] and after == before and env.overlaps() == 0 and events == 0)
    return f"{s1}/{s2} -> {r.status_code}, B2 {rows[1][3]}"


def tc03(workdir: Path, run: int, rep: Report) -> str:
    """TC-C08-03 (C09) – nové chování CR-08: propadlá žádost -> žadatel dostane právě jednu zprávu."""
    def request_hpc09(env: Env) -> int:
        rid = env.ev2.create(HPC09, "10:00", 1)
        env.ev2.post(f"/reservations/{rid}/confirm")
        return rid

    def expiry_events(env: Env) -> list:
        return env.read("SELECT sent_at FROM outbox WHERE event = 'reservation_expired'")

    env = Env(workdir, f"tc03a_{run}")
    b2 = request_hpc09(env)
    row = env.read("SELECT state, approval_deadline FROM reservations WHERE id = ?", (b2,))[0]
    rep.step(1, f"reset + ridic_ev2 potvrdí HPC 09 [10:00, 10:30) (#{b2})", "PENDING_APPROVAL, lhůta 09:45:00",
             f"{row[0]}, lhůta {row[1][11:]}", tuple(row) == ("PENDING_APPROVAL", "2026-09-15 09:45:00"))
    env.clock.value = datetime(2026, 9, 15, 9, 45, 0)
    r = env.admin.post(f"/reservations/{b2}/approve")
    st, events = env.read("SELECT state FROM reservations WHERE id = ?", (b2,))[0][0], expiry_events(env)
    rep.step(2, "09:45:00 admin schválí (AC-03 hranice), pak čtení z DB", "200, B2 CONFIRMED, 0 událostí reservation_expired",
             f"{r.status_code}, B2 {st}, událostí {len(events)}", (r.status_code, st, len(events)) == (200, "CONFIRMED", 0))

    env = Env(workdir, f"tc03b_{run}")  # krok 3: reset na stejný výchozí stav
    b2 = request_hpc09(env)
    env.clock.value = datetime(2026, 9, 15, 9, 45, 1)
    env.ev2.client.get("/reservations")  # běžný request, propadnutí je líné (D7)
    st, events = env.read("SELECT state FROM reservations WHERE id = ?", (b2,))[0][0], expiry_events(env)
    rep.step(3, "reset; 09:45:01 ridic_ev2 GET /reservations, pak čtení z DB",
             "B2 EXPIRED, 1 událost reservation_expired (zatím nedoručená)",
             f"B2 {st}, událostí {len(events)}, doručeno {sum(1 for e in events if e[0])}",
             st == "EXPIRED" and len(events) == 1 and events[0][0] is None)
    env.clock.value = datetime(2026, 9, 15, 9, 50)
    r = env.admin.post(f"/reservations/{b2}/approve")
    st, events = env.read("SELECT state FROM reservations WHERE id = ?", (b2,))[0][0], expiry_events(env)
    confirmed = env.read("SELECT COUNT(*) FROM reservations WHERE station_id = ? AND state = 'CONFIRMED'", (HPC09,))[0][0]
    rep.step(4, "09:50 admin schvaluje pozdě, pak čtení z DB",
             "409, B2 EXPIRED, žádná CONFIRMED na HPC 09, událost doručená (dispatch po POST), 0 překryvů",
             f"{r.status_code}, B2 {st}, CONFIRMED na HPC 09: {confirmed}, událostí {len(events)} "
             f"(doručeno {sum(1 for e in events if e[0])}), překryvů {env.overlaps()}",
             (r.status_code, st, confirmed, len(events)) == (409, "EXPIRED", 0, 1) and events[0][0] is not None
             and env.overlaps() == 0)
    page = env.ev2.notifications()
    stored = env.read("SELECT message FROM notifications n JOIN users u ON u.id = n.user_id WHERE u.username = 'ridic_ev2'")
    expired = [m[0] for m in stored if "propadla" in m[0]]
    approved = [m[0] for m in stored if "schválena" in m[0]]
    rep.step(5, "ridic_ev2 GET /notifications + čtení tabulky notifications",
             f"právě 1 zpráva o propadnutí #{b2}, žádná o schválení, zpráva je vidět na stránce",
             f"o propadnutí {len(expired)}, o schválení {len(approved)}, na stránce {'ano' if 'propadla' in page else 'ne'}",
             len(expired) == 1 and f"#{b2}" in expired[0] and not approved and "propadla" in page)
    return f"{r.status_code}, B2 {st}, zpráv {len(expired)}"


def main() -> int:
    rep = Report()
    with tempfile.TemporaryDirectory() as tmp:
        for name, tc in (("TC-C08-01 navazující rezervace, OP-05 Approve", tc01),
                         ("TC-C08-02 kolize při OP-03 Confirm", tc02),
                         ("TC-C08-03 CR-08 zpráva o propadlé žádosti (C09)", tc03)):
            outcomes = []
            for run in (1, 2):
                print(f"== {name} – běh {run}")
                outcomes.append(tc(Path(tmp), run, rep))
            rep.step("R", "reset a zopakování", "stejný výsledek v obou bězích", " | ".join(outcomes),
                     outcomes[0] == outcomes[1])
    passed = sum(rep.results)
    print(f"== {passed}/{len(rep.results)} kroků odpovídá očekávání")
    return 0 if passed == len(rep.results) else 1


if __name__ == "__main__":
    sys.exit(main())
