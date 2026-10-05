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

## C03 část A – jak funguje Confirm Reservation

Vybrali jsme Confirm Reservation, protože se tam rozhoduje i o schvalování (AC stanice se potvrdí hned, DC a HPC čekají na admina).

### A1. Scénář

| Položka | Hodnota |
| --- | --- |
| Scénář | Confirm Reservation (OP-03) |
| Požadavky | REQ-03, REQ-04 |
| Pravidla | BR-02, BR-06 |
| Baseline | v0.2 |

### A2. Hlavní průchod

| Krok | Kde to je v kódu |
| --- | --- |
| přijmout požadavek | `views.py` – `confirm_reservation()`, ta zavolá `_run_action()` |
| zkontrolovat přihlášení | `auth.py` – `login_required` (a CSRF v `security.py`) |
| načíst rezervaci | `ReservationService.confirm()` – `_get_reservation_row()` |
| ověřit, že jde potvrdit | `confirm()` – musí to být naše rezervace ve stavu DRAFT a nesmí být po 5 minutách |
| zjistit konflikt | `confirm()` – `_blocking_overlaps()` hledá jinou CONFIRMED nebo PENDING_APPROVAL rezervaci ve stejném čase |
| změnit stav | `confirm()` – AC stanice → CONFIRMED, DC/HPC → PENDING_APPROVAL |
| uložit | `db.py` – `transaction()`, celé to běží v `BEGIN IMMEDIATE` a na konci je `COMMIT` |

Ověřili jsme to testy `test_op03_...` v `tests/test_reservations.py`.

### A3. Chybová větev – konflikt

| Co říká v0.2 | Kde se to zjistí | Kde se rozhodne | Co dostane uživatel |
| --- | --- | --- | --- |
| když se termín překrývá, potvrzení se odmítne a rezervace zůstane DRAFT | `_blocking_overlaps()` v `ReservationService` | `confirm()` vyhodí `ConflictError`, transakce se vrátí (`ROLLBACK`) | v API chyba 409, ve webu červená hláška. Rezervace zůstane DRAFT |

Zkoušeli jsme to i ve skriptu `scripts/verify_c02.py` (příklad V2b vrátil 409).

Rozdíl proti specifikaci jsme nenašli. Jen `confirm()` ještě jednou kontroluje, že PHEV nejede na HPC, i když to specifikace u OP-03 nepíše (kontroluje se to už při vytvoření).

### A4. Hlavní části

| Část | Co tam je | Co dělá |
| --- | --- | --- |
| Web | `views.py`, `auth.py`, `security.py` | přijme požadavek, zkontroluje přihlášení a vrátí odpověď |
| ReservationService | `services/reservations.py` | rozhodne, jestli se potvrdí, a pouští SQL dotazy |
| Práce s DB | `db.py` | otevírá spojení a dělá transakce |
| Databáze | SQLite `instance/charging.db`, `schema.sql` | ukládá rezervace, triggery hlídají překryv |

### A5. Stav a pravidlo

- **Kde je stav:** v tabulce `reservations`, sloupec `state` (SQLite).
- **Kdo mění stav:** `ReservationService.confirm()`, udělá `UPDATE` na CONFIRMED nebo PENDING_APPROVAL.
- **Pravidlo BR-02 (žádný překryv):** hlídá se dvakrát. Jednou ve službě (`_blocking_overlaps()`), podruhé v databázi triggerem `trg_reservations_no_overlap`. Když najde překryv, stav se nezmění.

Souběh řeší `BEGIN IMMEDIATE`. Máme test, kde 10 lidí potvrzuje najednou a projde jen jeden (`test_op03_req04_concurrent_confirms_allow_exactly_one`).

### A6. Závislosti

| Závislost | Kde | Poznámka |
| --- | --- | --- |
| SQLite | `db.py`, SQL je přímo v `ReservationService` | jiná závislost tu není |
| Notification Service | – | při potvrzení se nevolá, posílá se až při schválení, zamítnutí nebo zrušení |
| IdP | – | nemáme, přihlášení je jen naše (tabulka `users`) |

### A7. Diagram

```
  uživatel (prohlížeč)
        |
        | POST /reservations/<id>/confirm
        v
+--------------------- Application code ---------------------+
|                                                            |
|  [Web]  views.py, auth.py                                  |
|  přijme požadavek, zkontroluje přihlášení                  |
|        |                                                   |
|        | confirm()                                         |
|        v                                                   |
|  [ReservationService]                                      |
|  rozhodne o potvrzení                                      |
|        |                                                   |
|        | transaction()                                     |
|        v                                                   |
|  [db.py]                                                   |
|  spojení a transakce                                       |
|                                                            |
+------------------------------------------------------------+
        |
        | SELECT / UPDATE reservations
        v
  [SQLite databáze]  (triggery hlídají překryv)
```

Notification Service v diagramu není, protože se při potvrzení nepoužívá.

### A8. Otázka na příště

| | |
| --- | --- |
| Otázka | Zvládne to SQLite, když bude chtít potvrzovat 10× víc lidí najednou? |
| Proč si to myslíme | `BEGIN IMMEDIATE` pustí najednou jen jeden zápis, ostatní čekají (max 10 s, `timeout=10` v `db.py`). Test s 10 vlákny jen ukáže, že projde jeden, ale neměří, jak dlouho to trvá. |
| Proč je to důležité | Kvůli future pressure (10× víc rezervací najednou) a BR-02 nesmí přestat platit. |
