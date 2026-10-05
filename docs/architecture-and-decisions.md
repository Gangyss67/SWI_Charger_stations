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

## Stavový automat rezervace (baseline v0.2)

```
                 OP-03 confirm [AC]
  DRAFT ─────────────────────────────────────────────► CONFIRMED ──OP-04 user [≤ T_start−30]──► CANCELLED
    │  OP-03 confirm [DC/HPC, ≤ T_start−15]               ▲    └─────OP-04 admin [< T_end]────► CANCELLED_BY_ADMIN
    ├──────────────► PENDING_APPROVAL ──OP-05 approve─────┘
    │                    │  ├──OP-05 reject──────► REJECTED
    │                    │  └──OP-04 cancel──────► CANCELLED
    │  [> 5 min]         │ [> T_start−15]
    └────────────────────┴───────────────────────► EXPIRED
```

Diagram v PlantUML: [diagrams/state_v02.puml](diagrams/state_v02.puml). Blokující stavy (BR-02) jsou `CONFIRMED` a `PENDING_APPROVAL`.
Stav slotu/stanice se nepersistuje, ale počítá z rezervací: **volná**, **čeká na schválení** (PENDING_APPROVAL), **rezervovaná** (CONFIRMED v budoucnu), **aktuálně používaná** (CONFIRMED probíhá právě teď).

## Rozhodnutí

### D1 – Stack: Python + Flask + SQLite + pytest
- **Kontext:** malý tým, krátký semestr, požadavek na funkční DB a přihlašování; stack si tým podporuje sám.
- **Rozhodnutí:** Flask (minimum „magie“, celý request je čitelný v jednom souboru), SQLite (součást Pythonu – žádný DB server k instalaci, clean checkout se spustí dvěma příkazy), pytest.
- **Důsledky:** SQLite má jednoho zapisujícího najednou – pro školní provoz dostačuje, ale je to přesně místo, na které míří zvolená future pressure (Q). Přechod na PostgreSQL by znamenal přepsat triggery (nebo použít exclusion constraint `tstzrange`) a připojení v `db.py`; doménová logika zůstává.

### D2 – Business pravidla vynucujeme dvakrát: ve službě i v databázi
- **Rozhodnutí:** `ReservationService` kontroluje pravidla a vrací srozumitelné chyby; triggery v `schema.sql` (`BLOCKING_OVERLAP`, `HIGH_SPEED_BEV_ONLY`, `INVALID_STATE_TRANSITION`, `INVALID_INITIAL_STATE`, `APPROVAL_REQUIRED`) jsou pojistka proti chybě v kódu, ručnímu zásahu do DB i souběhu. Časová pravidla (5 min, T_start − 15 min, 30 min) hlídá jen služba – DB nezná „teď“ aplikace.
- **Souběh:** operace běží v `BEGIN IMMEDIATE` transakci – kontrola dostupnosti a zápis jsou atomické.
- **Důsledky:** pravidlo je na dvou místech; testy ověřují obě vrstvy zvlášť.

### D3 – Časy jako text `YYYY-MM-DD HH:MM` v lokálním čase, sloty po 30 min
- **Rozhodnutí:** formát je lexikograficky řaditelný, takže překryv `a.start < b.end AND b.start < a.end` funguje přímo v SQL. Zarovnání na :00/:30 hlídá služba i `CHECK`.
- **Důsledky:** nepodporujeme více časových pásem; přechod letního/zimního času není řešen (viz Unknown v Project Frame).

### D4 – Schvalování jen pro DC/HPC, žádost PENDING_APPROVAL blokuje slot (C02, baseline v0.2)
- **Původně (C01):** schvalovaly se všechny rezervace a překrývající se žádosti PENDING mohly existovat současně.
- **Rozhodnutí:** OP-03 Confirm na AC stanici vede rovnou do CONFIRMED, na stanici ≥ 50 kW (DC 07, HPC 08–10) do PENDING_APPROVAL. Žádost blokuje slot stejně jako CONFIRMED (BR-02), takže admin nikdy neschvaluje termín, který mezitím obsadil někdo jiný; OP-05 Approve proto kolize znovu nehledá, jen hlídá roli a lhůtu T_start − 15 min.
- **Důsledky:** konflikt se řeší už při potvrzení (409 pro druhého žadatele), ne až u admina. Kdo má roli Admin, ověřuje služba (403), ne jen UI.

### D5 – Notification Service jako boundary za rozhraním
- **Rozhodnutí:** služba rezervací volá jen doménové události (`reservation_approved`, `reservation_rejected`, `reservation_cancelled_by_admin`, `slot_released`) – podle diagramu případů užití v0.2 je Notification Service napojena na OP-04 a OP-05. Kdo dostane zprávu, rozhoduje `NotificationService`; doručení zajišťuje `NotificationChannel` (dnes zápis do tabulky `notifications`).
- **Selhání boundary:** notifikace se posílá až po commitu a výjimka z ní je zalogována, ale nezvrátí provedenou operaci (test `test_notification_failure_does_not_break_reservation`).
- **slot_released:** po zrušení / stažení / zamítnutí blokující rezervace upozorní (1) vlastníky návrhů (DRAFT) na stejné stanici, které teď lze potvrdit, a (2) uživatele s rezervací ve stejném čase na slabší stanici, pokud smí uvolněnou stanici použít (HPC jen BEV podle typu vozu v rezervaci).

### D6 – Bezpečnost přihlášení
- Hesla hashovaná přes `werkzeug.security` (scrypt), session cookie `HttpOnly` + `SameSite=Lax`, obnovení session při přihlášení, CSRF token na všech POST, kontrola vlastnictví/role u každé operace, odhlášení jen přes POST.

### D7 – Časové přechody do EXPIRED provádíme „líně“ (dočasné řešení)
- **Rozhodnutí:** `ReservationService.expire_overdue()` je jeden samostatně commitovaný `UPDATE`, který se volá na začátku každé operace a každého dotazu na dostupnost. Musí běžet mimo transakci operace, aby odmítnuté potvrzení (rollback) nevrátilo zápis EXPIRED.
- **Důsledky:** bez provozu se EXPIRED zapíše až při nejbližším požadavku a k propadnutí žádosti se neposílá notifikace. Správné řešení (plánovač / worker) je architektonický driver pro C03.

### D8 – Typ vozu a SPZ patří k rezervaci, ne jen k účtu
- **Rozhodnutí:** podle OP-02 zadává uživatel typ vozu a SPZ u každé rezervace; typ vozu z profilu se jen předvyplní. BR-04 (HPC jen BEV) se vyhodnocuje nad typem vozu v rezervaci (služba i trigger).
- **Důsledky:** jeden účet může rezervovat pro různá auta; fyzické ověření vozu na místě zůstává neznámou z Project Frame.

## C03 Část A – AS-IS: jak dnes funguje Confirm Reservation

Vzali jsme Confirm Reservation, protože se v něm u nás rozhoduje i o schvalování (AC stanice se potvrdí hned, DC/HPC jdou do PENDING_APPROVAL). Všechno níže jsme dohledali v kódu na tagu `v0.2.3` a ověřili testy nebo během `scripts/verify_c02.py`.

### A1. Scénář

| Položka | Hodnota |
| --- | --- |
| Scénář / operace | OP-03 Confirm Reservation |
| Požadavky | REQ-03, REQ-04 |
| Pravidla / invarianty | BR-02 (blokující stavy se nepřekrývají), BR-05 (DRAFT platí 5 min), BR-06 (DC/HPC se schvalují do T_start − 15 min) |
| Baseline | v0.2 |

### A2. Hlavní průchod

Uživatel potvrzuje svůj DRAFT, který s ničím nekoliduje.

| Krok scénáře z C02 | Realizace v kódu | Doklad |
| --- | --- | --- |
| přijmout požadavek na potvrzení | `views.py` `confirm_reservation()` (route `POST /reservations/<id>/confirm`) → `_run_action("confirm", …)` | `views.py` ř. 266–269, 244–263 |
| ověřit přihlášení a CSRF | `auth.py` `login_required`, `security.py` `csrf_protect()` (běží před view) | `auth.py` ř. 36, `security.py` ř. 22; `test_post_without_csrf_token_is_rejected` |
| propadnout staré návrhy (BR-05) | `ReservationService.confirm()` volá `expire_overdue()`, což je samostatný `UPDATE … SET state='EXPIRED'` ještě před transakcí | `services/reservations.py` ř. 445, 171–188; `test_op03_draft_older_than_5_minutes_is_expired` |
| otevřít transakci | `_atomic()` → `db.transaction()` → `BEGIN IMMEDIATE` | `services/reservations.py` ř. 446, 148; `db.py` ř. 47–57 |
| načíst Reservation | `_get_reservation_row()` (`SELECT * FROM reservations WHERE id = ?`) | `services/reservations.py` ř. 447, 209 |
| ověřit, že přechod je povolený | vlastník, stav DRAFT, začátek v budoucnu | `services/reservations.py` ř. 448–456; `test_op03_only_owner_and_only_from_draft` |
| rozhodnout, jestli je potřeba schválení (BR-06) | `requires_approval(station)` = výkon ≥ 50 kW; u DC/HPC navíc kontrola `now > start − 15 min` | `services/reservations.py` ř. 459–465, 136 |
| vyhodnotit konflikt (BR-02) | `_blocking_overlaps()`: SELECT na CONFIRMED a PENDING_APPROVAL na stejné stanici s překryvem | `services/reservations.py` ř. 466, 286 |
| změnit stav | AC: `_set_state(CONFIRMED)`; DC/HPC: `UPDATE … state='PENDING_APPROVAL', approval_deadline=…` | `services/reservations.py` ř. 468–476, 551; `test_op03_ac_draft_without_collision_is_confirmed_immediately`, `test_op03_v02_dc_hpc_draft_goes_to_pending_approval` |
| uložit výsledek | `COMMIT` při odchodu z `transaction()`; při UPDATE ještě proběhnou DB triggery | `db.py` ř. 57; `schema.sql` ř. 70, 107, 120 |
| vrátit výsledek | JSON: rezervace se stavem (200); formulář: flash + redirect na Moje rezervace | `views.py` ř. 259–263; `test_json_actions_and_status_codes` |

### A3. Chybová větev: konflikt → odmítnout

| Co říká v0.2 | Kde se podmínka zjistí | Kde se rozhodne výsledek | Co dostane volající |
| --- | --- | --- | --- |
| Interval koliduje s CONFIRMED nebo PENDING_APPROVAL → odmítnuto, rezervace zůstává DRAFT | `_blocking_overlaps()` (`services/reservations.py` ř. 286). Pojistka: trigger `trg_reservations_no_overlap` při UPDATE (`schema.sql` ř. 70) | `confirm()` vyhodí `ConflictError` (ř. 466–467), `transaction()` udělá `ROLLBACK` (`db.py` ř. 54). Chybu z triggeru převede `_atomic()` (ř. 148–157) na stejnou `ConflictError` | JSON: `409` + text chyby (`views.py` `_status()` ř. 59–65, `_run_action()` ř. 252). Formulář: flash s chybou, redirect. Stav zůstává DRAFT |

Doklad: `test_op03_collision_with_confirmed_is_rejected_and_stays_draft`, `test_op03_v02_collision_with_pending_approval_is_rejected`, běh `verify_c02.py` příklad V2b: `409 Termín se na této stanici překrývá … -> stav DRAFT`.

Rozdíl proti v0.2 jsme v této větvi nenašli. Jedna věc navíc:

| Specifikace | Implementace | Doklad |
| --- | --- | --- |
| OP-03 nekontroluje kompatibilitu vozu (to dělá OP-02) | `confirm()` ji kontroluje znovu (`check_vehicle_allowed`, BR-04) | `services/reservations.py` ř. 458 |

Chování to nemění, protože OP-02 by takový návrh vůbec nevytvořilo. Jde jen o pojistku.

### A4. Hlavní části implementace

Aplikace je malá, takže bereme moduly / jednu třídu.

| Část implementace | Typ / obsah | Role v tomto scénáři | Doklad |
| --- | --- | --- | --- |
| Přihlášení a CSRF | modul: `auth.py` (`load_logged_in_user`, `login_required`), `security.py` (`csrf_protect`) | pustí dál jen přihlášeného uživatele s platným CSRF tokenem | `auth.py` ř. 24–46, `security.py` ř. 21–28 |
| Web vrstva | modul: `views.py` (`confirm_reservation`, `_run_action`, `_status`, `_service`) | přijme požadavek, zavolá službu, výsledek převede na HTTP odpověď | `views.py` ř. 52–65, 244–269 |
| Rezervační logika | třída: `ReservationService` v `services/reservations.py` (+ funkce `requires_approval`, `_atomic`) | rozhodne o přechodu stavu a sama pustí SQL dotazy | `services/reservations.py` ř. 441–477 |
| Přístup k DB | modul: `db.py` (`get_db`, `connect`, `transaction`) | otevře spojení a řídí transakci `BEGIN IMMEDIATE / COMMIT / ROLLBACK` | `db.py` ř. 38–57, 79 |
| Databáze | SQLite soubor `instance/charging.db`, schéma a triggery v `schema.sql` | drží stav a podruhé hlídá BR-02 a povolené přechody | `schema.sql` ř. 34, 70, 107, 120 |

### A5. Stav, změna stavu a pravidlo

Stav

| Otázka | Odpověď | Doklad |
| --- | --- | --- |
| Kde je stav Reservation trvale uložen? | sloupec `reservations.state` v SQLite (`instance/charging.db`), u DC/HPC ještě `approval_deadline` | `schema.sql` ř. 34; `scripts/spike_persistence.py` (zápis → nové spojení → načtení) |
| Který kód rozhoduje/provádí přechod? | rozhoduje `ReservationService.confirm()` (DRAFT → CONFIRMED / PENDING_APPROVAL), provádí ho UPDATE ve stejné metodě. Triggery `trg_reservations_state_transition` a `trg_reservations_approval_policy` přechod ještě ověří. Přechod DRAFT → EXPIRED dělá `expire_overdue()` | `services/reservations.py` ř. 468–476, 171; `schema.sql` ř. 107, 120 |

Pravidlo BR-02

| Otázka | Odpověď | Doklad |
| --- | --- | --- |
| Kde se zjistí podmínka pravidla? | na dvou místech: `_blocking_overlaps()` ve službě a trigger `trg_reservations_no_overlap` v DB | `services/reservations.py` ř. 286; `schema.sql` ř. 70 |
| Kde se podle výsledku rozhodne? | služba: `confirm()` ř. 466–467 (`ConflictError`); DB: trigger `RAISE(ABORT, 'BLOCKING_OVERLAP')`, který `_atomic()` přeloží na `ConflictError` | `test_db_triggers_enforce_approval_policy_overlap_and_transitions` |
| Kde se provede výsledná změna stavu? | jen když konflikt není: UPDATE v `confirm()` ř. 470 / 476, uloží se `COMMIT`em. Při konfliktu `ROLLBACK`, stav se nemění | `db.py` ř. 50–57 |

Souběh (REQ-04) drží `BEGIN IMMEDIATE`: kontrola i zápis běží pod zápisovým zámkem, takže druhý request čeká a pak už konflikt vidí. Ověřeno testem `test_op03_req04_concurrent_confirms_allow_exactly_one` (10 vláken, 10 spojení, projde právě jedno).

### A6. Závislosti

| Závislost | Kde se napojuje na náš kód | Která část zná její technické API | Doklad |
| --- | --- | --- | --- |
| databáze SQLite | `db.connect()` / `get_db()`, spojení se předá do `ReservationService` | `db.py` (spojení, transakce) a hlavně `ReservationService`, který píše SQL přímo (žádná repository vrstva) | `db.py` ř. 38–57; `services/reservations.py` ř. 209, 286, 470 |
| Notification Service | v Confirm se nepoužívá. `confirm()` nevolá `_notify()`, notifikace jdou až z approve / reject / cancel | – | `services/reservations.py`: `_notify` jen na ř. 514, 522, 528, 562 |
| IdP | nemáme. Přihlášení je lokální (hash hesla v tabulce `users`, Flask session cookie podepsaná `SECRET_KEY`) | `auth.py` | `auth.py` ř. 24–46 |

### A7. AS-IS diagram

```
   prohlížeč / JSON klient
         |
         | POST /reservations/<id>/confirm  (session cookie + CSRF token)
         v
+------------------------------- Application code -------------------------------+
|                                                                                |
|  [Přihlášení a CSRF]  module                                                   |
|  role: pustí dál jen přihlášeného uživatele s platným CSRF tokenem -----+      |
|       |                                                                 |      |
|       | zavolá view                                                     |      |
|       v                                                    SELECT users |      |
|  [Web vrstva]  module                                                   |      |
|  role: přijme požadavek, vrátí 200 / 409 nebo flash + redirect          |      |
|       |                                |                                |      |
|       | confirm(user_id, id)           | get_db()                       |      |
|       v                                v                                |      |
|  [ReservationService]  class         [Přístup k DB]  module             |      |
|  role: rozhodne o přechodu stavu     role: spojení a transakce          |      |
|       |      --- transaction() --->    |                                |      |
|       |                                |                                |      |
|       |                                |                                |      |
+-------|--------------------------------|--------------------------------|------+
        | UPDATE expire, SELECT          | BEGIN IMMEDIATE /              |
        | reservation + overlaps,        | COMMIT / ROLLBACK              |
        | UPDATE state                   |                                |
        v                                v                                v
   [SQLite instance/charging.db]  database
   tabulky reservations, users; triggery BR-02 a povolených přechodů stavů
```

Co je v blocích:
- Přihlášení a CSRF: `auth.py` – `load_logged_in_user()`, `login_required`; `security.py` – `csrf_protect()`
- Web vrstva: `views.py` – `confirm_reservation()`, `_run_action()`, `_status()`, `_service()`
- ReservationService: `services/reservations.py` – `confirm()`, `expire_overdue()`, `_get_reservation_row()`, `_blocking_overlaps()`, `_set_state()`, funkce `requires_approval()`, `_atomic()`
- Přístup k DB: `db.py` – `get_db()`, `connect()`, `transaction()`
- SQLite: `schema.sql` – tabulka `reservations`, triggery `trg_reservations_no_overlap`, `trg_reservations_state_transition`, `trg_reservations_approval_policy`

Notification Service v diagramu schválně není, v tomhle scénáři se nevolá (viz A6).

### A8. Otázka pro další část C03

| Položka | Obsah |
| --- | --- |
| Otázka | Vydrží potvrzování 10× souběh (naše future pressure Q), když SQLite pustí najednou jen jeden zápis a každý confirm spouští dva zápisové příkazy (`expire_overdue()` a pak transakci)? |
| Doklad | `db.transaction()` začíná `BEGIN IMMEDIATE` (`db.py` ř. 50), `connect()` má `timeout=10` (ř. 40), `confirm()` volá `expire_overdue()` mimo transakci (`services/reservations.py` ř. 445). Test s 10 vlákny ověřuje jen správnost (projde právě jeden), ne čekání ani chyby `database is locked` při větší zátěži. |
| Proč je důležitá | BR-02 při souběhu dnes drží právě díky tomu, že se všechny zápisy řadí za sebe. Při 10× více požadavcích to může znamenat dlouhé čekání nebo chyby po `timeout=10`. Potřebujeme rozhodnout, jak BR-02 hlídat jinak (DB se zámky na řádky nebo exclusion constraint, fronta požadavků), aniž bychom ho porušili. |
