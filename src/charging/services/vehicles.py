"""Profil vozidel uživatele – uložená auta, ze kterých se předvyplní rezervace.

Rezervace si typ vozu, SPZ a stát kopíruje (OP-02 je má u každé rezervace, D8), takže úprava
nebo smazání vozidla nemění historii rezervací.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from ..db import transaction
from ..timeutil import format_ts
from .reservations import NotFoundError, ReservationError, normalize_license_plate, parse_vehicle_type

MAX_VEHICLES = 10
NAME_MAX_LENGTH = 40


class VehicleService:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = datetime.now):
        self.conn = conn
        self.clock = clock

    def list(self, user_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM vehicles WHERE user_id = ? ORDER BY is_default DESC, name COLLATE NOCASE, id", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, user_id: int, vehicle_id) -> dict:
        """Vozidlo daného uživatele; cizí nebo neexistující vozidlo -> NotFoundError (neprozradí, že existuje)."""
        try:
            vehicle_id = int(vehicle_id)
        except (TypeError, ValueError):
            raise NotFoundError("Vozidlo neexistuje.") from None
        row = self.conn.execute("SELECT * FROM vehicles WHERE id = ? AND user_id = ?", (vehicle_id, user_id)).fetchone()
        if row is None:
            raise NotFoundError("Vozidlo neexistuje.")
        return dict(row)

    def default(self, user_id: int) -> dict | None:
        vehicles = self.list(user_id)
        return vehicles[0] if vehicles else None

    def add(self, user_id: int, name, vehicle_type, license_plate, plate_country="CZ") -> int:
        name = " ".join(str(name or "").split())
        if not 1 <= len(name) <= NAME_MAX_LENGTH:
            raise ReservationError(f"Pojmenujte vozidlo (1–{NAME_MAX_LENGTH} znaků), třeba „Tesla Model 3“.")
        vehicle = parse_vehicle_type(vehicle_type)
        plate, country = normalize_license_plate(license_plate, plate_country)
        with transaction(self.conn):
            count = self.conn.execute("SELECT COUNT(*) FROM vehicles WHERE user_id = ?", (user_id,)).fetchone()[0]
            if count >= MAX_VEHICLES:
                raise ReservationError(f"Uložit jde nejvýš {MAX_VEHICLES} vozidel.")
            try:
                cur = self.conn.execute(
                    "INSERT INTO vehicles (user_id, name, vehicle_type, license_plate, plate_country, is_default, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (user_id, name, vehicle, plate, country, int(count == 0), format_ts(self.clock())),
                )
            except sqlite3.IntegrityError:
                raise ReservationError(f"Vozidlo se SPZ {plate} už máte uložené.") from None
        return cur.lastrowid

    def set_default(self, user_id: int, vehicle_id) -> None:
        vehicle = self.get(user_id, vehicle_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE vehicles SET is_default = (id = ?) WHERE user_id = ?", (vehicle["id"], user_id))

    def delete(self, user_id: int, vehicle_id) -> None:
        vehicle = self.get(user_id, vehicle_id)
        with transaction(self.conn):
            self.conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle["id"],))
            if vehicle["is_default"]:  # výchozím se stane nejstarší zbylé vozidlo
                self.conn.execute(
                    "UPDATE vehicles SET is_default = 1 WHERE id = (SELECT MIN(id) FROM vehicles WHERE user_id = ?)",
                    (user_id,),
                )
