"""Doručí čekající události z outboxu do Notification Service (ADR-01).

Aplikace doručuje sama po každém POST požadavku. Skript je pro opakování nedoručených
událostí bez provozu, např. z cronu / Plánovače úloh:  python scripts/dispatch_notifications.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from charging import create_app  # noqa: E402
from charging.db import get_db  # noqa: E402
from charging.services.notifications import DatabaseChannel, NotificationService  # noqa: E402
from charging.services.outbox import OutboxDispatcher  # noqa: E402


def main() -> int:
    app = create_app()  # stejná DB a schéma jako běžící aplikace
    with app.app_context():
        conn, clock = get_db(), app.config["CLOCK"]
        dispatcher = OutboxDispatcher(conn, NotificationService(conn, DatabaseChannel(conn, clock)), clock)
        delivered = dispatcher.dispatch()
        print(f"doručeno {delivered}, čeká {len(dispatcher.pending())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
