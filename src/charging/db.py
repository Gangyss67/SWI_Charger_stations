"""Připojení k SQLite, inicializace schématu a demo data."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

from .topics import new_push_topic

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 7  # PRAGMA user_version nastavuje schema.sql; 0/1 = C01, 2–4 = v0.2.x, 5 = outbox (C03),
#                     6 = vozidla v profilu, stát SPZ, notifikace na telefon, 7 = stálé téma + zapnuto/vypnuto
MIN_MIGRATABLE_VERSION = 5  # z v5 umíme migrovat bez ztráty dat, starší DB jde do zálohy

# 10 nabíječek, z toho 3 vysokorychlostní (jen pro BEV). DC 07 a HPC 08–10 (>= 50 kW)
# vyžadují schválení administrátorem (BR-06).
STATIONS = [
    (1, "AC 01", 11, 0),
    (2, "AC 02", 11, 0),
    (3, "AC 03", 11, 0),
    (4, "AC 04", 22, 0),
    (5, "AC 05", 22, 0),
    (6, "AC 06", 22, 0),
    (7, "DC 07", 50, 0),
    (8, "HPC 08", 150, 1),
    (9, "HPC 09", 150, 1),
    (10, "HPC 10", 150, 1),
]

DEMO_USERS = [
    # username, password, vehicle_type, is_admin
    ("admin", "admin1234", "BEV", 1),
    ("ridic_ev", "ev123456", "BEV", 0),
    ("ridic_phev", "phev1234", "PHEV", 0),
]


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transakce řídíme explicitně přes transaction().
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Zápisy z vláken jednoho procesu čekají ve frontě tady, ne v busy handleru SQLite, který
# čekající vlákna uspává a budí po 1–100 ms (C04, D9). Jiný proces (dispatch skript) dál
# čeká přes timeout spojení.
_write_lock = threading.RLock()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE získá zápisový zámek hned, takže kontrola dostupnosti
    a zápis proběhnou atomicky i při souběžných požadavcích."""
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    """v5 -> v6 -> v7: jen přidává sloupce, data zůstávají. Nové tabulky a indexy pak založí schema.sql
    (CREATE ... IF NOT EXISTS), který nastaví i user_version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'reservations'").fetchone()
    if has_tables is None or not MIN_MIGRATABLE_VERSION <= version < SCHEMA_VERSION:
        return
    with transaction(conn):
        if version == 5:  # v6: stát SPZ, téma pro notifikace na telefon
            conn.execute(
                "ALTER TABLE reservations ADD COLUMN plate_country TEXT NOT NULL DEFAULT 'CZ'"
                " CHECK (length(plate_country) BETWEEN 1 AND 3)"
            )
            conn.execute("ALTER TABLE users ADD COLUMN push_topic TEXT")
            version = 6
        if version == 6:  # v7: zapnutí posílání je zvlášť, téma zůstává stálé
            conn.execute(
                "ALTER TABLE users ADD COLUMN push_enabled INTEGER NOT NULL DEFAULT 0 CHECK (push_enabled IN (0, 1))"
            )
            conn.execute("UPDATE users SET push_enabled = 1 WHERE push_topic IS NOT NULL")


def assign_push_topics(conn: sqlite3.Connection) -> None:
    """Každý uživatel má stálé téma pro notifikace na telefon – doplní ho těm, kteří ho ještě nemají."""
    for (user_id,) in conn.execute("SELECT id FROM users WHERE push_topic IS NULL").fetchall():
        conn.execute("UPDATE users SET push_topic = ? WHERE id = ? AND push_topic IS NULL", (new_push_topic(), user_id))


def init_schema(conn: sqlite3.Connection, seed_demo_users: bool = True) -> None:
    migrate(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with transaction(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO stations (id, name, power_kw, is_high_speed) VALUES (?, ?, ?, ?)",
            STATIONS,
        )
        if seed_demo_users:
            for username, password, vehicle_type, is_admin in DEMO_USERS:
                exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, vehicle_type, is_admin, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (username, generate_password_hash(password), vehicle_type, is_admin, now),
                    )
        assign_push_topics(conn)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def retire_outdated_database(path: str) -> Path | None:
    """Schéma starší než v5 nejde rozšířit přes CREATE TABLE IF NOT EXISTS (jiné stavy a sloupce).
    Soubor se proto nepřepisuje, ale přejmenuje na zálohu a založí se nová databáze.
    DB ve verzi 5 se nezálohuje, tu převede migrate()."""
    db_path = Path(path)
    if not db_path.exists():
        return None
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'reservations'").fetchone()
    finally:
        conn.close()
    if has_tables is None or version >= MIN_MIGRATABLE_VERSION:
        return None
    backup = db_path.with_name(f"{db_path.name}.v{version}.bak")
    db_path.replace(backup)
    return backup


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    backup = retire_outdated_database(app.config["DATABASE"])
    if backup is not None:
        app.logger.warning("Databáze se starším schématem byla přesunuta do %s", backup)
    conn = connect(app.config["DATABASE"])
    try:
        init_schema(conn, seed_demo_users=app.config["SEED_DEMO_USERS"])
    finally:
        conn.close()
