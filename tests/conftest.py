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
    # "teď" = pondělí 14.09.2026 08:10
    return FakeClock(datetime(2026, 9, 14, 8, 10))


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    init_schema(c, seed_demo_users=False)
    yield c
    c.close()


@pytest.fixture
def svc(conn, clock):
    return ReservationService(conn, NotificationService(conn, DatabaseChannel(conn, clock)), clock)


def make_user(conn, username: str, vehicle_type: str = "BEV", is_admin: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, vehicle_type, is_admin, created_at) VALUES (?, 'x', ?, ?, ?)",
        (username, vehicle_type, int(is_admin), format_ts(datetime.now())),
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
