# CV1 (C01)

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

---

# CV2 (C02)

## Evidence C02: specifikace → běžící aplikace

Přijatá baseline:
- Specification Baseline v0.2 (cv2.docx, bod 13) = v0.1 (OP-01–OP-04, BR-01–BR-05) + změna C02: rezervace rychlých stanic (DC ≥ 50 kW, HPC) schvaluje administrátor. Nová operace OP-05 Approve / Reject, nové BR-06 Approval policy, upravené BR-02 (blokující stavy CONFIRMED + PENDING_APPROVAL) a BR-03 (stažení žádosti), nové stavy PENDING_APPROVAL a REJECTED.

Předvedené základní operace (běžící server `python run.py`, HTTP + UI):
- OP-01 Check Availability – formulář „Nová rezervace“ na `/` přesně podle aktivitního diagramu OP-01/02 (krok 1: interval, typ vozu a SPZ; krok 2: seznam volných stanic, PHEV bez HPC; „Vytvořit návrh“ = OP-02), `GET /api/availability` (10 stanic, AVAILABLE/UNAVAILABLE + důvod `COLLISION` / `PENDING_APPROVAL` / `VEHICLE`, příznak „vyžaduje schválení“), sloty stanice `/stations/<id>`.
- OP-02 Create – `POST /reservations` → `201`, DRAFT s typem vozu, SPZ a `draft_expires_at` (+5 min); vrací ID a čas expirace.
- OP-03 Confirm – `POST /reservations/<id>/confirm`: AC → CONFIRMED, DC/HPC → PENDING_APPROVAL s `approval_deadline` (T_start − 15 min).
- OP-04 Cancel – `POST /reservations/<id>/cancel`: CONFIRMED → CANCELLED / CANCELLED_BY_ADMIN, PENDING_APPROVAL → CANCELLED.
- OP-05 Approve / Reject – záložka Schvalování `/admin/reservations` (fronta podle termínu rozhodnutí), `POST /reservations/<id>/approve|reject`.
- Ruční běh přes curl proti serveru (5. 10. 2026, rezervace na 6. 10.): OP-01 pro PHEV → HPC 08–10 `UNAVAILABLE VEHICLE`, DC 07 volná „vyžaduje schválení“ → OP-02 HPC 08 `201 DRAFT` → OP-03 `PENDING_APPROVAL`, lhůta 09:45 → OP-01 HPC 08 `UNAVAILABLE PENDING_APPROVAL` → schválení ne-adminem `403` → admin approve `CONFIRMED` + notifikace žadateli → admin cancel `CANCELLED_BY_ADMIN` → OP-01 `available: true`.

Skutečně provedené příklady ověření:
- `python scripts/verify_c02.py` – nové/změněné příklady V1–V9 z baseline v0.2 přes HTTP (řízené hodiny, „teď“ 15. 9. 2026 08:00), výsledek **14/14 OK**, exit 0:
  ```
  [OK] V1  OP-03 HPC 08 [10:00, 11:00) v 08:00          -> PENDING_APPROVAL, rozhodnout do 09:45
  [OK] V2  OP-01 BEV HPC 08 [10:30, 11:30)              -> UNAVAILABLE (PENDING_APPROVAL)
  [OK] V2b OP-03 kolidující návrh jiného uživatele      -> 409, zůstává DRAFT
  [OK] V3  OP-03 PHEV AC 01 [10:00, 10:30)              -> CONFIRMED (bez schválení)
  [OK] V5  OP-05 admin zamítne DC 07 [12:00, 13:00)     -> REJECTED, OP-01 AVAILABLE, notifikace
  [OK] V9  OP-05 schválení ne-adminem                   -> 403, zůstává PENDING_APPROVAL
  [OK] V4  OP-05 admin schválí v 09:00                  -> CONFIRMED, notifikace
  [OK] V7  OP-04 stažení žádosti v 09:40                -> CANCELLED (30min limit neplatí)
  [OK] V6  BR-06 nerozhodnuto do 09:45, v 09:46         -> EXPIRED, OP-01 AVAILABLE; pozdní approve 409
  [OK] V8  OP-03 DC 07 [10:00, 10:30) v 09:46           -> 409 nezbývá čas, zůstává DRAFT
  ```
- `pytest`: **73 passed** – 57 doménových testů (všechny příklady ověření OP-01–OP-05 z v0.1 i v0.2, hranice lhůt 5 min / 15 min / 30 min, DB triggery) a 16 webových (tok podle aktivitního diagramu OP-01/02, CSRF, role, CP1, JSON kódy 400/401/403/404/409).
- V10 / REQ-04 souběh: `test_op03_req04_concurrent_confirms_allow_exactly_one` – 10 vláken s 10 vlastními DB připojeními potvrzuje kolidující DRAFT současně → právě 1× PENDING_APPROVAL (HPC 08), resp. 1× CONFIRMED (AC 01), 9× odmítnuto.
- C01 spike `scripts/spike_persistence.py` upraven na API v0.2 – stále všechny kontroly OK (overlap blokuje trigger `BLOCKING_OVERLAP`).

Nalezený nesoulad a způsob vyřešení:
- Aplikace z C01 neodpovídala ani baseline v0.1: schvalovaly se všechny stanice (DRAFT → PENDING → CONFIRMED), kolize se kontrolovaly už při Create, typ vozu byl jen v profilu a chyběla SPZ, DRAFT neexpiroval, chyběl 30min storno limit i stav CANCELLED_BY_ADMIN. → Aplikace přegenerována podle baseline v0.2. Schéma DB má verzi 2 a starší databázi aplikace při startu přesune do zálohy `charging.db.v0.bak`.
- Mezera ve specifikaci: potvrzení DC/HPC méně než 15 min před začátkem by vytvořilo žádost, která je už po lhůtě. → Doplněno do BR-06 a OP-03: potvrzení se odmítne a rezervace zůstává DRAFT (V8).
- Rozpor v pracovní verzi bodů 13/15: „zrušení PENDING_APPROVAL > 30 min předem“ × stavový diagram „Cancel [User / Admin]“ bez podmínky. → BR-03 v0.2: stažení žádosti nemá 30min limit (V7).
- Kontrola UI proti docx (po předvedení): formulář vypadal jako „vyhledávání podle SPZ“, výsledky ukazovaly i obsazené stanice a tlačítko „Vytvořit a potvrdit“ slučovalo OP-02 a OP-03, které docx vede jako dvě samostatné akce uživatele. → UI upraveno přesně podle aktivitního diagramu OP-01/02: SPZ se zadává v kroku 1, ale dostupnost na ní nezávisí (OP-01 ji nepoužívá), zobrazí se jen volné stanice (PHEV bez HPC) a výběr stanice vytvoří jen DRAFT; potvrzení je samostatné tlačítko „Potvrdit“ (OP-03).
- Pracovní verze uváděla Administrátora jako nového aktéra, ale v diagramu případů užití je už ve v0.1. → Nový aktér nevzniká, Admin získává nový cíl OP-05. Lokální čísla pravidel v OP-01/OP-02 (BR-02 Compatibility, BR-03 Expiration) neodpovídala katalogu → v dotčených částech v0.2 odkazujeme na katalog (BR-04, BR-05).

Shrnutí dopadu změny:
- Nová operace OP-05 pro roli Admin, nové BR-06, upravené BR-02 a BR-03; OP-02, BR-01, BR-04, BR-05 beze změny.
- Životní cyklus rozšířen o PENDING_APPROVAL a REJECTED, EXPIRED nově i pro nerozhodnuté žádosti.
- OP-03 se větví podle výkonu stanice (AC × DC/HPC), OP-01 a OP-04 nově pracují s PENDING_APPROVAL.
- V aplikaci: endpointy confirm/approve/reject, fronta schvalování, DB triggery `BLOCKING_OVERLAP`, `APPROVAL_REQUIRED` a přechody stavů v0.2.
- UI přepracované (parkoviště jako řada stání, časová osa dne, SPZ jako česká značka, světlý i tmavý režim); logika baseline se tím nemění.

Zbývající předpoklad / neznámá:
- Expirace (BR-05, BR-06) je zatím „líná“ – zapíše se až při nejbližším požadavku na aplikaci a o propadnutí žádosti se žadatel nedozví notifikací. Pro C02 to stačí, řešení patří do C03 (AD-1).
- Předpokládáme, že admin rozhodne včas i mimo pracovní dobu; není určeno, zda má žádost po lhůtě eskalovat nebo se automaticky schválit (nyní propadne). Není určeno ani to, zda smí admin schválit vlastní žádost (nyní smí).
- Limity z C01 (max. 4 h, max. 14 dní dopředu) nejsou v baseline; v aplikaci zůstávají jako technická validace vstupu.

Architektonické drivery přenesené do C03:
- AD-1 Časově řízené přechody: DRAFT → EXPIRED (5 min) a PENDING_APPROVAL → EXPIRED (T_start − 15 min) i bez provozu, včetně uvolnění slotu a notifikace → plánovač / background worker.
- AD-2 Atomická alokace při souběhu: BR-02 nově hlídají dvě operace (Confirm, Approve) nad dvěma blokujícími stavy; při 10× souběhu (future pressure Q) SQLite serializuje všechny zápisy (`BEGIN IMMEDIATE`).
- AD-3 Spolehlivé asynchronní notifikace: výsledek schválení/zamítnutí a uvolnění slotu se nesmí ztratit ani blokovat či zvrátit transakci (dnes synchronně po commitu, chyba se jen zaloguje).
- AD-4 Autorizace podle role a dohledatelnost: OP-05 je první operace jen pro Admina – kontrola na serveru u každé operace; kdo a kdy rozhodl, se zatím neukládá.

Commit / tag aplikace:
- větev `c02-baseline-v0.2`, commit `578c0a6`, tag `v0.2.3` (stejná logika jako `v0.2.1`, nové UI; `v0.2.2` byl zamítnutý pokus s připouštěním podle výkonu vozidla, není součástí baseline)

---

# CV3 (C03)

## C03 — Architecture Evidence

Baseline: v0.2 (cv2.docx, aplikace `v0.2.3`)

Part A:
Část A jsme dělali v rámci tohoto C03. Krátké AS-IS shrnutí je v `docs/architecture-and-decisions.md` (C03 – Výchozí stav): stav mění jen `ReservationService`, BR-02 hlídá služba i trigger v `BEGIN IMMEDIATE` transakci a notifikace se volaly synchronně po COMMIT, při chybě se ztratily.

Drivers:
DR-1 souběh a BR-02, DR-2 schválení přijde později, DR-3 selhání Notification Service, DR-4 schvaluje jen admin (tabulka B).

Decision question:
Kde má být izolovaná integrace s Notification Service a co znamená její selhání pro Approve / Reject a Cancel?

Alternatives:
A – synchronní volání Notification Integration po COMMIT s pár pokusy v requestu. B – transakční outbox: událost se uloží ve stejné transakci jako změna stavu, doručuje ji `OutboxDispatcher` s opakováním.

Scenario walkthrough:
Confirm DC/HPC → PENDING_APPROVAL → admin schválí, Notification Service je nedostupná. V A se zpráva po pokusech ztratí, v B čeká v outboxu a doručí se později. V obou zůstane rezervace CONFIRMED. Při souběhu dvou schválení projde jedno a druhé dostane 409; v B zapíše událost jen úspěšná transakce (tabulka E3).

ADR:
ADR-01 – zvolena B (outbox). Negativa: zpoždění zprávy, doručení „aspoň jednou“, delší POST kvůli dispatchi v `after_request`, nová tabulka a skript.

Views:
- domain class – `docs/diagrams/c03_domain.png` (C1)
- context – G1 (ASCII v architecture-and-decisions.md)
- static architecture – `docs/diagrams/c03_static.png` (G2)
- state ownership – tabulka G3
- runtime/deployment – G4 (ASCII)
- design sequence – `docs/diagrams/c03_sequence.png` (H1, alt: selhání Notification Service)
- focused design class – `docs/diagrams/c03_design_class.png` (H2)

Cross-view issues found/resolved:
- Rezervace v kódu není třída (řádek tabulky) → v H2 označená jako «entity / tabulka».
- Skript pro doručení otevíral DB napřímo a na staré DB bez tabulky `outbox` by spadl → používá `create_app()`.
- `after_request` běží před odesláním odpovědi, takže doručení prodlouží POST → dopsáno do negativ ADR-01.
- Přechod do EXPIRED spouští každý request (líně), DR-2 tím není vyřešený celý → necháno jako riziko.

AS-IS → TO-BE delta:
CHANGE: volání Notification Service přes outbox, opakování při selhání, doručuje `OutboxDispatcher`, tabulka `outbox`, odstraněná závislost `ReservationService` → Notification Service. KEEP: změna stavu jen v `ReservationService`, BR-02 (`BEGIN IMMEDIATE` + trigger), kontrola role admina. VERIFY: pravidlo „RM nezná Notification Service“ → ověřeno testem (tabulka J).

Implementation changes:
`services/reservations.py` (`_emit()` místo `_notify()`, konstruktor bez `notifier`), nový `services/outbox.py`, `views.py` (dispatch po POST), `schema.sql` (tabulka `outbox`, verze 5), `scripts/dispatch_notifications.py`, testy `tests/test_reservations.py` a `tests/test_architecture.py`.

Behaviour verification:
`pytest` 77 passed (z toho nové: událost se zapíše v transakci a doručí později, vrácená operace nezapíše nic, při selhání událost čeká a další pokus ji doručí právě jednou). `scripts/verify_c02.py` 14/14 OK včetně notifikací po schválení a zamítnutí (V4b, V5c). Souběh: z 10 souběžných potvrzení projde jedno.

Architecture conformance rule + result:
Pravidlo: `services/reservations.py` nezná Notification Service, `NotificationService` volá jen `OutboxDispatcher`. Kontrola: `tests/test_architecture.py` (ast). Výsledek: 2 passed. Ověřeno i naopak: s dočasně přidaným `from .notifications import NotificationService` do `services/reservations.py` test spadl.

Remaining uncertainty / risk:
- Bez provozu se zprávy doručí až při spuštění `scripts/dispatch_notifications.py`. Cron jsme zatím nenastavili.
- Propadnutí (DR-2) je pořád „líné“ a k propadlé žádosti se neposílá zpráva.
- SQLite pouští jeden zápis najednou. Při 10× souběhu (future pressure Q) to může být úzké místo, zátěž jsme neměřili.
- U skutečného externího providera může zpráva přijít dvakrát (doručení aspoň jednou).

Commit/tag:
- větev `c02-baseline-v0.2`, commit `9f9a70c`, tag `v0.3.0`
