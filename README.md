# SWI_Charger_stations
Toto je naše testovací repo do předmětu SWI. Výsledkem je funkční rezervační systém na fiktivní nabíjecí stanice.

Webová aplikace pro rezervaci 10 nabíjecích stanic (z toho 3 vysokorychlostní jen pro plně elektrické vozy) po 30minutových slotech, s přihlašováním uživatelů, schvalováním rezervací a in-app notifikacemi.

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
Reset databáze = smazat soubor `instance/charging.db`.

### Demo účty

| Uživatel | Heslo | Vozidlo | Role |
| --- | --- | --- | --- |
| `admin` | `admin1234` | BEV | administrátor (schvaluje rezervace) |
| `ridic_ev` | `ev123456` | BEV | uživatel |
| `ridic_phev` | `phev1234` | PHEV | uživatel (nesmí na HPC nabíječky) |

Další účty lze vytvořit přes Registraci.

## Testy

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Doména v kostce

- **Resource:** nabíjecí stanice (`AC 01–03` 11 kW, `AC 04–06` 22 kW, `DC 07` 50 kW, `HPC 08–10` 150 kW vysokorychlostní).
- **Reservation:** stanice + čas (30min sloty, 1–8 slotů, max. 14 dní dopředu) + stav.
- **Stavy:** `DRAFT → PENDING → CONFIRMED`, z kteréhokoli aktivního stavu `→ CANCELLED`.
- **Stav slotu/stanice:** volná / rezervovaná / aktuálně používaná.
- **Operace:** create, submit, confirm/approve (admin), cancel, check availability.
- **Common rule:** dvě `CONFIRMED` rezervace stejné stanice se nesmí překrývat (vynuceno ve službě i DB triggerem).
- **Domain rule:** vysokorychlostní nabíječky smí rezervovat pouze BEV, ne plug-in hybrid (služba i DB trigger).
- **Boundary:** Notification Service – upozorní např. na uvolnění výkonnější nabíječky.

## HTTP rozhraní

| Metoda | URL | Popis |
| --- | --- | --- |
| GET | `/` | přehled stanic a jejich aktuální stav |
| GET | `/stations/<id>?date=YYYY-MM-DD` | dostupnost po slotech + formulář rezervace |
| POST | `/reservations` | create (formulář nebo JSON) |
| POST | `/reservations/<id>/submit` | DRAFT → PENDING |
| POST | `/reservations/<id>/approve` | PENDING → CONFIRMED (admin) |
| POST | `/reservations/<id>/cancel` | → CANCELLED |
| GET | `/api/stations/<id>/availability?date=` | dostupnost slotů (JSON) |
| GET | `/api/availability/check?station_id=&date=&time=&slots=` | check availability (JSON) |

Všechny POST požadavky vyžadují přihlášení a CSRF token (`csrf_token` ve formuláři nebo hlavička `X-CSRF-Token`).

## CP1 walking skeleton

Jedna end-to-end cesta, která je runnable (a už teď implementovaná a testovaná):

```
POST /reservations   (JSON: {"station_id": 8, "date": "2026-09-15", "time": "10:00", "slots": 2})
→ validate   přihlášený uživatel, CSRF, typy vstupu, 30min slot, 1–8 slotů, ne v minulosti,
             horizont 14 dní, stanice existuje, HPC jen BEV, nepřekrývá se s CONFIRMED
→ persist    INSERT do SQLite tabulky reservations ve stavu DRAFT (v transakci BEGIN IMMEDIATE)
→ return     201 Created + JSON rezervace včetně "id"  (400 validace / 401 nepřihlášen / 404 stanice / 409 kolize)
→ automated check
             tests/test_web.py::test_cp1_walking_skeleton_post_reservation_returns_id_and_persists
             (ověří 201 + id a že řádek v DB má správnou stanici, čas i stav)
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
  evidence-and-evolution.md     C01 engineering spike – evidence
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
tests/                          pytest testy
```
