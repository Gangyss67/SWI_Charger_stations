-- Schéma databáze rezervačního systému nabíjecích stanic (SQLite) – specifikace baseline v0.2.
-- Business pravidla jsou vynucena i v databázi (CHECK + triggery), aby je nešlo
-- obejít chybou v aplikační vrstvě ani souběžnými požadavky. Časová pravidla
-- (5 min DRAFT, T_start − 15 min, storno 30 min) hlídá služba – DB nezná „teď“ aplikace.

-- Katalog modelů vozidel: typ pohonu (BR-04) a maximální DC nabíjecí výkon (BR-06, 0 = umí jen AC).
CREATE TABLE IF NOT EXISTS vehicle_models (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    vehicle_type TEXT    NOT NULL CHECK (vehicle_type IN ('BEV', 'PHEV')),
    max_dc_kw    INTEGER NOT NULL CHECK (max_dc_kw >= 0)
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash    TEXT    NOT NULL,
    vehicle_model_id INTEGER NOT NULL REFERENCES vehicle_models (id),  -- výchozí vozidlo do formulářů
    is_admin         INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at       TEXT    NOT NULL
);

-- is_high_speed = HPC (BR-04: jen BEV); power_kw >= 50 = rychlá stanice DC/HPC (BR-06: připuštění podle výkonu vozidla).
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
    vehicle_model_id  INTEGER NOT NULL REFERENCES vehicle_models (id),
    license_plate     TEXT    NOT NULL CHECK (length(trim(license_plate)) > 0),
    start_time        TEXT    NOT NULL CHECK (length(start_time) = 16 AND substr(start_time, 15, 2) IN ('00', '30')),
    end_time          TEXT    NOT NULL CHECK (length(end_time) = 16 AND substr(end_time, 15, 2) IN ('00', '30')),
    state             TEXT    NOT NULL CHECK (state IN (
                          'DRAFT', 'PENDING_APPROVAL', 'CONFIRMED',
                          'CANCELLED', 'CANCELLED_BY_ADMIN', 'REJECTED', 'EXPIRED')),
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    draft_expires_at  TEXT    NOT NULL,  -- BR-05: created_at + 5 min
    approval_deadline TEXT,              -- BR-06: start_time − 15 min (jen žádost o výjimku)
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
-- BR-06: na rychlou stanici (>= 50 kW) smí jen vozidlo, které umí nabíjet stejnosměrně (max_dc_kw > 0).
CREATE TRIGGER IF NOT EXISTS trg_reservations_vehicle_insert
BEFORE INSERT ON reservations
BEGIN
    SELECT RAISE(ABORT, 'HIGH_SPEED_BEV_ONLY')
    WHERE (SELECT is_high_speed FROM stations WHERE id = NEW.station_id) = 1
      AND (SELECT vehicle_type FROM vehicle_models WHERE id = NEW.vehicle_model_id) <> 'BEV';
    SELECT RAISE(ABORT, 'DC_NOT_SUPPORTED')
    WHERE (SELECT power_kw FROM stations WHERE id = NEW.station_id) >= 50
      AND (SELECT max_dc_kw FROM vehicle_models WHERE id = NEW.vehicle_model_id) = 0;
END;

CREATE TRIGGER IF NOT EXISTS trg_reservations_vehicle_update
BEFORE UPDATE OF station_id, vehicle_model_id ON reservations
BEGIN
    SELECT RAISE(ABORT, 'HIGH_SPEED_BEV_ONLY')
    WHERE (SELECT is_high_speed FROM stations WHERE id = NEW.station_id) = 1
      AND (SELECT vehicle_type FROM vehicle_models WHERE id = NEW.vehicle_model_id) <> 'BEV';
    SELECT RAISE(ABORT, 'DC_NOT_SUPPORTED')
    WHERE (SELECT power_kw FROM stations WHERE id = NEW.station_id) >= 50
      AND (SELECT max_dc_kw FROM vehicle_models WHERE id = NEW.vehicle_model_id) = 0;
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

-- BR-06: potvrzení DRAFT vede do CONFIRMED, pokud je stanice AC (< 50 kW) nebo vozidlo nabíjí aspoň
-- polovinou výkonu stanice; slabší vozidlo na rychlé stanici smí jen do PENDING_APPROVAL (žádost o výjimku).
CREATE TRIGGER IF NOT EXISTS trg_reservations_approval_policy
BEFORE UPDATE OF state ON reservations
WHEN OLD.state = 'DRAFT' AND NEW.state IN ('CONFIRMED', 'PENDING_APPROVAL')
 AND (NEW.state = 'PENDING_APPROVAL') <> (
        (SELECT power_kw FROM stations WHERE id = NEW.station_id) >= 50
    AND (SELECT max_dc_kw FROM vehicle_models WHERE id = NEW.vehicle_model_id) * 2
        < (SELECT power_kw FROM stations WHERE id = NEW.station_id)
 )
BEGIN
    SELECT RAISE(ABORT, 'APPROVAL_REQUIRED');
END;

PRAGMA user_version = 3;
