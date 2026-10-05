"""Kontrola architektonického pravidla z ADR-01.

Pravidlo: rezervační logika (services/reservations.py) nezná Notification Service. Notifikace jdou
jen přes outbox – službu NotificationService volá výhradně OutboxDispatcher (services/outbox.py).
Kontrola čte zdrojáky přes ast, takže ji stačí spustit spolu s ostatními testy (pytest).
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "charging"
NOTIFICATION_NAMES = {"NotificationService", "DatabaseChannel", "NotificationChannel"}


def imported_names(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def used_attributes(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_reservation_logic_does_not_depend_on_notification_service():
    path = SRC / "services" / "reservations.py"
    imports = imported_names(path)
    assert not {"notifications", "outbox"} & {name.rsplit(".", 1)[-1] for name in imports}
    assert not NOTIFICATION_NAMES & imports
    assert not {"notifier", "_notify", "send"} & used_attributes(path)


def test_only_dispatcher_calls_notification_service():
    """Události (reservation_approved, slot_released, ...) na Notification Service volá jen OutboxDispatcher."""
    events = {"reservation_approved", "reservation_rejected", "reservation_cancelled_by_admin", "slot_released"}
    callers = set()
    for path in SRC.rglob("*.py"):
        if path.name == "notifications.py":
            continue  # tady jsou ty metody definované
        attrs = used_attributes(path)
        if attrs & events or "notifier" in attrs:
            callers.add(path.relative_to(SRC).as_posix())
    assert callers == {"services/outbox.py"}
