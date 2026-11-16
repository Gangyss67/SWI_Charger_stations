"""Doručí čekající události z outboxu do Notification Service (ADR-01).

Aplikace doručuje sama po každém POST požadavku, ale jednu událost zkusí nejvýš MAX_ATTEMPTS×.
Skript je pro opakování nedoručených událostí bez provozu a bere i ty po limitu, např. z cronu /
Plánovače úloh:  python scripts/dispatch_notifications.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from charging import create_app  # noqa: E402
from charging.db import get_db  # noqa: E402
from charging.services.notifications import NotificationService, build_channel  # noqa: E402
from charging.services.outbox import OutboxDispatcher  # noqa: E402


def main() -> int:
    app = create_app()  # stejná DB a schéma jako běžící aplikace
    with app.app_context():
        conn, clock = get_db(), app.config["CLOCK"]
        # stejné kanály jako aplikace: in-app schránka + telefon (ntfy), když ho má uživatel zapnutý
        dispatcher = OutboxDispatcher(conn, NotificationService(conn, build_channel(conn, clock, app.config)), clock)
        delivered = dispatcher.dispatch(max_attempts=None)
        print(f"doručeno {delivered}, čeká {len(dispatcher.pending(max_attempts=None))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
