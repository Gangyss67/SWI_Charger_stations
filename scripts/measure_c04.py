"""C04 – měření kvality: souběžné potvrzování rezervací (future pressure Q, driver DR-1).

Kritérium (docs/evidence-and-evolution.md, C04) – při 50 souběžných klientech, kteří ve stejné chvíli
vytvoří a potvrdí (OP-02, OP-03) kolidující návrhy na 10 stanicích:
  K1  žádné dvě rezervace v blokujícím stavu se nepřekrývají (BR-02) a o každé stanici v kole
      vyhraje právě jedno potvrzení, ostatní dostanou 409,
  K2  žádná odpověď 5xx, žádné vypršení spojení (limit 10 s) a žádná výjimka v logu serveru,
  K3  p95 doby odpovědi OP-03 (POST /reservations/<id>/confirm) <= 500 ms.

Skript spustí aplikaci stejně jako `python run.py` (samostatný proces, vývojový server Werkzeug,
vlákno na request, SQLite) nad čistou DB v dočasném adresáři a zátěž posílá přes HTTP z vláken.
Úrovně: 5 klientů (odhad dnešní špičky), 50 (10×, kritérium) a 100 (20×, kolik máme rezervy).

Spuštění:  python scripts/measure_c04.py      exit code 0 = kritérium při 50 klientech splněno
"""
import http.cookiejar
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from werkzeug.security import generate_password_hash  # noqa: E402

from charging.db import connect, init_schema  # noqa: E402

LEVELS = (5, 50, 100)
CRITERION_CLIENTS = 50
ROUNDS = 6
P95_LIMIT_MS = 500
TIMEOUT_S = 10
STATIONS = list(range(1, 11))  # AC 01–06 -> CONFIRMED, DC 07 a HPC 08–10 -> PENDING_APPROVAL
PASSWORD = "mereni-c04"
PLATE = "1AB 2345"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class Client:
    """Jeden přihlášený řidič se svou session (cookies + CSRF token), jako prohlížeč."""

    def __init__(self, base: str, username: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        form = {"username": username, "password": PASSWORD, "csrf_token": self._token("/login")}
        self.opener.open(self.base + "/login", urllib.parse.urlencode(form).encode(), timeout=TIMEOUT_S).read()
        self.token = self._token("/")  # přihlášení obnovuje session -> nový token

    def _token(self, path: str) -> str:
        html = self.opener.open(self.base + path, timeout=TIMEOUT_S).read().decode()
        return _CSRF_RE.search(html).group(1)

    def post(self, path: str, payload: dict | None = None):
        """Vrací (HTTP status nebo None při chybě spojení, JSON odpověď, doba odpovědi v ms)."""
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.token},
        )
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=TIMEOUT_S) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except OSError as exc:  # vypršelo spojení / server neodpověděl
            return None, {"error": str(exc)}, (time.perf_counter() - started) * 1000
        elapsed = (time.perf_counter() - started) * 1000
        try:
            return status, json.loads(body), elapsed
        except ValueError:
            return status, {"error": body[:200].decode(errors="replace")}, elapsed


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare_database(path: Path, users: int) -> None:
    conn = connect(str(path))
    try:
        init_schema(conn)
        # Rychlý hash jen pro testovací účty, aby přihlášení 100 klientů netrvalo minutu.
        # Měří se až OP-02 a OP-03, přihlášení do výsledků nevstupuje.
        password_hash = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.executemany(
            "INSERT INTO users (username, password_hash, vehicle_type, is_admin, created_at) VALUES (?, ?, 'BEV', 0, ?)",
            [(f"mereni{i:03d}", password_hash, now) for i in range(users)],
        )
    finally:
        conn.close()


def start_server(db_path: Path, log_path: Path) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = dict(os.environ, DATABASE_PATH=str(db_path), PORT=str(port), SECRET_KEY="measure-c04", FLASK_DEBUG="0")
    log = open(log_path, "w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=ROOT, env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/login", timeout=1).read()
            return server, base
        except OSError:
            time.sleep(0.2)
    server.terminate()
    raise RuntimeError(f"Server se nespustil, viz {log_path}")


def run_round(clients: list[Client], day: date, start: str) -> list[dict]:
    """Všichni klienti naráz vytvoří návrh (klient i -> stanice i % 10) a pak ho naráz potvrdí."""
    created, confirm = threading.Barrier(len(clients)), threading.Barrier(len(clients))
    results: list[dict] = [{} for _ in clients]

    def worker(i: int, client: Client) -> None:
        station = STATIONS[i % len(STATIONS)]
        result = results[i]
        result["station"] = station
        created.wait(timeout=60)
        status, body, ms = client.post("/reservations", {"station_id": station, "date": day.isoformat(),
                                                        "time": start, "slots": 1, "vehicle_type": "BEV",
                                                        "license_plate": PLATE})
        result["create"] = (status, ms)
        confirm.wait(timeout=60)
        if status != 201:
            return
        status, body, ms = client.post(f"/reservations/{body['id']}/confirm")
        result["confirm"] = (status, ms, body.get("state"))

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def blocking_overlaps(db_path: Path) -> int:
    conn = connect(str(db_path))
    try:
        return conn.execute(
            """
            SELECT COUNT(*) FROM reservations a JOIN reservations b
              ON a.station_id = b.station_id AND a.id < b.id
             AND a.start_time < b.end_time AND b.start_time < a.end_time
            WHERE a.state IN ('CONFIRMED', 'PENDING_APPROVAL') AND b.state IN ('CONFIRMED', 'PENDING_APPROVAL')
            """
        ).fetchone()[0]
    finally:
        conn.close()


def summarize(clients: int, rounds: list[list[dict]]) -> dict:
    creates = [r["create"] for rnd in rounds for r in rnd if "create" in r]
    confirms = [r["confirm"] for rnd in rounds for r in rnd if "confirm" in r]
    won = sum(1 for status, _, _ in confirms if status == 200)
    conflicts = sum(1 for status, _, _ in confirms if status == 409)
    errors = sum(1 for status, _ in creates if status is None or status >= 500)
    errors += sum(1 for status, _, _ in confirms if status is None or status >= 500)
    other = len(confirms) - won - conflicts - sum(1 for s, _, _ in confirms if s is None or s >= 500)
    # o každé stanici, kde v kole někdo potvrzoval, má vyhrát právě jeden
    winners_ok = all(
        sum(1 for r in rnd if r.get("station") == station and r.get("confirm", (None,))[0] == 200) == 1
        for rnd in rounds
        for station in {r["station"] for r in rnd if "confirm" in r}
    )
    create_ms, confirm_ms = [ms for _, ms in creates], [ms for _, ms, _ in confirms]
    return {
        "clients": clients, "requests": len(confirms), "won": won, "conflicts": conflicts,
        "errors": errors, "other": other, "winners_ok": winners_ok,
        "create": (percentile(create_ms, 50), percentile(create_ms, 95), max(create_ms)),
        "confirm": (percentile(confirm_ms, 50), percentile(confirm_ms, 95), max(confirm_ms)),
    }


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="measure_c04_"))
    db_path, log_path = workdir / "measure_c04.db", workdir / "server.log"
    prepare_database(db_path, max(LEVELS))
    server, base = start_server(db_path, log_path)
    print(f"== C04 měření {datetime.now():%d.%m.%Y %H:%M}, {platform.platform()}, Python {platform.python_version()},"
          f" {os.cpu_count()} logických CPU")
    print(f"== server: python run.py (Werkzeug, vlákno na request), SQLite {db_path}")
    try:
        clients = [Client(base, f"mereni{i:03d}") for i in range(max(LEVELS))]
        summaries = []
        for level_index, n in enumerate(LEVELS):
            day = date.today() + timedelta(days=1 + level_index)  # každá úroveň má svůj den, kola se nepřekrývají
            rounds = [run_round(clients[:n], day, f"{8 + r:02d}:00") for r in range(ROUNDS)]
            summaries.append(summarize(n, rounds))
    finally:
        server.terminate()
        server.wait(timeout=10)
    overlaps = blocking_overlaps(db_path)
    log = log_path.read_text(encoding="utf-8", errors="replace")
    tracebacks = log.count("Traceback")

    print(f"\n{'klienti':>7} {'OP-03':>6} {'úspěch':>6} {'409':>5} {'5xx/timeout':>11} {'jiné':>5}"
          f"   {'OP-02 p50 / p95 / max [ms]':>27}   {'OP-03 p50 / p95 / max [ms]':>27}")
    for s in summaries:
        create, confirm = "{:.0f} / {:.0f} / {:.0f}".format(*s["create"]), "{:.0f} / {:.0f} / {:.0f}".format(*s["confirm"])
        print(f"{s['clients']:>7} {s['requests']:>6} {s['won']:>6} {s['conflicts']:>5} {s['errors']:>11} {s['other']:>5}"
              f"   {create:>27}   {confirm:>27}")
    print(f"\nPřekrývající se blokující rezervace v DB (BR-02): {overlaps}")
    print(f"Výjimky v logu serveru: {tracebacks}  ({log_path})")

    s = next(x for x in summaries if x["clients"] == CRITERION_CLIENTS)
    checks = [
        ("K1", overlaps == 0 and s["winners_ok"] and s["other"] == 0,
         f"překryvy {overlaps}, na každé stanici v kole právě 1 vítěz: {'ano' if s['winners_ok'] else 'ne'}"),
        ("K2", s["errors"] == 0 and tracebacks == 0, f"5xx/timeout {s['errors']}, výjimky v logu {tracebacks}"),
        ("K3", s["confirm"][1] <= P95_LIMIT_MS, f"p95 OP-03 = {s['confirm'][1]:.0f} ms (limit {P95_LIMIT_MS} ms)"),
    ]
    print(f"\n== Kritérium při {CRITERION_CLIENTS} klientech ({ROUNDS} kol, {s['requests']} potvrzení):")
    for code, ok, detail in checks:
        print(f"[{'OK  ' if ok else 'FAIL'}] {code} {detail}")
    passed = all(ok for _, ok, _ in checks)
    print(f"== {'SPLNĚNO' if passed else 'NESPLNĚNO'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
