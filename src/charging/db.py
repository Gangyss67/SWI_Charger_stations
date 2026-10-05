"""Připojení k SQLite, inicializace schématu a demo data."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 5  # PRAGMA user_version nastavuje schema.sql; 0/1 = C01, 2–4 = v0.2.x, 5 = outbox (C03)

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


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE získá zápisový zámek hned, takže kontrola dostupnosti
    a zápis proběhnou atomicky i při souběžných požadavcích."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection, seed_demo_users: bool = True) -> None:
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


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def retire_outdated_database(path: str) -> Path | None:
    """Starší schéma nejde rozšířit přes CREATE TABLE IF NOT EXISTS (jiné stavy a sloupce).
    Soubor se proto nepřepisuje, ale přejmenuje na zálohu a založí se nová databáze."""
    db_path = Path(path)
    if not db_path.exists():
        return None
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'reservations'").fetchone()
    finally:
        conn.close()
    if has_tables is None or version >= SCHEMA_VERSION:
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
