-- Schéma databáze rezervačního systému nabíjecích stanic (SQLite) – specifikace baseline v0.2.
-- Business pravidla jsou vynucena i v databázi (CHECK + triggery), aby je nešlo
-- obejít chybou v aplikační vrstvě ani souběžnými požadavky. Časová pravidla
-- (5 min DRAFT, T_start − 15 min, storno 30 min) hlídá služba – DB nezná „teď“ aplikace.

-- push_topic = stálé téma pro notifikace na telefon (ntfy), přidělí se jednou (v6),
-- push_enabled = posílat na telefon (v7). Komentáře nejsou uvnitř tabulky kvůli ALTER TABLE.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    vehicle_type  TEXT    NOT NULL CHECK (vehicle_type IN ('BEV', 'PHEV')),  -- výchozí vůz do formulářů
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT    NOT NULL,
    push_topic    TEXT,
    push_enabled  INTEGER NOT NULL DEFAULT 0 CHECK (push_enabled IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_push_topic ON users (push_topic) WHERE push_topic IS NOT NULL;

-- Profil vozidel uživatele (schéma v6). Rezervace si typ vozu a SPZ kopíruje, takže smazání
-- vozidla historii rezervací nezmění.
CREATE TABLE IF NOT EXISTS vehicles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users (id),
    name          TEXT    NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 40),
    vehicle_type  TEXT    NOT NULL CHECK (vehicle_type IN ('BEV', 'PHEV')),
    license_plate TEXT    NOT NULL CHECK (length(trim(license_plate)) > 0),
    plate_country TEXT    NOT NULL DEFAULT 'CZ' CHECK (length(plate_country) BETWEEN 1 AND 3),
    is_default    INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at    TEXT    NOT NULL,
    UNIQUE (user_id, plate_country, license_plate)
);

CREATE INDEX IF NOT EXISTS idx_vehicles_user ON vehicles (user_id);

-- is_high_speed = HPC (BR-04: jen BEV); power_kw >= 50 = DC/HPC (BR-06: vyžaduje schválení).
CREATE TABLE IF NOT EXISTS stations (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL UNIQUE,
    power_kw      INTEGER NOT NULL CHECK (power_kw > 0),
    is_high_speed INTEGER NOT NULL CHECK (is_high_speed IN (0, 1))
);

-- Intervaly jsou polouzavřené [start_time, end_time), lokální čas 'YYYY-MM-DD HH:MM',
-- zarovnané na 30min bloky (BR-01). Formát je lexikograficky řaditelný.
-- Lhůty (draft_expires_at, approval_deadline) jsou 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS reservations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users (id),
    station_id        INTEGER NOT NULL REFERENCES stations (id),
    vehicle_type      TEXT    NOT NULL CHECK (vehicle_type IN ('BEV', 'PHEV')),
    license_plate     TEXT    NOT NULL CHECK (length(trim(license_plate)) > 0),
    start_time        TEXT    NOT NULL CHECK (length(start_time) = 16 AND substr(start_time, 15, 2) IN ('00', '30')),
    end_time          TEXT    NOT NULL CHECK (length(end_time) = 16 AND substr(end_time, 15, 2) IN ('00', '30')),
    state             TEXT    NOT NULL CHECK (state IN (
                          'DRAFT', 'PENDING_APPROVAL', 'CONFIRMED',
                          'CANCELLED', 'CANCELLED_BY_ADMIN', 'REJECTED', 'EXPIRED')),
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    draft_expires_at  TEXT    NOT NULL,  -- BR-05: created_at + 5 min
    approval_deadline TEXT,              -- BR-06: start_time − 15 min (jen DC/HPC po potvrzení)
    plate_country     TEXT    NOT NULL DEFAULT 'CZ' CHECK (length(plate_country) BETWEEN 1 AND 3),  -- stát SPZ (v6)
    CHECK (start_time < end_time),
    CHECK (state <> 'PENDING_APPROVAL' OR approval_deadline IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_reservations_station_state_time
    ON reservations (station_id, state, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_reservations_user ON reservations (user_id);
CREATE INDEX IF NOT EXISTS idx_reservations_state ON reservations (state);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    message    TEXT    NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0 CHECK (is_read IN (0, 1)),
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, is_read);

-- Outbox (ADR-01): události pro Notification Service zapsané ve stejné transakci jako změna
-- rezervace. Doručuje je services/outbox.py, neúspěšné pokusy se opakují.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event      TEXT    NOT NULL,
    payload    TEXT    NOT NULL,  -- JSON
    created_at TEXT    NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox (sent_at, id);

-- Stavový diagram: každá rezervace vzniká jako DRAFT ([none] -> DRAFT, OP-02).
CREATE TRIGGER IF NOT EXISTS trg_reservations_initial_state
BEFORE INSERT ON reservations
WHEN NEW.state <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'INVALID_INITIAL_STATE');
END;

-- BR-02 (v0.2): dvě rezervace v blokujícím stavu (CONFIRMED, PENDING_APPROVAL)
-- se na stejné stanici nesmí překrývat.
CREATE TRIGGER IF NOT EXISTS trg_reservations_no_overlap
BEFORE UPDATE OF state, station_id, start_time, end_time ON reservations
WHEN NEW.state IN ('CONFIRMED', 'PENDING_APPROVAL')
BEGIN
    SELECT RAISE(ABORT, 'BLOCKING_OVERLAP')
    WHERE EXISTS (
        SELECT 1 FROM reservations r
        WHERE r.id <> NEW.id
          AND r.station_id = NEW.station_id
          AND r.state IN ('CONFIRMED', 'PENDING_APPROVAL')
          AND r.start_time < NEW.end_time
          AND NEW.start_time < r.end_time
    );
END;

-- BR-04: vysokorychlostní (HPC) stanice smí rezervovat pouze BEV, ne plug-in hybrid.
CREATE TRIGGER IF NOT EXISTS trg_reservations_high_speed_insert
BEFORE INSERT ON reservations
WHEN NEW.vehicle_type <> 'BEV'
 AND (SELECT is_high_speed FROM stations WHERE id = NEW.station_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'HIGH_SPEED_BEV_ONLY');
END;

CREATE TRIGGER IF NOT EXISTS trg_reservations_high_speed_update
BEFORE UPDATE OF station_id, vehicle_type ON reservations
WHEN NEW.vehicle_type <> 'BEV'
 AND (SELECT is_high_speed FROM stations WHERE id = NEW.station_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'HIGH_SPEED_BEV_ONLY');
END;

-- Povolené přechody stavů (stavový diagram v0.2):
--   DRAFT            -> CONFIRMED | PENDING_APPROVAL | EXPIRED
--   PENDING_APPROVAL -> CONFIRMED | REJECTED | CANCELLED | EXPIRED
--   CONFIRMED        -> CANCELLED | CANCELLED_BY_ADMIN
--   CANCELLED, CANCELLED_BY_ADMIN, REJECTED, EXPIRED jsou koncové stavy.
CREATE TRIGGER IF NOT EXISTS trg_reservations_state_transition
BEFORE UPDATE OF state ON reservations
WHEN NEW.state <> OLD.state AND NOT (
       (OLD.state = 'DRAFT'            AND NEW.state IN ('CONFIRMED', 'PENDING_APPROVAL', 'EXPIRED'))
    OR (OLD.state = 'PENDING_APPROVAL' AND NEW.state IN ('CONFIRMED', 'REJECTED', 'CANCELLED', 'EXPIRED'))
    OR (OLD.state = 'CONFIRMED'        AND NEW.state IN ('CANCELLED', 'CANCELLED_BY_ADMIN'))
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_STATE_TRANSITION');
END;

-- BR-06: potvrzení DRAFT vede na AC stanici (< 50 kW) rovnou do CONFIRMED,
-- na DC/HPC stanici (>= 50 kW) pouze do PENDING_APPROVAL.
CREATE TRIGGER IF NOT EXISTS trg_reservations_approval_policy
BEFORE UPDATE OF state ON reservations
WHEN OLD.state = 'DRAFT' AND (
       (NEW.state = 'CONFIRMED'        AND (SELECT power_kw FROM stations WHERE id = NEW.station_id) >= 50)
    OR (NEW.state = 'PENDING_APPROVAL' AND (SELECT power_kw FROM stations WHERE id = NEW.station_id) < 50)
)
BEGIN
    SELECT RAISE(ABORT, 'APPROVAL_REQUIRED');
END;

PRAGMA user_version = 7;
