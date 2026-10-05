from datetime import datetime

import pytest

from charging import create_app
from charging.db import connect, init_schema
from charging.services.notifications import DatabaseChannel, NotificationService
from charging.services.reservations import ReservationService
from charging.timeutil import format_ts


class FakeClock:
    def __init__(self, now: datetime):
        self.value = now

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def clock():
    # "teď" = úterý 15.09.2026 08:00; příklady ověření ze specifikace jsou na tentýž den
    return FakeClock(datetime(2026, 9, 15, 8, 0))


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    init_schema(c, seed_demo_users=False)
    yield c
    c.close()


@pytest.fixture
def svc(conn, clock):
    return ReservationService(conn, NotificationService(conn, DatabaseChannel(conn, clock)), clock)


def make_user(conn, username: str, vehicle_model_id: int = 1, is_admin: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, vehicle_model_id, is_admin, created_at) VALUES (?, 'x', ?, ?, ?)",
        (username, vehicle_model_id, int(is_admin), format_ts(datetime.now())),
    )
    return cur.lastrowid


@pytest.fixture
def app(tmp_path, clock):
    return create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "app.db"), "SECRET_KEY": "test-secret", "CLOCK": clock}
    )


@pytest.fixture
def client(app):
    return app.test_client()
