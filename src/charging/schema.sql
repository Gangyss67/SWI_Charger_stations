-- Schéma databáze rezervačního systému nabíjecích stanic (SQLite).
-- Business pravidla jsou vynucena i v databázi (CHECK + triggery), aby je nešlo
-- obejít chybou v aplikační vrstvě ani souběžnými požadavky.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    vehicle_type  TEXT    NOT NULL CHECK (vehicle_type IN ('BEV', 'PHEV')),
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    power_kw      INTEGER NOT NULL CHECK (power_kw > 0),
    is_high_speed INTEGER NOT NULL CHECK (is_high_speed IN (0, 1))
);

-- Časy jsou lokální, ve formátu 'YYYY-MM-DD HH:MM', zarovnané na 30min sloty.
-- Tento formát je lexikograficky řaditelný, takže porovnání řetězců = porovnání časů.
CREATE TABLE IF NOT EXISTS reservations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    station_id INTEGER NOT NULL REFERENCES stations (id),
    start_time TEXT    NOT NULL CHECK (length(start_time) = 16 AND substr(start_time, 15, 2) IN ('00', '30')),
    end_time   TEXT    NOT NULL CHECK (length(end_time) = 16 AND substr(end_time, 15, 2) IN ('00', '30')),
    state      TEXT    NOT NULL CHECK (state IN ('DRAFT', 'PENDING', 'CONFIRMED', 'CANCELLED')),
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL,
    CHECK (start_time < end_time)
);

CREATE INDEX IF NOT EXISTS idx_reservations_station_state_time
    ON reservations (station_id, state, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_reservations_user ON reservations (user_id);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    message    TEXT    NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, is_read);

-- Common rule: dvě CONFIRMED rezervace stejné stanice se nesmí překrývat.
CREATE TRIGGER IF NOT EXISTS trg_reservations_no_overlap_insert
BEFORE INSERT ON reservations
WHEN NEW.state = 'CONFIRMED'
BEGIN
    SELECT RAISE(ABORT, 'CONFIRMED_OVERLAP')
    WHERE EXISTS (
        SELECT 1 FROM reservations r
        WHERE r.station_id = NEW.station_id
          AND r.state = 'CONFIRMED'
          AND r.start_time < NEW.end_time
          AND NEW.start_time < r.end_time
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_reservations_no_overlap_update
BEFORE UPDATE OF state, station_id, start_time, end_time ON reservations
WHEN NEW.state = 'CONFIRMED'
BEGIN
    SELECT RAISE(ABORT, 'CONFIRMED_OVERLAP')
    WHERE EXISTS (
        SELECT 1 FROM reservations r
        WHERE r.id <> NEW.id
          AND r.station_id = NEW.station_id
          AND r.state = 'CONFIRMED'
          AND r.start_time < NEW.end_time
          AND NEW.start_time < r.end_time
    );
END;

-- Domain rule: vysokorychlostní nabíječky smí rezervovat pouze BEV (ne plug-in hybrid).
CREATE TRIGGER IF NOT EXISTS trg_reservations_high_speed_insert
BEFORE INSERT ON reservations
WHEN (SELECT is_high_speed FROM stations WHERE id = NEW.station_id) = 1
 AND (SELECT vehicle_type FROM users WHERE id = NEW.user_id) <> 'BEV'
BEGIN
    SELECT RAISE(ABORT, 'HIGH_SPEED_BEV_ONLY');
END;

CREATE TRIGGER IF NOT EXISTS trg_reservations_high_speed_update
BEFORE UPDATE OF state, station_id, user_id ON reservations
WHEN NEW.state <> 'CANCELLED'
 AND (SELECT is_high_speed FROM stations WHERE id = NEW.station_id) = 1
 AND (SELECT vehicle_type FROM users WHERE id = NEW.user_id) <> 'BEV'
BEGIN
    SELECT RAISE(ABORT, 'HIGH_SPEED_BEV_ONLY');
END;

-- Povolené přechody stavů:
--   DRAFT -> PENDING | CANCELLED
--   PENDING -> CONFIRMED | CANCELLED
--   CONFIRMED -> CANCELLED
--   CANCELLED je koncový stav.
CREATE TRIGGER IF NOT EXISTS trg_reservations_state_transition
BEFORE UPDATE OF state ON reservations
WHEN NEW.state <> OLD.state AND NOT (
       (OLD.state = 'DRAFT'     AND NEW.state IN ('PENDING', 'CANCELLED'))
    OR (OLD.state = 'PENDING'   AND NEW.state IN ('CONFIRMED', 'CANCELLED'))
    OR (OLD.state = 'CONFIRMED' AND NEW.state = 'CANCELLED')
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_STATE_TRANSITION');
END;
