# SWI_Charger_stations
Toto je naše testovací repo do předmětu SWI. Výsledkem je funkční rezervační systém na fiktivní nabíjecí stanice.

Webová aplikace pro rezervaci 10 nabíjecích stanic (z toho 3 vysokorychlostní jen pro plně elektrické vozy) po 30minutových slotech, s přihlašováním uživatelů, schvalováním rezervací rychlých stanic a in-app notifikacemi. Aplikace odpovídá **specifikaci baseline v0.2** (cv2, změna C02).

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
- **Boundary:** Notification Service – výsledek schválení/zamítnutí, zrušení administrátorem, uvolněný (výkonnější) slot.

## HTTP rozhraní

| Metoda | URL | Operace |
| --- | --- | --- |
| GET | `/?date=&time=&slots=&vehicle_type=` | přehled stanic + OP-01 vyhledání volných stanic |
| GET | `/stations/<id>?date=YYYY-MM-DD` | dostupnost po slotech + formulář rezervace |
| POST | `/reservations` | OP-02 create (formulář nebo JSON); formulář s `action=confirm` provede i OP-03 |
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
  intent-and-change.md          Project Frame + future pressure
  architecture-and-decisions.md architektura a rozhodnutí
  evidence-and-evolution.md     evidence C01 (spike) a C02 (specifikace → běžící aplikace)
  diagrams/                     stavový, use case a aktivitní diagramy v0.2 (PlantUML + PNG)
src/charging/
  __init__.py                   app factory
  schema.sql                    tabulky, CHECK constrainty, triggery (business pravidla v DB)
  db.py                         připojení, transakce, seed dat
  auth.py, security.py          přihlášení/registrace, CSRF
  views.py                      stránky + JSON API
  services/reservations.py      doménová logika (stavy, pravidla, dostupnost)
  services/notifications.py     Notification Service (boundary)
  templates/, static/
scripts/spike_persistence.py    C01 spike A – persistence
scripts/verify_c02.py           C02 – příklady ověření baseline v0.2 přes HTTP
tests/                          pytest testy
```
