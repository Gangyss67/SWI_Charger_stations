"""Doručování událostí z outboxu do Notification Service (ADR-01).

ReservationService jen zapíše událost do tabulky outbox ve stejné transakci jako změnu stavu.
OutboxDispatcher ji později předá NotificationService. Když doručení selže, událost zůstane
v outboxu (attempts + last_error) a zkusí se znovu při dalším spuštění dispatcheru.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Callable

from ..db import transaction
from ..timeutil import format_ts, parse_dt

logger = logging.getLogger(__name__)

# Dispatch po POST zkouší událost nejvýš 5×, aby jedna nedoručitelná zpráva nebrzdila každý
# request. Skript scripts/dispatch_notifications.py bere i ty po limitu, takže se zpráva neztratí (C06, F1).
MAX_ATTEMPTS = 5


class OutboxDispatcher:
    def __init__(self, conn: sqlite3.Connection, notifier, clock: Callable[[], datetime] = datetime.now):
        self.conn = conn
        self.notifier = notifier
        self.clock = clock

    def pending(self, max_attempts: int | None = MAX_ATTEMPTS) -> list:
        """Nedoručené události; max_attempts=None = všechny bez ohledu na počet pokusů."""
        if max_attempts is None:
            return self.conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id", (max_attempts,)
        ).fetchall()

    def dispatch(self, max_attempts: int | None = MAX_ATTEMPTS) -> int:
        """Pokusí se doručit nedoručené události (viz pending()). Vrací počet doručených."""
        delivered = 0
        for row in self.pending(max_attempts):
            try:
                with transaction(self.conn):
                    # druhý dispatcher mohl stejnou událost mezitím doručit
                    if self.conn.execute("SELECT sent_at FROM outbox WHERE id = ?", (row["id"],)).fetchone()[0]:
                        continue
                    self._deliver(row["event"], json.loads(row["payload"]))
                    self.conn.execute(
                        "UPDATE outbox SET sent_at = ?, attempts = attempts + 1, last_error = NULL WHERE id = ?",
                        (format_ts(self.clock()), row["id"]),
                    )
                delivered += 1
            except Exception as exc:  # selhání Notification Service nesmí shodit request
                logger.exception("Outbox event %s (%s) failed", row["id"], row["event"])
                self.conn.execute(
                    "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                    (f"{type(exc).__name__}: {exc}"[:500], row["id"]),
                )
        return delivered

    def _deliver(self, event: str, payload: dict) -> None:
        if event == "slot_released":
            self.notifier.slot_released(
                station_id=payload["station_id"],
                start=parse_dt(payload["start"]),
                end=parse_dt(payload["end"]),
                exclude_user_id=payload["exclude_user_id"],
            )
        elif event in ("reservation_approved", "reservation_rejected", "reservation_cancelled_by_admin"):
            getattr(self.notifier, event)(payload["reservation"])
        else:
            raise ValueError(f"Neznámá událost {event}")
