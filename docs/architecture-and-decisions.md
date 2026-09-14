# Architecture and decisions

## Přehled

```
Prohlížeč / HTTP klient
        │
        ▼
Flask (views.py, auth.py, security.py)      ← HTTP, přihlášení, CSRF, formuláře, JSON API
        │
        ▼
ReservationService (services/reservations.py) ← doménová logika: stavy, pravidla, dostupnost
        │                         │
        ▼                         ▼  doménové události (po commitu)
SQLite (schema.sql)          NotificationService (services/notifications.py)  ← boundary
 tabulky + CHECK + triggery        │
                                   ▼
                          NotificationChannel (dnes DatabaseChannel = in-app schránka)
```

## Stavový automat rezervace

```
            submit            approve (admin)
  DRAFT ───────────► PENDING ───────────────► CONFIRMED
    │                   │                         │
    └──── cancel ───────┴──────── cancel ─────────┘
                        ▼
                    CANCELLED  (koncový stav)
```

Stav slotu/stanice se nepersistuje, ale počítá z rezervací: **volná** (bez CONFIRMED), **rezervovaná** (CONFIRMED v budoucnu), **aktuálně používaná** (CONFIRMED probíhá právě teď).

## Rozhodnutí

### D1 – Stack: Python + Flask + SQLite + pytest
- **Kontext:** malý tým, krátký semestr, požadavek na funkční DB a přihlašování; stack si tým podporuje sám.
- **Rozhodnutí:** Flask (minimum „magie“, celý request je čitelný v jednom souboru), SQLite (součást Pythonu – žádný DB server k instalaci, clean checkout se spustí dvěma příkazy), pytest.
- **Důsledky:** SQLite má jednoho zapisujícího najednou – pro školní provoz dostačuje, ale je to přesně místo, na které míří zvolená future pressure (Q). Přechod na PostgreSQL by znamenal přepsat triggery (nebo použít exclusion constraint `tstzrange`) a připojení v `db.py`; doménová logika zůstává.

### D2 – Business pravidla vynucujeme dvakrát: ve službě i v databázi
- **Rozhodnutí:** `ReservationService` kontroluje pravidla a vrací srozumitelné chyby; triggery v `schema.sql` (`CONFIRMED_OVERLAP`, `HIGH_SPEED_BEV_ONLY`, `INVALID_STATE_TRANSITION`) jsou pojistka proti chybě v kódu, ručnímu zásahu do DB i souběhu.
- **Souběh:** operace běží v `BEGIN IMMEDIATE` transakci – kontrola dostupnosti a zápis jsou atomické.
- **Důsledky:** pravidlo je na dvou místech; testy ověřují obě vrstvy zvlášť.

### D3 – Časy jako text `YYYY-MM-DD HH:MM` v lokálním čase, sloty po 30 min
- **Rozhodnutí:** formát je lexikograficky řaditelný, takže překryv `a.start < b.end AND b.start < a.end` funguje přímo v SQL. Zarovnání na :00/:30 hlídá služba i `CHECK`.
- **Důsledky:** nepodporujeme více časových pásem; přechod letního/zimního času není řešen (viz Unknown v Project Frame).

### D4 – Schvalování přes PENDING, ne rovnou CONFIRMED
- **Rozhodnutí:** uživatel vytvoří DRAFT, odešle (PENDING), admin potvrdí (CONFIRMED). Více PENDING žádostí se smí překrývat; potvrdit jde jen tu, která nekoliduje s jinou CONFIRMED.
- **Důsledky:** admin vidí u kolidujících žádostí varování a tlačítko potvrzení se nezobrazí.

### D5 – Notification Service jako boundary za rozhraním
- **Rozhodnutí:** služba rezervací volá jen doménové události (`reservation_submitted`, `reservation_confirmed`, `reservation_cancelled_by_admin`, `slot_released`). Kdo dostane zprávu, rozhoduje `NotificationService`; doručení zajišťuje `NotificationChannel` (dnes zápis do tabulky `notifications`).
- **Selhání boundary:** notifikace se posílá až po commitu a výjimka z ní je zalogována, ale nezvrátí provedenou operaci (test `test_notification_failure_does_not_break_reservation`).
- **slot_released:** po zrušení CONFIRMED rezervace upozorní (1) žadatele s PENDING na stejné stanici, kterou nyní lze potvrdit, a (2) uživatele s rezervací ve stejném čase na slabší stanici, pokud smí uvolněnou stanici použít (HPC jen BEV).

### D6 – Bezpečnost přihlášení
- Hesla hashovaná přes `werkzeug.security` (scrypt), session cookie `HttpOnly` + `SameSite=Lax`, obnovení session při přihlášení, CSRF token na všech POST, kontrola vlastnictví/role u každé operace, odhlášení jen přes POST.
