"""Notification Service – externí hranice systému.

ReservationService volá pouze doménové události (reservation_confirmed, slot_released, ...).
NotificationService rozhodne, koho upozornit, a zprávu odešle přes NotificationChannel.
Výchozí kanál ukládá zprávy do DB (in-app schránka); e-mail/SMS/push provider lze
doplnit jako další implementaci NotificationChannel bez zásahu do doménové logiky.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable, Protocol

from ..timeutil import format_dt, format_range, format_ts


class NotificationChannel(Protocol):
    def send(self, user_id: int, message: str) -> None: ...


class DatabaseChannel:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = datetime.now):
        self.conn = conn
        self.clock = clock

    def send(self, user_id: int, message: str) -> None:
        self.conn.execute(
            "INSERT INTO notifications (user_id, message, created_at) VALUES (?, ?, ?)",
            (user_id, message, format_ts(self.clock())),
        )


class NotificationService:
    def __init__(self, conn: sqlite3.Connection, channel: NotificationChannel):
        self.conn = conn
        self.channel = channel

    def reservation_submitted(self, res: dict) -> None:
        admins = self.conn.execute("SELECT id FROM users WHERE is_admin = 1 AND id <> ?", (res["user_id"],)).fetchall()
        for admin in admins:
            self.channel.send(
                admin["id"],
                f"Nová žádost #{res['id']} od uživatele {res['username']}: {res['station_name']}, "
                f"{format_range(res['start_time'], res['end_time'])}. Čeká na schválení.",
            )

    def reservation_confirmed(self, res: dict) -> None:
        self.channel.send(
            res["user_id"],
            f"Vaše rezervace #{res['id']} ({res['station_name']}, "
            f"{format_range(res['start_time'], res['end_time'])}) byla potvrzena.",
        )

    def reservation_cancelled_by_admin(self, res: dict) -> None:
        self.channel.send(
            res["user_id"],
            f"Vaše rezervace #{res['id']} ({res['station_name']}, "
            f"{format_range(res['start_time'], res['end_time'])}) byla zrušena administrátorem.",
        )

    def slot_released(self, station_id: int, start: datetime, end: datetime, exclude_user_id: int) -> None:
        """Uvolnil se termín potvrzené rezervace. Upozorníme:
        1) žadatele s čekající (PENDING) rezervací na stejné stanici, kterou teď lze potvrdit,
        2) uživatele s aktivní rezervací ve stejném čase na slabší stanici, kteří smí
           uvolněnou stanici použít (vysokorychlostní jen BEV) – uvolnil se vyšší výkon.
        """
        station = self.conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        if station is None:
            return
        s, e = format_dt(start), format_dt(end)
        released = format_range(s, e)
        notified = {exclude_user_id}

        same_station = self.conn.execute(
            """
            SELECT r.id, r.user_id FROM reservations r
            WHERE r.station_id = ? AND r.state = 'PENDING'
              AND r.start_time < ? AND ? < r.end_time
              AND NOT EXISTS (
                  SELECT 1 FROM reservations c
                  WHERE c.station_id = r.station_id AND c.state = 'CONFIRMED'
                    AND c.start_time < r.end_time AND r.start_time < c.end_time
              )
            ORDER BY r.created_at, r.id
            """,
            (station_id, e, s),
        ).fetchall()
        for row in same_station:
            if row["user_id"] in notified:
                continue
            notified.add(row["user_id"])
            self.channel.send(
                row["user_id"],
                f"Na stanici {station['name']} se uvolnil termín {released}. "
                f"Vaše čekající rezervace #{row['id']} tak nyní může být potvrzena.",
            )

        better = self.conn.execute(
            """
            SELECT r.id, r.user_id, s.name AS station_name, s.power_kw FROM reservations r
            JOIN stations s ON s.id = r.station_id
            JOIN users u ON u.id = r.user_id
            WHERE r.station_id <> ? AND r.state IN ('DRAFT', 'PENDING', 'CONFIRMED')
              AND s.power_kw < ?
              AND r.start_time < ? AND ? < r.end_time
              AND (? = 0 OR u.vehicle_type = 'BEV')
            ORDER BY r.start_time, r.id
            """,
            (station_id, station["power_kw"], e, s, station["is_high_speed"]),
        ).fetchall()
        for row in better:
            if row["user_id"] in notified:
                continue
            notified.add(row["user_id"])
            self.channel.send(
                row["user_id"],
                f"Uvolnila se výkonnější nabíječka {station['name']} ({station['power_kw']} kW) v čase {released}, "
                f"který se překrývá s vaší rezervací #{row['id']} na {row['station_name']} ({row['power_kw']} kW).",
            )


def list_for_user(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 200", (user_id,)
    ).fetchall()


def unread_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,)
    ).fetchone()[0]


def mark_all_read(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (user_id,))
