# VoltBook
Toto je naše týmové repo do předmětu SWI. Výsledkem je funkční rezervační systém na fiktivní nabíjecí stanice.

**Projekt:** VoltBook
**Tým:** Jan Staněk, Dominik Volný, Šimon Exner
**Repozitář:** <https://github.com/Gangyss67/SWI_Charger_stations>

Webová aplikace pro rezervaci 10 nabíjecích stanic (z toho 3 vysokorychlostní jen pro plně elektrické vozy) po 30minutových slotech, s přihlašováním uživatelů, schvalováním rezervací rychlých stanic, profilem uložených vozidel, kontrolou SPZ a notifikacemi v aplikaci i na telefonu. Aplikace odpovídá **specifikaci baseline v0.2** ([docs/intent-and-change.md](docs/intent-and-change.md), sekce CV2).

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
Reset databáze = smazat soubor `instance/charging.db`. Databázi ze schématu verze 5 (C03–C10) aplikace při startu sama doplní na verzi 6 (vozidla, stát SPZ, notifikace na telefon), data zůstanou. Ještě starší databázi (C01, C02) přesune do zálohy `instance/charging.db.v<verze>.bak` a založí novou.

### Demo účty

| Uživatel | Heslo | Vozidlo | Role |
| --- | --- | --- | --- |
| `admin` | `admin1234` | BEV | administrátor (schvaluje žádosti DC/HPC) |
| `ridic_ev` | `ev123456` | BEV | uživatel |
| `ridic_phev` | `phev1234` | PHEV | uživatel (nesmí na HPC nabíječky) |

Typ vozidla u účtu je jen výchozí hodnota – typ vozu a SPZ se zadávají u každé rezervace. Další účty lze vytvořit přes Registraci.

### Quality of life improvements

Přehled všech vylepšení po C10 je v [docs/evidence-and-evolution.md](docs/evidence-and-evolution.md) (Quality of life improvements).

**Výběr termínu na časové ose:** na stránce stání se termín vybírá přímo na ose dne. Myší stisknete začátek a táhnete, na mobilu klepnete na začátek a pak na konec. Začátek a délka se propíšou do formuláře.

#### Profil: vozidla a notifikace na telefon

V **Profil** si uživatel uloží svá auta (název, typ BEV/PHEV, stát a SPZ). Při rezervaci pak jen vybere vozidlo, typ a SPZ se doplní samy. Na HPC stání nejdou vybrat plug-in hybridy (BR-04). Rezervace si údaje kopíruje, takže smazání vozidla historii nemění.

SPZ se kontroluje podle státu ([src/charging/services/plates.py](src/charging/services/plates.py)): česká běžná `1AB 2345` (druhý znak je kód kraje), elektromobil `EL 123AB`, značka na přání 5–8 písmen a číslic (aspoň jedna číslice). U SK, PL, D, A, H jen obecně (písmena, číslice, mezera, pomlčka).

**Notifikace na telefon** jdou přes [ntfy](https://ntfy.sh) – open-source push službu s aplikací pro Android i iPhone, bez registrace a bez API klíče:
1. Každý účet má **jedno stálé téma** ve tvaru `voltbook-xxxx-xxxx-xxxx` (funguje jako heslo), v Profilu si zapnete posílání. Zapnutí ani vypnutí téma nemění.
2. Naskenujte telefonem **QR kód** z Profilu. Otevře se stránka tématu na ntfy, kde povolíte notifikace. Nebo si nainstalujte aplikaci **ntfy** (Google Play, F-Droid, App Store), klepněte na **+** a opište téma (jen malá písmena a číslice).
3. Tlačítkem *Poslat zkušební notifikaci* si to ověřte. Když nepřijde, telefon odebírá jiné téma – smažte odběr a přidejte ho znovu.

Na telefon pak přijde totéž co do Notifikací v aplikaci, jen bez SPZ:

| Kdy | Kdo dostane zprávu |
| --- | --- |
| řidič potvrdí rezervaci DC/HPC (nová žádost) | všichni administrátoři kromě žadatele |
| admin žádost schválí / zamítne | žadatel |
| o žádosti nikdo nerozhodne do T_start − 15 min | žadatel |
| admin zruší cizí rezervaci | vlastník |
| uvolní se slot | vlastníci návrhů na stejném stání a řidiči na slabším stání ve stejném čase |

Nechodí při vytvoření návrhu, potvrzení na AC stání ani při vlastním zrušení. Server nastavuje proměnná `NTFY_URL` (výchozí `https://ntfy.sh`, může to být i vlastní ntfy server, prázdná hodnota = funkce vypnutá).

## Testy

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\verify_c02.py     # příklady ověření baseline v0.2 přes HTTP (exit 0 = OK)
.\.venv\Scripts\python.exe scripts\verify_c08.py     # C08/C09 – test casy TC-C08-01..03 přes HTTP + kontrola stavu přímo v DB (exit 0 = OK)
.\.venv\Scripts\python.exe scripts\dispatch_notifications.py   # doručí nedoručené notifikace z outboxu
.\.venv\Scripts\python.exe scripts\measure_c04.py    # C04 – měření souběžného potvrzování, cca 1 min (exit 0 = kritérium splněno)
```

Testy si DB zakládají samy v dočasném adresáři (`verify_c02.py` v `instance/verify_c02.db`), nic dalšího se nastavovat nemusí.
Smoke test po spuštění `run.py`: <http://127.0.0.1:5000/login> vrátí přihlašovací stránku.

**CI (C09):** [.github/workflows/ci.yml](.github/workflows/ci.yml) při každém push a PR na GitHubu (Ubuntu, Python 3.14) nainstaluje `requirements.txt` a pustí `pytest`, `verify_c02.py` a `verify_c08.py` – stejné příkazy jako výše. `measure_c04.py` v CI není, na sdíleném stroji by čísla nic neříkala.

## Doména v kostce (baseline v0.2)

- **Resource:** nabíjecí stanice `AC 01–03` 11 kW, `AC 04–06` 22 kW, `DC 07` 50 kW, `HPC 08–10` 150 kW.
- **Reservation:** stanice + interval `[start, end)` po 30min slotech (1–8 slotů, max. 14 dní dopředu) + typ vozu + SPZ (+ stát) + stav.
- **Operace:** OP-01 Check Availability, OP-02 Create, OP-03 Confirm, OP-04 Cancel, OP-05 Approve / Reject (admin).
- **Stavy:** `DRAFT` (návrh, 5 min, neblokuje) → `CONFIRMED` (AC) nebo `PENDING_APPROVAL` (DC/HPC, blokuje, čeká na admina)
  → `CONFIRMED` / `REJECTED`; koncové `CANCELLED`, `CANCELLED_BY_ADMIN`, `REJECTED`, `EXPIRED`.
  Diagramy: [docs/diagrams/](docs/diagrams/).
- **BR-02 (common rule):** dvě rezervace v blokujícím stavu (`CONFIRMED`, `PENDING_APPROVAL`) stejné stanice se nesmí překrývat – služba i DB trigger.
- **BR-03:** uživatel ruší `CONFIRMED` nejpozději 30 min před začátkem, admin kdykoliv před koncem; žádost `PENDING_APPROVAL` lze stáhnout kdykoliv.
- **BR-04 (domain rule):** HPC nabíječky smí rezervovat pouze BEV, ne plug-in hybrid – služba i DB trigger.
- **BR-05 / BR-06:** `DRAFT` propadá po 5 min; o žádosti DC/HPC (≥ 50 kW) musí admin rozhodnout do `T_start − 15 min`, jinak `EXPIRED`.
- **Boundary:** Notification Service – výsledek schválení/zamítnutí, zrušení administrátorem, uvolněný (výkonnější) slot, od C09 i propadlá žádost (CR-08), z QoL nová žádost administrátorům. Od C03 jdou události přes outbox (ADR-01): doručují se po každém POST, nedoručené zkusí znovu `scripts/dispatch_notifications.py`.

## HTTP rozhraní

| Metoda | URL | Operace |
| --- | --- | --- |
| GET | `/?date=&time=&slots=&vehicle_type=&license_plate=` | přehled stanic + „Nová rezervace“ podle aktivitního diagramu OP-01/02: krok 1 interval, typ vozu, SPZ → krok 2 seznam volných stanic (OP-01) → „Vytvořit návrh“ (OP-02) |
| GET | `/stations/<id>?date=YYYY-MM-DD` | dostupnost po slotech + formulář rezervace |
| POST | `/reservations` | OP-02 create → DRAFT (formulář nebo JSON); místo `vehicle_type` + `license_plate` [+ `plate_country`] jde poslat `vehicle_id` uloženého vozidla; potvrzení je samostatná akce OP-03 |
| POST | `/reservations/<id>/confirm` | OP-03 DRAFT → CONFIRMED (AC) / PENDING_APPROVAL (DC/HPC) |
| POST | `/reservations/<id>/cancel` | OP-04 → CANCELLED / CANCELLED_BY_ADMIN |
| POST | `/reservations/<id>/approve` | OP-05 PENDING_APPROVAL → CONFIRMED (admin) |
| POST | `/reservations/<id>/reject` | OP-05 PENDING_APPROVAL → REJECTED (admin) |
| GET | `/admin/reservations?state=` | fronta schvalování a správa rezervací (admin) |
| GET | `/api/availability?date=&time=&slots=&vehicle_type=[&station_id=]` | OP-01 (JSON) |
| GET | `/api/stations/<id>/availability?date=` | sloty jedné stanice (JSON) |
| GET | `/api/reservations/<id>` | detail rezervace (vlastník nebo admin) |
| GET | `/profile` | profil: uložená vozidla a notifikace na telefon |
| POST | `/profile/vehicles`, `/profile/vehicles/<id>/default`, `/profile/vehicles/<id>/delete` | přidat vozidlo / nastavit výchozí / smazat |
| POST | `/profile/push/enable`, `/profile/push/disable`, `/profile/push/test` | zapnout (nové téma) / vypnout / zkušební notifikace na telefon |

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
.github/workflows/ci.yml        C09 – CI (pytest + verify_c02 + verify_c08)
docs/
  intent-and-change.md          Project Frame + future pressure (C01), specifikace OP-01..OP-05,
                                BR-01..BR-06, baseline v0.1 a v0.2 + dopad změny (C02), change request CR-08 (C08)
  architecture-and-decisions.md rozhodnutí, architektura (C03), D9 (C04) a detailní návrh (C05)
  evidence-and-evolution.md     evidence C01 (spike), C02, C03, C04 (měření), C05, C06 (reakce na nálezy),
                                C07 (ověření nálezů, rizika), C08 (baseline a test casy),
                                C09 (integrace, CI, release), C10 (provozní situace a logy), quality of life improvements
  assessment_form.md            C06 – hodnocení od týmu ParkingCrew + naše disposition
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
  services/notifications.py     Notification Service (boundary), kanály: in-app schránka + telefon (ntfy)
  services/outbox.py            doručování událostí z outboxu (ADR-01)
  services/plates.py            kontrola a sjednocení SPZ podle státu
  services/vehicles.py          profil vozidel uživatele
  qr.py                         QR kód (SVG) pro nastavení notifikací na telefonu, bez knihovny
  topics.py                     stálé téma pro notifikace na telefon (ntfy)
  static/timeline.js            výběr termínu tažením na časové ose stání
  templates/, static/
scripts/spike_persistence.py    C01 spike A – persistence
scripts/verify_c02.py           C02 – příklady ověření baseline v0.2 přes HTTP
scripts/verify_c08.py           C08 – krokované test casy TC-C08-01/02 (BR-02), C09 – TC-C08-03 (CR-08)
scripts/dispatch_notifications.py  C03 – opakované doručení notifikací z outboxu
scripts/measure_c04.py          C04 – měření souběžného potvrzování (kritérium K1–K3)
tests/                          pytest testy (test_architecture.py = pravidlo z ADR-01, test_confirmation_design.py = C05,
                                test_profile.py = SPZ, vozidla, notifikace na telefon, migrace DB,
                                test_qr.py = QR kód, test_timeline_js.py = výběr na časové ose přes node)
```
