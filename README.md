# VoltBook
Toto je naše týmové repo do předmětu SWI. Výsledkem je funkční rezervační systém na fiktivní nabíjecí stanice.

**Projekt:** VoltBook
**Tým:** Jan Staněk, Dominik Volný, Šimon Exner
**Repozitář:** <https://github.com/Gangyss67/SWI_Charger_stations>

Webová aplikace pro rezervaci 10 nabíjecích stanic (z toho 3 vysokorychlostní jen pro plně elektrické vozy) po 30minutových slotech, s přihlašováním uživatelů, schvalováním rezervací rychlých stanic a in-app notifikacemi. Aplikace odpovídá **specifikaci baseline v0.2** ([docs/intent-and-change.md](docs/intent-and-change.md), sekce CV2).

## Stack

| Vrstva | Volba |
| --- | --- |
| Jazyk | Python 3.12+ (ověřeno na 3.14) |
| Web | Flask 3 (server-side šablony Jinja2 + JSON API) |
| Databáze | SQLite (součást Pythonu, soubor `instance/charging.db`) |
| Testy | pytest |

Zdůvodnění volby je v [docs/architecture-and-decisions.md](docs/architecture-and-decisions.md).

## Spuštění (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Linux / macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py
```

Aplikace běží na <http://127.0.0.1:5000>. Databáze se vytvoří automaticky při prvním startu (schéma + 10 stanic + demo účty).
Reset databáze = smazat soubor `instance/charging.db`. Databázi se starším schématem (C01) aplikace při startu sama přesune do zálohy `instance/charging.db.v0.bak` a založí novou.

### Demo účty

| Uživatel | Heslo | Vozidlo | Role |
| --- | --- | --- | --- |
| `admin` | `admin1234` | BEV | administrátor (schvaluje žádosti DC/HPC) |
| `ridic_ev` | `ev123456` | BEV | uživatel |
| `ridic_phev` | `phev1234` | PHEV | uživatel (nesmí na HPC nabíječky) |

Typ vozidla u účtu je jen výchozí hodnota – typ vozu a SPZ se zadávají u každé rezervace. Další účty lze vytvořit přes Registraci.

## Testy

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\verify_c02.py     # příklady ověření baseline v0.2 přes HTTP (exit 0 = OK)
.\.venv\Scripts\python.exe scripts\dispatch_notifications.py   # doručí nedoručené notifikace z outboxu
.\.venv\Scripts\python.exe scripts\measure_c04.py    # C04 – měření souběžného potvrzování, cca 1 min (exit 0 = kritérium splněno)
```

## Doména v kostce (baseline v0.2)

- **Resource:** nabíjecí stanice `AC 01–03` 11 kW, `AC 04–06` 22 kW, `DC 07` 50 kW, `HPC 08–10` 150 kW.
- **Reservation:** stanice + interval `[start, end)` po 30min slotech (1–8 slotů, max. 14 dní dopředu) + typ vozu + SPZ + stav.
- **Operace:** OP-01 Check Availability, OP-02 Create, OP-03 Confirm, OP-04 Cancel, OP-05 Approve / Reject (admin).
- **Stavy:** `DRAFT` (návrh, 5 min, neblokuje) → `CONFIRMED` (AC) nebo `PENDING_APPROVAL` (DC/HPC, blokuje, čeká na admina)
  → `CONFIRMED` / `REJECTED`; koncové `CANCELLED`, `CANCELLED_BY_ADMIN`, `REJECTED`, `EXPIRED`.
  Diagramy: [docs/diagrams/](docs/diagrams/).
- **BR-02 (common rule):** dvě rezervace v blokujícím stavu (`CONFIRMED`, `PENDING_APPROVAL`) stejné stanice se nesmí překrývat – služba i DB trigger.
- **BR-03:** uživatel ruší `CONFIRMED` nejpozději 30 min před začátkem, admin kdykoliv před koncem; žádost `PENDING_APPROVAL` lze stáhnout kdykoliv.
- **BR-04 (domain rule):** HPC nabíječky smí rezervovat pouze BEV, ne plug-in hybrid – služba i DB trigger.
- **BR-05 / BR-06:** `DRAFT` propadá po 5 min; o žádosti DC/HPC (≥ 50 kW) musí admin rozhodnout do `T_start − 15 min`, jinak `EXPIRED`.
- **Boundary:** Notification Service – výsledek schválení/zamítnutí, zrušení administrátorem, uvolněný (výkonnější) slot. Od C03 jdou události přes outbox (ADR-01): doručují se po každém POST, nedoručené zkusí znovu `scripts/dispatch_notifications.py`.

## HTTP rozhraní

| Metoda | URL | Operace |
| --- | --- | --- |
| GET | `/?date=&time=&slots=&vehicle_type=&license_plate=` | přehled stanic + „Nová rezervace“ podle aktivitního diagramu OP-01/02: krok 1 interval, typ vozu, SPZ → krok 2 seznam volných stanic (OP-01) → „Vytvořit návrh“ (OP-02) |
| GET | `/stations/<id>?date=YYYY-MM-DD` | dostupnost po slotech + formulář rezervace |
| POST | `/reservations` | OP-02 create → DRAFT (formulář nebo JSON); potvrzení je samostatná akce OP-03 |
| POST | `/reservations/<id>/confirm` | OP-03 DRAFT → CONFIRMED (AC) / PENDING_APPROVAL (DC/HPC) |
| POST | `/reservations/<id>/cancel` | OP-04 → CANCELLED / CANCELLED_BY_ADMIN |
| POST | `/reservations/<id>/approve` | OP-05 PENDING_APPROVAL → CONFIRMED (admin) |
| POST | `/reservations/<id>/reject` | OP-05 PENDING_APPROVAL → REJECTED (admin) |
| GET | `/admin/reservations?state=` | fronta schvalování a správa rezervací (admin) |
| GET | `/api/availability?date=&time=&slots=&vehicle_type=[&station_id=]` | OP-01 (JSON) |
| GET | `/api/stations/<id>/availability?date=` | sloty jedné stanice (JSON) |
| GET | `/api/reservations/<id>` | detail rezervace (vlastník nebo admin) |

Všechny POST požadavky vyžadují přihlášení a CSRF token (`csrf_token` ve formuláři nebo hlavička `X-CSRF-Token`).
Akce posílané jako JSON vrací rezervaci, případně chybu: `400` validace, `401` nepřihlášen, `403` cizí rezervace / chybí role Admin, `404` neexistuje, `409` kolize nebo nepovolený stav či lhůta.

## CP1 walking skeleton

Jedna end-to-end cesta, která je runnable (a implementovaná a testovaná):

```
POST /reservations   (JSON: {"station_id": 8, "date": "2026-09-15", "time": "10:00", "slots": 2,
                             "vehicle_type": "BEV", "license_plate": "1AB 2345"})
→ validate   přihlášený uživatel, CSRF, typy vstupu, SPZ, 30min slot, 1–8 slotů, začátek v budoucnu,
             horizont 14 dní, stanice existuje, HPC jen BEV (kolize se ověřují až při potvrzení – OP-03)
→ persist    INSERT do SQLite tabulky reservations ve stavu DRAFT s expirací +5 min (BEGIN IMMEDIATE)
→ return     201 Created + JSON rezervace včetně "id"  (400 validace / 401 nepřihlášen / 404 stanice)
→ automated check
             tests/test_web.py::test_cp1_walking_skeleton_post_reservation_returns_id_and_persists
             tests/test_web.py::test_cp1_validation_errors
```

Spuštění jen těchto testů:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_web.py -k cp1
```

## Struktura repozitáře

```
README.md
requirements.txt, pytest.ini, run.py
docs/
  intent-and-change.md          Project Frame + future pressure (C01), specifikace OP-01..OP-05,
                                BR-01..BR-06, baseline v0.1 a v0.2 + dopad změny (C02)
  architecture-and-decisions.md rozhodnutí, architektura (C03), D9 (C04) a detailní návrh (C05)
  evidence-and-evolution.md     evidence C01 (spike), C02, C03, C04 (měření) a C05
  diagrams/                     use case, stavové a aktivitní diagramy v0.1/v0.2 (C02),
                                diagramy architektury c03_* a detailního návrhu c05_* (PlantUML + PNG)
src/charging/
  __init__.py                   app factory
  schema.sql                    tabulky, CHECK constrainty, triggery (business pravidla v DB)
  db.py                         připojení, transakce (fronta zápisů, D9), seed dat
  auth.py, security.py          přihlášení/registrace, CSRF
  views.py                      stránky + JSON API
  services/reservations.py      ReservationService – operace OP-01..OP-05, transakce
  services/domain.py            stavy, typy vozů, BR-04 a BR-06
  services/policies.py          C05 – rozhodnutí o potvrzení a dostupnosti (bez DB)
  services/notifications.py     Notification Service (boundary)
  services/outbox.py            doručování událostí z outboxu (ADR-01)
  templates/, static/
scripts/spike_persistence.py    C01 spike A – persistence
scripts/verify_c02.py           C02 – příklady ověření baseline v0.2 přes HTTP
scripts/dispatch_notifications.py  C03 – opakované doručení notifikací z outboxu
scripts/measure_c04.py          C04 – měření souběžného potvrzování (kritérium K1–K3)
tests/                          pytest testy (test_architecture.py = pravidlo z ADR-01, test_confirmation_design.py = C05)
```
