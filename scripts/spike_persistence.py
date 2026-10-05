"""C01 Engineering spike A – Persistence.

Otázka: Přežije rezervace (včetně stavu a business pravidel) uložení do skutečné
SQLite databáze na disku, zavření spojení a nové načtení?

Spuštění:  python scripts/spike_persistence.py
Skript vždy začíná s čistou DB v instance/spike.db, výsledek vypisuje na stdout
a při jakémkoli nesouladu skončí s nenulovým exit kódem.
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from charging.db import connect, init_schema  # noqa: E402
from charging.services.reservations import ReservationService  # noqa: E402
from charging.timeutil import floor_to_slot  # noqa: E402


def main() -> int:
    db_path = ROOT / "instance" / "spike.db"
    db_path.parent.mkdir(exist_ok=True)
    db_path.unlink(missing_ok=True)

    # 1) zápis přes první spojení
    conn = connect(str(db_path))
    init_schema(conn, seed_demo_users=True)
    user_id = conn.execute("SELECT id FROM users WHERE username = 'ridic_ev'").fetchone()["id"]
    admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
    svc = ReservationService(conn)
    start = floor_to_slot(datetime.now()) + timedelta(days=1)
    # baseline v0.2: VW e-up! (40 kW) potřebuje na HPC 08 výjimku -> DRAFT -> PENDING_APPROVAL -> CONFIRMED
    reservation_id = svc.create(user_id, 8, start, 2, 10, "1AB 2345")
    svc.confirm(user_id, reservation_id)
    svc.approve(admin_id, reservation_id)
    conn.close()
    print(f"[write] reservation id={reservation_id} created+confirmed+approved, connection closed")

    # 2) načtení přes úplně nové spojení
    conn = connect(str(db_path))
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    print(f"[read ] {dict(row)}")
    checks = {
        "row exists": row is not None,
        "state == CONFIRMED": row is not None and row["state"] == "CONFIRMED",
        "station == 8 (HPC)": row is not None and row["station_id"] == 8,
        "30min x 2 slots": row is not None
        and datetime.strptime(row["end_time"], "%Y-%m-%d %H:%M") - datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M")
        == timedelta(minutes=60),
    }

    # 3) pravidlo v DB drží i po znovuotevření: překrývající DRAFT vložený mimo službu
    #    nesmí přejít do blokujícího stavu (BR-02 v0.2)
    cur = conn.execute(
        "INSERT INTO reservations (user_id, station_id, vehicle_model_id, license_plate, start_time, end_time, state,"
        " created_at, updated_at, draft_expires_at) VALUES (?, 8, 1, 'X', ?, ?, 'DRAFT', 'x', 'x', 'x')",
        (user_id, row["start_time"], row["end_time"]),
    )
    try:
        conn.execute(
            "UPDATE reservations SET state = 'CONFIRMED' WHERE id = ?", (cur.lastrowid,)
        )
        checks["overlap blocked by DB trigger"] = False
    except sqlite3.IntegrityError as exc:
        checks["overlap blocked by DB trigger"] = "BLOCKING_OVERLAP" in str(exc)
    conn.close()

    for name, ok in checks.items():
        print(f"[check] {'OK  ' if ok else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
