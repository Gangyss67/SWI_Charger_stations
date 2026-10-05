# C01 Engineering Spike

Varianta: **A – Persistence** (Reservation → skutečná DB → načtení → ověření)

Question / unknown:
Uloží se rezervace do skutečné databáze na disku tak, že po zavření spojení a otevření nového spojení načteme stejná data (stanici, čas, stav CONFIRMED)? A drží pravidlo „potvrzené rezervace stejné stanice se nepřekrývají“ přímo v databázi, i když zápis obejde aplikační logiku?

What we did:
- Napsali jsme skript [scripts/spike_persistence.py](../scripts/spike_persistence.py), který smaže a znovu vytvoří `instance/spike.db`, založí schéma, přes `ReservationService` vytvoří rezervaci na HPC 08 (2 sloty) a projde ji stavy DRAFT → PENDING → CONFIRMED, **zavře spojení**.
- Otevře nové spojení, načte řádek a zkontroluje stav, stanici a délku 60 min.
- Zkusí přímým SQL `INSERT` (mimo službu) vložit překrývající CONFIRMED rezervaci na stejnou stanici.
- Spuštěno příkazem `.\.venv\Scripts\python.exe scripts\spike_persistence.py` (Windows 11, Python 3.14.7). Stejný scénář je trvale v automatických testech (`tests/test_reservations.py::test_db_trigger_blocks_overlapping_confirmed_even_without_service`, `tests/test_web.py::test_cp1_walking_skeleton_post_reservation_returns_id_and_persists`).

Observed result:
```
[write] reservation id=1 created+submitted+approved, connection closed
[read ] {'id': 1, 'user_id': 2, 'station_id': 8, 'start_time': '2026-09-15 18:00', 'end_time': '2026-09-15 19:00', 'state': 'CONFIRMED', 'created_at': '2026-09-14 18:19:04', 'updated_at': '2026-09-14 18:19:04'}
[check] OK   row exists
[check] OK   state == CONFIRMED
[check] OK   station == 8 (HPC)
[check] OK   30min x 2 slots
[check] OK   overlap blocked by DB trigger
exit code: 0
```
Celá sada testů: `60 passed`.

Vedlejší zjištění při ověřování: první běh webových testů selhal (2 z 60), protože demo účet `ev` měl 2 znaky, zatímco registrace vyžaduje uživatelské jméno 3–32 znaků – seed data porušovala vlastní validaci. Demo účty jsme přejmenovali na `ridic_ev` / `ridic_phev`.

Decision / what changes because of the result:
- Persistence přes SQLite funguje a pro C02/CP1 u ní zůstáváme; walking skeleton `POST /reservations` ukládá do stejné DB.
- Overlap pravidlo a pravidlo „HPC jen BEV“ necháváme vynucené **i v DB triggeru**, protože spike potvrdil, že zachytí zápis, který obejde službu – služba zůstává hlavním místem se srozumitelnými chybami.
- Časy ukládáme jako řetězec `YYYY-MM-DD HH:MM`; porovnání řetězců v SQL se ve spiku chovalo správně, takže nepotřebujeme převod na epoch.
- Seed data musí splňovat stejné validace jako uživatelský vstup (poučení z vedlejšího zjištění).
- Riziko pro budoucnost: SQLite povoluje jednoho zapisujícího – souvisí s vybranou future pressure Q (viz `intent-and-change.md`).

## Evidence C02: specifikace → běžící aplikace

Přijatá baseline:
- Specification Baseline v0.2 (cv2.docx, bod 13) = v0.1 (OP-01–OP-04, BR-01–BR-05) + změna C02: rychlé stanice (DC 07 50 kW, HPC 08–10 150 kW) automaticky připustí vozidlo, které nabíjí aspoň polovinou jejich výkonu. Slabší vozidlo je dostane jen jako výjimku, kterou schválí administrátor. Vozidlo se vybírá z katalogu modelů se známým max. DC nabíjecím výkonem. Nová operace OP-05 Approve / Reject, nové BR-06, upravené BR-02 (blokující stavy CONFIRMED + PENDING_APPROVAL) a BR-03 (stažení žádosti), nové stavy PENDING_APPROVAL a REJECTED.

Předvedené základní operace (běžící server `python run.py`, HTTP + UI):
- OP-01 Check Availability – formulář „Nová rezervace“ na `/` podle aktivitního diagramu OP-01/02 (krok 1: termín, vozidlo z katalogu, SPZ; krok 2: volná stání, která vozidlo připustí, u rychlých stání „automaticky“ nebo „jen s výjimkou“), `GET /api/availability?…&vehicle_model_id=` (AVAILABLE/UNAVAILABLE, důvod `COLLISION` / `PENDING_APPROVAL` / `VEHICLE` / `NO_DC`, připuštění `AUTO` / `EXCEPTION`), časová osa dne na `/stations/<id>`.
- OP-02 Create – `POST /reservations` → `201`, DRAFT s vozidlem, SPZ a `draft_expires_at` (+5 min); vrací ID a čas expirace.
- OP-03 Confirm – `POST /reservations/<id>/confirm`: AC stanice nebo dost výkonné vozidlo → CONFIRMED, slabší vozidlo na DC/HPC → PENDING_APPROVAL s `approval_deadline` (T_start − 15 min).
- OP-04 Cancel – `POST /reservations/<id>/cancel`: CONFIRMED → CANCELLED / CANCELLED_BY_ADMIN, PENDING_APPROVAL → CANCELLED.
- OP-05 Approve / Reject – záložka Schvalování `/admin/reservations` (fronta výjimek podle termínu rozhodnutí, u každé vozidlo a jeho výkon proti požadavku stanice), `POST /reservations/<id>/approve|reject`.
- Běh serveru s finálním kódem (5. 10. 2026, samostatná DB): `GET /`, `/stations/8`, `/login`, `/register`, `/api/vehicle-models` → `200`; `GET /api/availability` pro VW e-up! (40 kW) → DC 07 `AUTO`, HPC 08–10 `EXCEPTION`.

Skutečně provedené příklady ověření:
- `python scripts/verify_c02.py` – nové a změněné příklady V1–V9 z baseline v0.2 přes HTTP (řízené hodiny, „teď“ 15. 9. 2026 08:00), výsledek **18/18 OK**, exit 0:
  ```
  [OK] V1  OP-03 Enyaq 135 kW potvrdí HPC 08 [10:00, 11:00)   -> CONFIRMED (bez schválení)
  [OK] V2  OP-03 e-up! 40 kW (< 75 kW) potvrdí HPC 09           -> PENDING_APPROVAL, rozhodnout do 09:45
  [OK] V2b OP-01 Enyaq na HPC 09 [10:30, 11:30)                -> UNAVAILABLE (PENDING_APPROVAL)
  [OK] V2c OP-03 kolidující návrh jiného uživatele             -> 409, zůstává DRAFT
  [OK] V3  OP-03 Outlander PHEV 22 kW (< 25 kW) potvrdí DC 07  -> PENDING_APPROVAL
  [OK] V3b OP-01 RAV4 Plug-in (jen AC): DC 07 / HPC 10 / AC 01 -> NO_DC / VEHICLE / AVAILABLE (AUTO)
  [OK] V3c OP-02 RAV4 Plug-in na DC 07                         -> 400
  [OK] V5  OP-05 admin zamítne výjimku DC 07                   -> REJECTED, OP-01 AVAILABLE, notifikace
  [OK] V9  OP-05 schválení ne-adminem                          -> 403, zůstává PENDING_APPROVAL
  [OK] V4  OP-05 admin schválí výjimku v 09:00                 -> CONFIRMED, notifikace
  [OK] V7  OP-04 stažení žádosti v 09:40                       -> CANCELLED
  [OK] V6  BR-06 nerozhodnuto do 09:45, v 09:46                -> EXPIRED, OP-01 AVAILABLE; pozdní approve 409
  [OK] V8  OP-03 e-up! na HPC 10 v 09:46                       -> 409 nezbývá čas, zůstává DRAFT
  [OK] V8b OP-03 Enyaq na tentýž slot v 09:46                  -> CONFIRMED (výjimka není potřeba)
  ```
- `pytest`: **82 passed** – 66 doménových testů (všechny příklady ověření OP-01–OP-05 z v0.1 i v0.2, hranice „polovina výkonu stanice“ 24/25 kW a 74/75 kW, lhůty 5 / 15 / 30 min, DB triggery) a 16 webových (tok podle aktivitního diagramu OP-01/02, CSRF, role, CP1, JSON kódy 400/401/403/404/409).
- V10 / REQ-04 souběh: `test_op03_req04_concurrent_confirms_allow_exactly_one` – 10 vláken s 10 vlastními DB připojeními potvrzuje kolidující DRAFT současně → právě 1× CONFIRMED (AC 01, Enyaq), resp. 1× PENDING_APPROVAL (HPC 08, e-up!), 9× odmítnuto.
- C01 spike `scripts/spike_persistence.py` upraven na API v0.2 – stále všechny kontroly OK.

Nalezený nesoulad a způsob vyřešení:
- Aplikace z C01 neodpovídala ani baseline v0.1: schvalovaly se všechny stanice (DRAFT → PENDING → CONFIRMED), kolize se kontrolovaly už při Create, typ vozu byl jen v profilu a chyběla SPZ, DRAFT neexpiroval, chyběl 30min storno limit i stav CANCELLED_BY_ADMIN. → Aplikace přegenerována podle baseline v0.2. Databázi se starším schématem aplikace při startu přesune do zálohy.
- Pracovní podmínka C02 „každou rezervaci DC/HPC schvaluje administrátor“ neměla věcný důvod: admin by schvaloval i vozidla, která rychlou nabíječku plně využijí. → Podmínka upřesněna: o připuštění rozhoduje max. DC výkon vozidla z katalogu (aspoň polovina výkonu stanice = automaticky, méně = výjimka se schválením, bez DC = nesmí). Změna se tím dotkla i OP-02 (vozidlo z katalogu místo ručně zadaného typu); bod 12 i 13 v docx jsou upravené.
- Mezera ve specifikaci: potvrzení výjimky méně než 15 min před začátkem by vytvořilo žádost, která je už po lhůtě. → Doplněno do BR-06 a OP-03: potvrzení se odmítne a rezervace zůstává DRAFT (V8); vozidlo bez potřeby výjimky tuto lhůtu nemá (V8b).
- Rozpor v pracovní verzi bodů 13/15: „zrušení PENDING_APPROVAL > 30 min předem“ × stavový diagram „Cancel [User / Admin]“ bez podmínky. → BR-03 v0.2: stažení žádosti nemá 30min limit (V7).
- Kontrola UI proti docx: formulář vypadal jako „vyhledávání podle SPZ“, výsledky ukazovaly i obsazená stání a tlačítko „Vytvořit a potvrdit“ slučovalo OP-02 a OP-03. → UI upraveno přesně podle aktivitního diagramu OP-01/02 (SPZ se zadává v kroku 1, ale dostupnost na ní nezávisí; zobrazí se jen stání, která vozidlo může použít; výběr vytvoří jen DRAFT, potvrzení je samostatná akce OP-03).
- Pracovní verze uváděla Administrátora jako nového aktéra, ale v diagramu případů užití je už ve v0.1. → Nový aktér nevzniká, Admin získává nový cíl OP-05. Lokální čísla pravidel v OP-01/OP-02 (BR-02 Compatibility, BR-03 Expiration) neodpovídala katalogu → v dotčených částech v0.2 odkazujeme na katalog (BR-04, BR-05).

Shrnutí dopadu změny:
- Nová operace OP-05 (rozhodnutí o výjimce) pro roli Admin, nové BR-06, upravené BR-02 a BR-03; OP-02 nově vybírá vozidlo z katalogu; BR-01, BR-04 a BR-05 beze změny.
- Nový persistentní pojem: katalog modelů vozidel (pohon, max. DC výkon).
- Životní cyklus rozšířen o PENDING_APPROVAL (žádost o výjimku) a REJECTED, EXPIRED nově i pro nerozhodnuté výjimky.
- OP-03 se větví podle stanice a vozidla, OP-01 vrací i způsob připuštění, OP-04 umí stáhnout žádost.
- V aplikaci: katalog vozidel, endpointy confirm / approve / reject, fronta výjimek, DB triggery `BLOCKING_OVERLAP`, `DC_NOT_SUPPORTED`, `APPROVAL_REQUIRED` a přechody stavů v0.2, nové UI.

Zbývající předpoklad / neznámá:
- Expirace (BR-05, BR-06) je zatím „líná“ – zapíše se až při nejbližším požadavku na aplikaci a o propadnutí žádosti se žadatel nedozví notifikací. Pro C02 to stačí, řešení patří do C03 (AD-1).
- Hodnoty max. DC výkonu v katalogu jsou orientační údaje výrobců a předpokládáme, že uživatel vybere model, který odpovídá SPZ. Ověření vozidla na místě (Unknown z Project Frame) zůstává otevřené.
- Hranice „polovina výkonu stanice“ je volba týmu, s provozovatelem parkoviště není ověřená.
- Předpokládáme, že admin rozhodne včas i mimo pracovní dobu; není určeno, zda má výjimka po lhůtě eskalovat, ani zda smí admin schválit vlastní žádost (nyní smí).
- Limity z C01 (max. 4 h, max. 14 dní dopředu) nejsou v baseline; v aplikaci zůstávají jako technická validace vstupu.

Architektonické drivery přenesené do C03:
- AD-1 Časově řízené přechody: DRAFT → EXPIRED (5 min) a PENDING_APPROVAL → EXPIRED (T_start − 15 min) i bez provozu, včetně uvolnění slotu a notifikace → plánovač / background worker.
- AD-2 Atomická alokace při souběhu: BR-02 nově hlídají dvě operace (Confirm, Approve) nad dvěma blokujícími stavy; při 10× souběhu (future pressure Q) SQLite serializuje všechny zápisy (`BEGIN IMMEDIATE`).
- AD-3 Spolehlivé asynchronní notifikace: výsledek výjimky a uvolnění slotu se nesmí ztratit ani blokovat či zvrátit transakci (dnes synchronně po commitu, chyba se jen zaloguje).
- AD-4 Autorizace podle role a dohledatelnost: OP-05 je první operace jen pro Admina – kontrola na serveru u každé operace; kdo a kdy výjimku udělil, se zatím neukládá.

Commit / tag aplikace:
- větev `c02-baseline-v0.2`, commit `9704809`, tag `v0.2.2` (starší tagy `v0.2.0` a `v0.2.1` jsou pracovní verze se schvalováním všech DC/HPC rezervací)
