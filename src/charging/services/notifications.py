"""Notification Service – externí hranice systému.

ReservationService volá pouze doménové události (reservation_approved, slot_released, ...).
NotificationService rozhodne, koho upozornit, a zprávu odešle přes NotificationChannel.
Výchozí kanál ukládá zprávy do DB (in-app schránka); e-mail/SMS/push provider lze
doplnit jako další implementaci NotificationChannel bez zásahu do doménové logiky.
Tak je doplněný PushChannel – kopie zprávy na telefon přes ntfy, když si ji uživatel zapne v profilu.

Napojení podle diagramu případů užití v0.2: OP-04 Cancel (uvolnění slotu, zrušení
administrátorem) a OP-05 Approve / Reject (výsledek rozhodnutí žadateli). Od C09 (CR-08)
i propadnutí žádosti podle BR-06.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import urllib.request
from datetime import datetime
from typing import Callable, Protocol

from ..db import transaction
from ..topics import new_push_topic
from ..timeutil import format_dt, format_range, format_ts

logger = logging.getLogger(__name__)


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


# ------------------------------------------------------------------ notifikace na telefon (ntfy)

DEFAULT_NTFY_URL = "https://ntfy.sh"
PUSH_TIMEOUT_S = 5
_PLATE_IN_MESSAGE = re.compile(r", SPZ [^)]*")


def ntfy_send(url: str, text: str) -> None:
    """Pošle zprávu na téma ntfy (https://docs.ntfy.sh/publish/). Telefon s aplikací ntfy,
    která téma odebírá, ji ukáže jako notifikaci."""
    request = urllib.request.Request(
        url, data=text.encode("utf-8"), method="POST", headers={"Title": "VoltBook", "Tags": "zap"}
    )
    with urllib.request.urlopen(request, timeout=PUSH_TIMEOUT_S) as response:
        response.read()


class PushChannel:
    """Kopie zprávy na telefon pro uživatele, kteří si v profilu zapnuli notifikace (users.push_enabled)
    – posílá se na jejich stálé téma (users.push_topic).

    Hlavní kanál zůstává in-app schránka (DatabaseChannel), která jde přes outbox a neztratí se (ADR-01).
    Push je navíc a posílá se jen jednou: když ntfy nejede, chyba se zaloguje a zpráva v aplikaci zůstane.
    Kdyby se push opakoval přes outbox, výpadek ntfy by zdržoval každý POST a blokoval i in-app zprávy.
    Posílá se ve vlákně, aby HTTP volání nedrželo zámek zápisu do DB (dispatcher běží v transakci).
    SPZ na cizí server neposíláme.
    """

    def __init__(self, conn: sqlite3.Connection, base_url: str | None, sender: Callable[[str, str], None] = ntfy_send,
                 background: bool = True):
        self.conn = conn
        self.base_url = (base_url or "").rstrip("/")
        self.sender = sender
        self.background = background

    def send(self, user_id: int, message: str) -> None:
        if not self.base_url:
            return
        row = self.conn.execute("SELECT push_topic, push_enabled FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or not row["push_enabled"] or not row["push_topic"]:
            return
        topic = row["push_topic"]
        args = (user_id, f"{self.base_url}/{topic}", _PLATE_IN_MESSAGE.sub("", message))
        if self.background:
            threading.Thread(target=self._deliver, args=args, name="push").start()
        else:
            self._deliver(*args)

    def _deliver(self, user_id: int, url: str, text: str) -> None:
        try:
            self.sender(url, text)
        except Exception as exc:  # výpadek ntfy nesmí rozbít doručení do aplikace
            logger.warning("Push notifikace pro uživatele %s se nepovedla: %s: %s", user_id, type(exc).__name__, exc)


class MultiChannel:
    """Pošle zprávu všemi kanály popořadě (in-app schránka, pak telefon)."""

    def __init__(self, *channels: NotificationChannel):
        self.channels = channels

    def send(self, user_id: int, message: str) -> None:
        for channel in self.channels:
            channel.send(user_id, message)


def build_channel(conn: sqlite3.Connection, clock: Callable[[], datetime], config) -> NotificationChannel:
    """Kanály podle konfigurace aplikace: NTFY_URL (prázdné = push vypnutý), PUSH_SENDER a
    PUSH_BACKGROUND jen pro testy."""
    push = PushChannel(conn, config.get("NTFY_URL"), config.get("PUSH_SENDER") or ntfy_send,
                       config.get("PUSH_BACKGROUND", True))
    return MultiChannel(DatabaseChannel(conn, clock), push)


def get_push_topic(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Stálé téma uživatele. Kdo ho ještě nemá (účet založený jinou cestou), dostane ho teď – jednou."""
    row = conn.execute("SELECT push_topic FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    if row["push_topic"] is None:
        with transaction(conn):
            conn.execute("UPDATE users SET push_topic = ? WHERE id = ? AND push_topic IS NULL", (new_push_topic(), user_id))
        row = conn.execute("SELECT push_topic FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["push_topic"]


def is_push_enabled(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT push_enabled FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["push_enabled"])


def enable_push(conn: sqlite3.Connection, user_id: int) -> str:
    """Zapne posílání na telefon. Téma se nemění – v telefonu stačí přidat jednou."""
    topic = get_push_topic(conn, user_id)
    with transaction(conn):
        conn.execute("UPDATE users SET push_enabled = 1 WHERE id = ?", (user_id,))
    return topic


def disable_push(conn: sqlite3.Connection, user_id: int) -> None:
    """Vypne posílání, téma zůstává – po opětovném zapnutí chodí zprávy na totéž téma."""
    with transaction(conn):
        conn.execute("UPDATE users SET push_enabled = 0 WHERE id = ?", (user_id,))


def regenerate_push_topic(conn: sqlite3.Connection, user_id: int) -> str:
    """Nové téma jen na výslovnou žádost – když staré zná někdo cizí (téma funguje jako heslo k odběru)."""
    topic = new_push_topic()
    with transaction(conn):
        conn.execute("UPDATE users SET push_topic = ? WHERE id = ?", (topic, user_id))
    return topic


def _describe(res: dict) -> str:
    return f"#{res['id']} ({res['station_name']}, {format_range(res['start_time'], res['end_time'])}, SPZ {res['license_plate']})"


class NotificationService:
    def __init__(self, conn: sqlite3.Connection, channel: NotificationChannel):
        self.conn = conn
        self.channel = channel

    def reservation_approved(self, res: dict) -> None:
        self.channel.send(
            res["user_id"],
            f"Vaše žádost {_describe(res)} byla schválena administrátorem. Rezervace je potvrzená.",
        )

    def reservation_rejected(self, res: dict) -> None:
        self.channel.send(
            res["user_id"],
            f"Vaše žádost {_describe(res)} byla zamítnuta administrátorem. "
            f"Termín je znovu volný, zvolte jiný čas nebo stanici.",
        )

    def approval_requested(self, res: dict) -> None:
        """Nová žádost na DC/HPC čeká na schválení -> všem administrátorům (kromě žadatele, když je sám admin)."""
        deadline = res["approval_deadline"]
        admins = self.conn.execute("SELECT id FROM users WHERE is_admin = 1 AND id <> ?", (res["user_id"],)).fetchall()
        for admin in admins:
            self.channel.send(
                admin["id"],
                f"Nová žádost ke schválení {_describe(res)}. "
                f"Rozhodněte nejpozději {deadline[8:10]}.{deadline[5:7]}. v {deadline[11:16]}, jinak propadne.",
            )

    def reservation_expired(self, res: dict) -> None:
        """CR-08: o žádosti nikdo nerozhodl do T_start − 15 min (BR-06)."""
        self.channel.send(
            res["user_id"],
            f"Vaše žádost {_describe(res)} propadla – administrátor o ní nerozhodl do {res['approval_deadline'][11:16]}. "
            f"Termín pro vás neplatí, zvolte jiný čas nebo AC stanici.",
        )

    def reservation_cancelled_by_admin(self, res: dict) -> None:
        self.channel.send(
            res["user_id"],
            f"Vaše rezervace {_describe(res)} byla zrušena administrátorem.",
        )

    def slot_released(self, station_id: int, start: datetime, end: datetime, exclude_user_id: int) -> None:
        """Stanice přestala být v intervalu blokována. Upozorníme:
        1) vlastníky návrhů (DRAFT) na stejné stanici, které teď lze potvrdit,
        2) uživatele s aktivní rezervací ve stejném čase na slabší stanici, kteří smí
           uvolněnou stanici použít (HPC jen BEV podle typu vozu v rezervaci) – uvolnil se vyšší výkon.
        """
        station = self.conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
        if station is None:
            return
        s, e = format_dt(start), format_dt(end)
        released = format_range(s, e)
        notified = {exclude_user_id}

        same_station = self.conn.execute(
            """
            SELECT r.id, r.user_id, r.draft_expires_at FROM reservations r
            WHERE r.station_id = ? AND r.state = 'DRAFT'
              AND r.start_time < ? AND ? < r.end_time
              AND NOT EXISTS (
                  SELECT 1 FROM reservations c
                  WHERE c.station_id = r.station_id AND c.state IN ('CONFIRMED', 'PENDING_APPROVAL')
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
                f"Váš návrh #{row['id']} teď můžete potvrdit (platí do {row['draft_expires_at'][11:]}).",
            )

        better = self.conn.execute(
            """
            SELECT r.id, r.user_id, s.name AS station_name, s.power_kw FROM reservations r
            JOIN stations s ON s.id = r.station_id
            WHERE r.station_id <> ? AND r.state IN ('DRAFT', 'PENDING_APPROVAL', 'CONFIRMED')
              AND s.power_kw < ?
              AND r.start_time < ? AND ? < r.end_time
              AND (? = 0 OR r.vehicle_type = 'BEV')
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
