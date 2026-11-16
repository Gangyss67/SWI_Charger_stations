# CV1 (C01)

# C01 Engineering Spike

Varianta: **A – Persistence** (Reservation → skutečná DB → načtení → ověření)

Question / unknown:
Uloží se rezervace do skutečné databáze na disku tak, že po zavření spojení a otevření nového spojení načteme stejná data (stanici, čas, stav CONFIRMED)? A drží pravidlo „potvrzené rezervace stejné stanice se nepřekrývají“ přímo v databázi, i když zápis obejde aplikační logiku?

What we did:
- Napsali jsme skript [scripts/spike_persistence.py](../scripts/spike_persistence.py), který smaže a znovu vytvoří `instance/spike.db`, založí schéma, přes `ReservationService` vytvoří rezervaci na HPC 08 (2 sloty) a projde ji stavy DRAFT → PENDING → CONFIRMED, **zavře spojení**.
- Otevře nové spojení, načte řádek a zkontroluje stav, stanici a délku 60 min.
- Zkusí přímým SQL `INSERT` (mimo službu) vložit překrývající CONFIRMED rezervaci na stejnou stanici.
- Spuštěno příkazem `.\.venv\Scripts\python.exe scripts\spike_persistence.py` (Windows 11, Python 3.14.7). Stejný scénář je trvale v automatických testech (`tests/test_reservations.py::test_db_trigger_blocks_overlapping_confirmed_even_without_service`, `tests/test_web.py::test_cp1_walking_skeleton_post_reservation_returns_id_and_persists`). *(Pozn. z kontroly v C08: první test jsme v C02 nahradili testem `test_db_triggers_enforce_approval_policy_overlap_and_transitions`, který kontroluje i triggery v0.2.)*

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
- Specification Baseline v0.2 ([intent-and-change.md](intent-and-change.md), CV2 – část 2; původně cv2.docx, bod 13) = v0.1 (OP-01–OP-04, BR-01–BR-05) + změna C02: rezervace rychlých stanic (DC ≥ 50 kW, HPC) schvaluje administrátor. Nová operace OP-05 Approve / Reject, nové BR-06 Approval policy, upravené BR-02 (blokující stavy CONFIRMED + PENDING_APPROVAL) a BR-03 (stažení žádosti), nové stavy PENDING_APPROVAL a REJECTED.

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
- Doplněno později (říjen 2026, při převodu specifikace z cv2.docx do `docs/intent-and-change.md`):
  - OP-02 je ve v0.2 nedotčená, takže v ní lokální čísla pravidel zůstala → opraveno i v OP-01 a OP-02 v0.1 (BR-01, BR-02, BR-04, BR-05).
  - REQ-01 v0.1 říkalo, že pro PHEV jsou nedostupné „nízkorychlostní i vysokorychlostní“ stanice → opraveno na HPC, jak to už měla v0.2 i aplikace. BR-04 nově výslovně říká, že PHEV smí na AC 01–06 a DC 07.
  - Zadání chtělo popsat souběh zrušení s jinou operací, ve specifikaci chyběl → OP-04 v0.2 doplněná: operace nad stejnou rezervací proběhnou jedna po druhé a druhá se vyhodnotí podle stavu po první. Ověřuje to nový test `test_op04_v02_withdraw_and_admin_decision_see_state_left_by_the_other`.
  - Baseline v0.1 je výslovně označená jako schválená týmem a do repa přibyly diagramy v0.1 (`docs/diagrams/*_v01.png`).

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

Baseline: v0.2 ([intent-and-change.md](intent-and-change.md), aplikace `v0.2.3`)

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
- SQLite pouští jeden zápis najednou. Při 10× souběhu (future pressure Q) to může být úzké místo, zátěž jsme neměřili (později změřeno, viz C04).
- U skutečného externího providera může zpráva přijít dvakrát (doručení aspoň jednou).

Commit/tag:
- větev `c02-baseline-v0.2`, commit `9f9a70c`, tag `v0.3.0`

---

# CV4 (C04)

## C04 — Quality & Measurement

Quality concern:
Souběh ve špičce – future pressure Q z C01 (10× víc souběžných rezervací) a driver DR-1 z C03. Když hodně řidičů potvrzuje rezervace ve stejnou chvíli, nesmí vzniknout dvojí rezervace stanice (BR-02, REQ-04), požadavky nesmí padat a potvrzení nesmí trvat dlouho. SQLite pouští jeden zápis najednou (D1) a v C01–C03 jsme to vedli jako neověřené riziko.

Evaluable criterion:
Při **50 souběžných klientech**, kteří ve stejné chvíli vytvoří a potvrdí kolidující návrhy na 10 stanicích (5 klientů na stanici), v 6 kolech, tj. 300 potvrzení:
- **K1** – v DB není žádný překryv rezervací v blokujícím stavu a na každé stanici v kole vyhraje právě jedno potvrzení, ostatní dostanou 409,
- **K2** – žádná odpověď 5xx, žádné vypršení spojení (10 s, stejně jako timeout SQLite v `db.py`) a žádná výjimka v logu serveru,
- **K3** – p95 doby odpovědi OP-03 (`POST /reservations/<id>/confirm`) je nejvýš 500 ms.

Kritérium je splněné, jen když platí všechny tři body. Odkud jsou čísla:
- 5 souběžných klientů je náš odhad dnešní špičky (10 stanic, ráno se potvrzuje víc rezervací naráz). Je to předpoklad, ne změřený provoz.
- 50 klientů = 10× podle future pressure Q.
- 500 ms jsme zvolili jako hranici, kdy odpověď na kliknutí „Potvrdit“ ještě působí okamžitě.

Measurement / evidence:
- Skript `scripts/measure_c04.py`, jeden příkaz, exit 0 = kritérium splněno. Spustí aplikaci stejně jako `python run.py` (samostatný proces, vývojový server Werkzeug s vláknem na request, viz G4 z C03) nad čistou SQLite DB v dočasném adresáři. Založí 100 testovacích účtů a zátěž posílá přes HTTP včetně přihlášení a CSRF tokenu, stejně jako prohlížeč.
- V každém kole všichni klienti naráz vytvoří návrh (OP-02) a pak ho naráz potvrdí (OP-03). Klient *i* jde na stanici *i* mod 10, takže se zkouší AC → CONFIRMED i DC/HPC → PENDING_APPROVAL. Doba odpovědi se měří na straně klienta (HTTP na localhost, bez sítě).
- Úrovně zátěže: 5 / 50 / 100 klientů, 6 kol na každé úrovni. BR-02 se na konci kontroluje SQL dotazem přímo nad DB.
- Prostředí: notebook s Windows 11, AMD Ryzen 5 7535U (6 jader / 12 vláken), 14 GB RAM, Python 3.14.7. Klient i server běží na stejném stroji. Měřeno 25. 10. 2026.

Výsledek – původní stav (kód z C05), 3 běhy:

| Klienti | OP-03 p50 / p95 / max [ms] | K1 | K2 | K3 |
| --- | --- | --- | --- | --- |
| 5 | 46 / 69 / 97 · 39 / 67 / 91 · 46 / 87 / 89 | OK | OK | OK |
| 50 | 761 / 1935 / 2500 · 997 / 1804 / 2417 · 813 / 2216 / 2715 | OK | OK | **FAIL** |
| 100 | 1617 / 3430 / 4025 · 1637 / 3693 / 4213 · 1588 / 2752 / 3229 | OK | OK | FAIL |

BR-02 drželo i při 100 klientech (0 překryvů, na každé stanici právě jeden vítěz, žádná 5xx). Nesplněná je jen doba odpovědi.

Hledání příčiny – každá varianta změřená stejným skriptem, 50 klientů:

| Varianta | OP-03 p95 / max [ms] | K3 |
| --- | --- | --- |
| původní stav (rollback journal, čekání přes busy timeout SQLite) | 1804–2216 / 2417–2715 | FAIL |
| WAL | 584 / 903 | FAIL |
| WAL + `synchronous = NORMAL` | 384 / 759, 507 / 1181 | na hraně, 1× FAIL |
| WAL + fronta zápisů v aplikaci | 427 / 489, 276 / 319, 274 / 305 | OK |
| fronta zápisů v aplikaci (bez WAL) | 358 / 408, 315 / 378 | OK |

Každý request dělá dva zápisy (`expire_overdue()` a transakci operace) a SQLite pouští jen jeden zápis najednou. Čekající spojení přitom nestojí ve frontě, busy handler SQLite je uspí a zkouší zámek znovu po 1–100 ms, takže zámek je často volný a všichni spí. Fronta zápisů v aplikaci tohle odstranila, WAL k ní přidal už jen málo.

Trade-off / decision:
- **D9** ([architecture-and-decisions.md](architecture-and-decisions.md)): zápisy z jednoho procesu čekají ve frontě v aplikaci (`db.write_lock()` v `transaction()` a `expire_overdue()`). WAL ani `synchronous = NORMAL` nezavádíme.
- Proč ne WAL: kritérium splní i bez něj a přidává provozní složitost (soubory `-wal` / `-shm`, nevhodný pro synchronizovaný disk – repo i `instance/` máme v OneDrive). Proč ne `synchronous = NORMAL`: při výpadku proudu by se mohly ztratit poslední potvrzené rezervace a výsledek byl stejně na hraně.
- Cena: fronta pomáhá jen v jednom procesu a zápisy jsou dál sériové. Při 100 klientech (20×) p95 kritérium nesplňuje.

Výsledek po změně, 3 běhy:

| Klienti | OP-03 p50 / p95 / max [ms] | K1 | K2 | K3 |
| --- | --- | --- | --- | --- |
| 5 | 28 / 43 / 44 · 33 / 55 / 65 · 36 / 62 / 71 | OK | OK | OK |
| 50 | 196 / 272 / 299 · 228 / 332 / 391 · 219 / 293 / 320 | OK | OK | **OK** |
| 100 | 414 / 603 / 696 · 439 / 593 / 680 · 451 / 599 / 644 | OK | OK | FAIL (mimo kritérium) |

Kritérium při 50 klientech je splněné ve všech třech bězích (`measure_c04.py` exit 0). Funkčně se nic nezměnilo: `pytest` 100 passed, `scripts/verify_c02.py` 14/14 OK, test souběhu REQ-04 dál pouští právě jedno potvrzení z 10.

Remaining uncertainty / risk:
- Měřeno jen na jednom notebooku a na vývojovém serveru z G4. Produkční WSGI server ani jiný HW jsme nezkoušeli.
- Klient a server sdílejí stejné CPU, takže čísla jsou spíš horší odhad.
- 5 souběžných jako dnešní špička je odhad, provoz nemáme.
- 20× (100 klientů) kritérium nesplňuje (p95 kolem 600 ms). Na to by byla potřeba jiná DB (PostgreSQL, D1) nebo WAL a víc procesů.

Commit/tag:
- větev `c04-quality-measurement`, tag `v0.4.0`

---

# CV5 (C05)

## C05 DD Verification Evidence

Tests derived from DD:

| DD element | What must hold | Test level | Test case |
| --- | --- | --- | --- |
| kontrakt `decide()` | AC návrh → CONFIRMED, bez lhůty | unit | `test_contract_ac_draft_is_confirmed_without_deadline` |
| větev DC/HPC | PENDING_APPROVAL a lhůta = start − 15 min | unit | `test_branch_dc_hpc_requests_approval_with_deadline_start_minus_15` |
| hranice lhůty schválení | v 09:45 ještě projde, v 09:45:01 už REJECT (TOO_LATE_FOR_APPROVAL) | unit | `test_branch_approval_deadline_boundary` |
| failure behavior | každá chyba má svůj důvod, vlastník se kontroluje jako první | unit | `test_failure_behavior_returns_defined_reason` |
| rozhodovací tabulka `evaluate()` | 4 řádky tabulky, navazující rezervace nekolidují | unit | `test_availability_decision_table` |
| přechod stavu | služba uloží PENDING_APPROVAL i `approval_deadline` | component | `test_state_transition_service_persists_policy_decision` |
| spolupráce služba ↔ policy | služba se řídí výsledkem policy, při odmítnutí zůstane DRAFT | component | `test_collaboration_service_uses_injected_policies` |
| OP-01 a OP-03 rozhodují stejně | co hledání ukáže jako obsazené, potvrzení odmítne | component | `test_collaboration_op01_and_op03_decide_the_same_way` |
| jedna transakce (REQ-04) | z 10 souběžných potvrzení projde jedno | component | `test_op03_req04_concurrent_confirms_allow_exactly_one` (už z C02) |

Proč zrovna tahle úroveň:

`test_branch_approval_deadline_boundary`
- Selected level: unit.
- Why this level: hranice je jen výpočet v policy (start − 15 min proti `now`), takže stačí slovník s rezervací a čas, DB ani hodiny nepotřebujeme.
- What design decision does this test verify: lhůta schválení je `start − 15 min` a hranice je ještě povolená (`now > deadline` → REJECT s důvodem TOO_LATE_FOR_APPROVAL).

`test_collaboration_op01_and_op03_decide_the_same_way`
- Selected level: component.
- Why this level: chyba by tu byla ve spolupráci – hledání i potvrzení si musí z DB načíst stejné blokující rezervace a poslat je do policy. Unit test samotné policy by to nezachytil.
- What design decision does this test verify: o konfliktu rozhoduje jeden vlastník (`AvailabilityPolicy`) pro OP-01 i OP-03, takže co hledání ukáže jako obsazené, potvrzení odmítne.

Unit/component tests executed:
- `python -m pytest tests/test_confirmation_design.py` → 22 passed
- `python -m pytest` → 99 passed (77 z C03 + 22 nových, staré testy jsme neměnili)
- `scripts/verify_c02.py` → 14/14 OK

Design ↔ implementation mismatch:

DD jsme porovnávali s kódem z C03 (tag `v0.3.0`).

| DD decision | Implemented? | Evidence | Action |
| --- | --- | --- | --- |
| rozhodnutí o potvrzení má jednoho vlastníka (`ConfirmationPolicy`) | NO | pravidla byla přímo v `confirm()` a ještě jednou v `_decorate()` (jestli ukázat tlačítko Potvrdit) | update code |
| OP-01 a OP-03 vyhodnocují konflikt stejně (`AvailabilityPolicy`) | PARTLY | SQL `_blocking_overlaps()` bylo společné, ale vyhodnocení (vůz, CONFIRMED vs PENDING) bylo napsané zvlášť v `search_availability()` a v `confirm()` | update code |
| BR-05 (5 min) kontroluje samo rozhodnutí | PARTLY | `confirm()` spoléhal na to, že `expire_overdue()` předtím přepne starý DRAFT na EXPIRED | update code |
| pořadí: nejdřív pravidla rezervace, pak vůz a konflikt | PARTLY | BR-04 se kontrolovalo dřív než lhůta schválení | update code |
| stav rezervace mění jen `ReservationService`, čas jde přes `clock` | YES | `UPDATE reservations` je jen v `services/reservations.py` (kromě spike skriptu z C01), testy mají pevné hodiny | keep |
| kontrola a zápis v jedné transakci, trigger jako pojistka | YES | `_atomic()` / `BEGIN IMMEDIATE`, trigger `trg_reservations_no_overlap`, test souběhu | keep |
| SQL v samostatné repository vrstvě | NO | SQL je v `ReservationService` | update DD (necháno kódu, viz „DD decisions intentionally left to code“) |

Resolution:
- nové `services/domain.py` (stavy, BR-04, BR-06) a `services/policies.py` (`ConfirmationPolicy`, `AvailabilityPolicy`, `ConfirmationDecision`),
- `confirm()` teď jen načte data, zavolá obě policy a zapíše výsledek. `search_availability()` používá stejnou `AvailabilityPolicy`, `_decorate()` stejnou `ConfirmationPolicy`,
- policy jdou do `ReservationService` předat v konstruktoru (využívá to test spolupráce),
- hlášky pro uživatele zůstaly stejné, takže se nemusely měnit ani staré testy, ani `verify_c02.py`.

Remaining design risk:
- propadání je pořád „líné“ (`expire_overdue()` při requestu), stejné riziko jako v C03,
- rezervace a stanice jsou v policy jen slovníky / `sqlite3.Row`, překlep v názvu sloupce se ukáže až za běhu,
- DB triggery opakují část pravidel z policy, při změně pravidla se musí upravit obě místa,
- SQLite pustí jen jeden zápis najednou, při větší zátěži to může brzdit (neměřili jsme, později změřeno, viz C04).

Commit/tag:
- větev `c05-detailed-design`, commit `b6cdfd4`, tag `v0.5.0`

---

# CV6 (C06)

## C06 Internal Evidence Assessment

Assessed baseline:
`main` ve verzi `v0.5.0`. Hodnotil tým ParkingCrew 25. 10. 2026, vyplněný formulář je v [assessment_form.md](assessment_form.md).

Verified findings:
- **F1 (C03, ADR-01):** ADR-01 slibuje, že se zpráva neztratí, ale `OutboxDispatcher.pending()` bere jen události s méně než 5 pokusy. Po 5 neúspěšných doručeních se událost už nikdy nedoručí. Ověřili dočasným testem (5× výpadek → nic nedoručeno, nic nečeká).
- **F2 (C04):** C04 v projektu chybělo – žádný quality concern, kritérium, měření ani rozhodnutí.
- F3 (C02, specifikace jen v docx mimo repo) hodnotitelé zamítli jako kandidáta, protože R1–R5 ověřili i bez docx.

Owner disposition:

| Finding | Disposition | Action |
| --- | --- | --- |
| F1 | ACCEPTED – change required | opraveno, viz níže |
| F2 | ACCEPTED – change required | C04 doplněno zpětně |
| F3 | ACCEPTED – change required (i když ho hodnotitelé zamítli) | specifikace přepsaná z docx do Markdownu |

Actions / justified no-change decisions:
- **F1 – jak jsme tomu předešli:** limit 5 pokusů jsme nechali jen pro dispatch po POST, aby jedna nedoručitelná zpráva nebrzdila každý request. `OutboxDispatcher.dispatch()` / `pending()` dostaly parametr `max_attempts` a `scripts/dispatch_notifications.py` volá `dispatch(max_attempts=None)`, takže bere všechny nedoručené události bez ohledu na počet pokusů. Zpráva tak v outboxu čeká, dokud se nedoručí, a ADR-01 zase platí. Nový test `test_adr01_event_over_attempt_limit_is_not_lost_and_script_delivers_it`: 5× výpadek, dispatch po POST zprávu přeskočí, skript ji doručí. Bez opravy test padá. ADR-01 (negativní důsledky) a D5 jsou doplněné.
- **F2:** C04 jsme doplnili zpětně (sekce CV4 výše, rozhodnutí D9, tag `v0.4.0`). Měření ukázalo, že původní stav kritérium nesplňoval (p95 OP-03 kolem 2 s), proto jsme změnili i kód.
- **F3:** specifikaci v0.1 a v0.2 jsme přepsali z `cv2.docx` do Markdownu do [intent-and-change.md](intent-and-change.md) (sekce CV2). Přidali jsme diagramy v0.1 a tabulku, kde je který požadavek ověřený.
- Mimo findings: u D4 hodnotitelé napsali, že jim přežil mutant na hranici „start = teď“. Doplnili jsme test `test_failure_behavior_returns_defined_reason` o případ start == teď → ALREADY_STARTED (REQ-03 v0.2: začátek musí být v budoucnu).
- Ověření po změnách: `pytest` 102 passed, `scripts/verify_c02.py` 14/14 OK.

Remaining evidence gaps:
- Cron pro `scripts/dispatch_notifications.py` nastavený nemáme. Bez provozu a bez spuštění skriptu čeká zpráva po 5 pokusech v outboxu.
- Propadání (DR-2) je pořád „líné“ a o propadlé žádosti se žadatel nedozví (od C03 beze změny).
- C04 je měřené jen na jednom notebooku a vývojovém serveru. Při 20× souběhu kritérium neplatí.

Commit/tag:
- větev `c06-assessment-fixes`, tag `v0.6.0`

---

# CV7 (C07–C08)

## C07 — Ověření C06 findings

Assessed baseline (C06): `v0.5.0` (`b6cdfd4`).
Reporting baseline (A i C): `main` `8e60b8c` (kód = `v0.6.0`). Ověřeno 2. 11. 2026: `pytest` 102 passed, `scripts/verify_c02.py` 14/14, `scripts/measure_c04.py` exit 0.

| C06 | Claim | Expected ↔ Observed | Evidence (`v0.5.0` → reporting) | Decision | Action + owner |
| --- | --- | --- | --- | --- | --- |
| F1 | ADR-01: zpráva se při výpadku Notification Service neztratí | E: událost čeká v outboxu, dokud se nedoručí · O: po 5 pokusech ji nevybere ani dispatch po POST, ani `dispatch_notifications.py` | `v0.5.0`: repro test (5× výpadek → `dispatch()` jako skript → zpráva nedorazí) **padá** · `8500a69`: `test_adr01_event_over_attempt_limit_is_not_lost_and_script_delivers_it` prochází | VERIFIED FINDING → ACCEPT | opraveno v `8500a69`, J. Staněk. Zbývá issue I-01 |
| F2 | C04: concern → criterion → měření → rozhodnutí | E: C04 v repu · O: ve `v0.5.0` chybí | `git ls-tree v0.5.0 scripts/` bez `measure_c04.py` · `50a1579` sekce CV4, D9; přeměřeno 2. 11. na `8e60b8c`: K1–K3 OK, p95 OP-03 315 ms | VERIFIED FINDING → ACCEPT | doplněno zpětně v `50a1579` (proto je tag `v0.4.0` novější než `v0.5.0`), J. Staněk |
| F3 | Specifikace v0.2 je v repu | E: text OP/REQ v repu · O: jen `cv2.docx` (v `.gitignore`) | `v0.5.0`: `intent-and-change.md` 62 řádků bez REQ · `50a1579`: sekce CV2, 532 řádků | REJECTED CANDIDATE → ACCEPT (opraveno i tak) | J. Staněk, uzavřeno |

Open issues:
- **I-01:** `scripts/dispatch_notifications.py` nespouští žádný plánovač → po 5 neúspěšných pokusech zpráva čeká, dokud skript někdo nespustí ručně. Owner J. Staněk, akce P2.
- **I-02:** propadání (AD-1) je líné (`expire_overdue()` až při requestu) a o EXPIRED žádosti se žadatel nedozví. Známé od C03. Notifikaci doplníme (akce P1) a jestli má žádost místo propadnutí eskalovat, se zeptáme na C08 (R-01). Owner J. Staněk.

## A — Technical Design Review (co ukážeme)

Jedna linie: REQ-04 + BR-02 → OP-03 Confirm / OP-05 Approve, baseline `8e60b8c`.

| Krok | Evidence |
| --- | --- |
| 1 Scope | [intent-and-change.md](intent-and-change.md) CV2: OP-03, změna v0.2 (DC/HPC → PENDING_APPROVAL), `state_v02.png`, `verify_c02.py` 14/14 |
| 2 Architecture | C03: drivery DR-1 (souběh) a výpadek NS, ADR-01 outbox, `c03_static.png`, `c03_sequence.png`, `tests/test_architecture.py` |
| 3 Detailed Design | C05: `ConfirmationPolicy.decide()` + `AvailabilityPolicy.evaluate()` v `services/policies.py`, `c05_structure.png`, `tests/test_confirmation_design.py` (22) |
| 4 Verification | pozitivní `test_op03_ac_draft_without_collision_is_confirmed_immediately`, hraniční `test_branch_approval_deadline_boundary` (09:45 vs 09:45:01), REQ-04 `test_op03_req04_concurrent_confirms_allow_exactly_one`, C04 `measure_c04.py` |
| 5 C06 | F1: repro padá na `v0.5.0`, oprava `8500a69` + regresní test, zbývá I-01 |

## Registr rizik — C07/C08

### R-01
- **Popis rizika:** Žádosti na DC/HPC schvaluje jediný admin do T_start − 15 min (BR-06) a specifikace neříká, co když nerozhodne (otevřený předpoklad z C02) → žádost propadne do EXPIRED a k tomu neposíláme žádnou notifikaci (D7, I-02) → řidič se to dozví až v aplikaci, nebo až u stanice.
- **Kategorie:** Scope / Requirements
- **Počáteční riziko (L/I/priorita/důvod):** L=2 (jeden admin, žádné připomenutí, noc a víkend), I=2 (uživatel přijde o rezervaci, BR-02 to neporuší) → MEDIUM (4)
- **Strategie ošetření a zdůvodnění:** MITIGATE – dopad snížíme tím, že se to žadatel dozví včas
- **Opatření, vlastník, termín:** při přechodu PENDING_APPROVAL → EXPIRED zapsat do outboxu událost o propadnutí (P1) a pouštět `expire_overdue()` + dispatch plánovačem každých 5 min (P2); na C08 se zeptat, jestli má žádost místo propadnutí eskalovat; owner J. Staněk, do 9. 11. 2026
- **Důkaz účinnosti a stav ověření:** zatím žádný; ověří ho test „žádost propadne bez requestu a žadatel dostane zprávu“
- **Očekávané reziduální riziko:** L=2, I=1 → LOW (2)
- **Skutečné reziduální riziko / NOT VERIFIED:** NOT VERIFIED
- **Přijetí reziduálního rizika, vlastník, zdůvodnění:** J. Staněk po odpovědi cvičícího
- **Monitoring, trigger, contingency, další review:** trigger: EXPIRED žádost bez zprávy v `notifications`, nebo cvičící chce eskalaci; review 9. 11.

### R-02
- **Popis rizika:** Stejná pravidla (BR-02, BR-04, schvalování DC/HPC, přechody stavů) máme ve službě (`services/policies.py`) i v triggerech (`schema.sql`, D2) → při další změně pravidla, jako byla změna C02, se upraví jen jedno místo → DB odmítne operaci, kterou služba povolila (uživatel dostane chybu místo hlášky), nebo pojistka přestane chránit.
- **Kategorie:** Technical / Quality
- **Počáteční riziko (L/I/priorita/důvod):** L=2 (pravidla se už jednou měnila, C05 to vede jako zbývající riziko), I=2 (rozbitá operace nebo slabší ochrana BR-02) → MEDIUM (4)
- **Strategie ošetření a zdůvodnění:** MITIGATE – rozdíl musí odhalit test, ne uživatel
- **Opatření, vlastník, termín:** test shody: stejné příklady pro BR-02, BR-04 a schvalování pustit přes službu i přímo proti DB (INSERT/UPDATE mimo službu) a porovnat výsledek (P3); owner Š. Exner, do 9. 11. 2026
- **Důkaz účinnosti a stav ověření:** `test_db_triggers_enforce_approval_policy_overlap_and_transitions` dnes ověřuje triggery, ale ne shodu s policy → NOT VERIFIED
- **Očekávané reziduální riziko:** L=1, I=2 → LOW (2)
- **Skutečné reziduální riziko / NOT VERIFIED:** NOT VERIFIED
- **Přijetí reziduálního rizika, vlastník, zdůvodnění:** Š. Exner
- **Monitoring, trigger, contingency, další review:** trigger: změna pravidla ve specifikaci nebo v `policies.py` bez změny `schema.sql`

### R-03
- **Popis rizika:** SQLite pustí jeden zápis najednou (D1, D9) a C04 jsme měřili jen na jednom notebooku s vývojovým serverem → při vyšší zátěži nebo na slabším stroji přesáhne p95 potvrzení 500 ms (při 100 klientech už 583 ms) → kritérium C04 přestane platit.
- **Kategorie:** Technical / Quality
- **Počáteční riziko (L/I/priorita/důvod):** L=1 (10× dnešní špičky splněno ve 4 bězích, 25. 10. i 2. 11.; víc provozu nečekáme), I=2 (pomalejší potvrzení, BR-02 drží i při 100 klientech) → LOW (2)
- **Strategie ošetření a zdůvodnění:** ACCEPT – limit nad 10× je vědomý (D9), jiná DB by byla nepřiměřená
- **Opatření, vlastník, termín:** přeměřit `measure_c04.py` na druhém stroji (P4); owner D. Volný, do 9. 11. 2026
- **Důkaz účinnosti a stav ověření:** `measure_c04.py` 2. 11. na `8e60b8c` exit 0 (p95 315 ms), druhý stroj zatím ne
- **Očekávané reziduální riziko:** L=1, I=2 → LOW (2)
- **Skutečné reziduální riziko / NOT VERIFIED:** LOW (2), beze změny (přijaté riziko)
- **Přijetí reziduálního rizika, vlastník, zdůvodnění:** J. Staněk – kritérium platí pro 50 klientů, 20× není požadavek
- **Monitoring, trigger, contingency, další review:** přeměřit po každé změně `db.py` nebo `services/`; trigger: `measure_c04.py` exit ≠ 0

### R-04
- **Popis rizika:** Když Notification Service nejede déle než 5 pokusů, dispatch po POST zprávu vzdá a doručí ji až skript, který zatím nikdo automaticky nespouští (I-01); doručení je navíc „aspoň jednou“ (ADR-01) → zpráva o schválení nebo zamítnutí dorazí pozdě, u skutečného providera i dvakrát.
- **Kategorie:** External dependency
- **Počáteční riziko (L/I/priorita/důvod):** L=1 (dnes je Notification Service kanál do vlastní DB, výpadek jsme viděli jen v testech), I=2 (žadatel neví výsledek, rezervace sama je v pořádku) → LOW (2)
- **Strategie ošetření a zdůvodnění:** MITIGATE – levné, plánovač stejně potřebujeme kvůli R-01
- **Opatření, vlastník, termín:** Plánovač úloh pouští `dispatch_notifications.py` každých 5 min (P2); owner J. Staněk, do 9. 11. 2026
- **Důkaz účinnosti a stav ověření:** regresní test F1 prochází (`8500a69`), běh plánovače NOT VERIFIED
- **Očekávané reziduální riziko:** L=1, I=1 → LOW (1)
- **Skutečné reziduální riziko / NOT VERIFIED:** NOT VERIFIED
- **Přijetí reziduálního rizika, vlastník, zdůvodnění:** J. Staněk
- **Monitoring, trigger, contingency, další review:** trigger: v outboxu událost s `attempts >= 5` a `sent_at IS NULL`, nebo přechod na skutečný e-mail / SMS

### R-05
- **Popis rizika:** Neukládáme, kdo žádost schválil nebo zamítl (jen `updated_at`, AD-4 z C02), a `_decide()` kontroluje jen roli, takže admin smí schválit i vlastní žádost → při sporu nejde dohledat, kdo rozhodl, a admin si může rychlou stanici přidělit sám.
- **Kategorie:** Security / Privacy
- **Počáteční riziko (L/I/priorita/důvod):** L=1 (jeden admin, školní provoz), I=2 (obejití schvalování v0.2) → LOW (2)
- **Strategie ošetření a zdůvodnění:** ACCEPT – pro CP1 a jednoho admina to stačí; roli admina kontroluje server (V9, `test_op05_non_admin_cannot_approve_or_reject`)
- **Opatření, vlastník, termín:** nic pro CP1; při víc adminech přidat `decided_by` / `decided_at` a zákaz schvalovat vlastní žádost; owner J. Staněk
- **Důkaz účinnosti a stav ověření:** přijaté riziko, není co ověřovat
- **Očekávané reziduální riziko:** L=1, I=2 → LOW (2)
- **Skutečné reziduální riziko / NOT VERIFIED:** LOW (2), přijato
- **Přijetí reziduálního rizika, vlastník, zdůvodnění:** J. Staněk – jeden admin, fiktivní data
- **Monitoring, trigger, contingency, další review:** trigger: druhý admin, reálný provoz, nebo cvičící řekne, že vlastní žádost admin schválit nesmí

Předpoklady (nejsou rizika): uživatelé dodrží časy rezervace (C01), fyzické ověření auta a SPZ na místě zůstává neznámá (C01, D8).

## C — Project Status Report

[c08-project-status.pptx](c08-project-status.pptx), stejná baseline `8e60b8c` a stejné F1–F3, R-01–R-05.

---

# CV8 (C08)

Change Card CR-08 (request, dopady, varianty, rozhodnutí) je v [intent-and-change.md](intent-and-change.md), CV8. Tady je výchozí baseline a test casy.

## Výchozí baseline (before)

- **Commit / tag:** `v0.8.0`, větev `c08-c10`. Kód aplikace (`src/`) je stejný jako `v0.6.0` (`8e60b8c`, `git diff v0.6.0 -- src` je prázdný), v C08 přibyly jen dokumenty a `scripts/verify_c08.py`.
- **Specifikace:** v0.2 – BR-02 (v0.2), BR-06, OP-03 a OP-05 v [intent-and-change.md](intent-and-change.md), stavový diagram `diagrams/state_v02.png`. Architektura: ADR-01, D7, G3 v [architecture-and-decisions.md](architecture-and-decisions.md).
- **Runtime:** Windows 11, Python 3.14.7, SQLite 3.50.4; Flask 3.1.3, Werkzeug 3.1.8, Jinja2 3.1.6, pytest 9.1.1 (`pip freeze` z `.venv`).
- **DB schéma:** `src/charging/schema.sql`, `PRAGMA user_version = 5`.
- **Data:** každý běh si založí novou DB v dočasném adresáři (schéma + 10 stanic + demo účty `admin`, `ridic_ev`), účet `ridic_ev2` se zaregistruje přes `/register`. Rezervace se připraví přes HTTP API. Hodiny jsou řízené, „teď“ = 15. 9. 2026 08:00. Hesla jsou jen demo účty z README, `SECRET_KEY` je testovací hodnota.
- **Příkazy:** `python scripts/verify_c08.py` (TC-C08-01/02), `python -m pytest` → 102 passed, `python scripts/verify_c02.py` → 14/14. Spuštěno 16. 11. 2026.

## C08 / TC-C08-01 — navazující rezervace, schválení adminem

- **Test basis:** BR-01 (intervaly [start, end), navazující nekolidují), BR-02 v0.2, OP-05 REQ-07 (v0.2). Riziko: R-02 (pravidla ve službě i v DB).
- **Baseline:** viz výše, `python scripts/verify_c08.py`.
- **Předpoklady:** izolovaná DB; B1 = `ridic_ev` HPC 08 [10:00, 11:00) vytvořená, potvrzená a schválená adminem (CONFIRMED); B2 = `ridic_ev2` HPC 08 [11:00, 12:00) vytvořená a potvrzená (PENDING_APPROVAL, lhůta 09:45). Role: admin schvaluje.
- **Data:** 15. 9. 2026, lokální čas serveru (D3), HPC 08 (150 kW, vyžaduje schválení), oba vozy BEV.

| # | Akce | Očekáváno (napsáno před během) | Skutečně | Důkaz |
| --- | --- | --- | --- | --- |
| 1 | reset (nová DB) a příprava B1, B2 přes API | B1 CONFIRMED, B2 PENDING_APPROVAL | B1 #1 CONFIRMED, B2 #2 PENDING_APPROVAL | výstup skriptu |
| 2 | nezávislé čtení z DB (nové `sqlite3` spojení) | [10:00, 11:00) CONFIRMED, [11:00, 12:00) PENDING_APPROVAL, nic jiného na HPC 08 | přesně tak | `SELECT … FROM reservations WHERE station_id = 8` |
| 3 | 09:00, admin `POST /reservations/2/approve` | operace projde, 200 CONFIRMED | 200 CONFIRMED | odpověď JSON |
| 4 | nezávislé čtení po commitu | obě CONFIRMED, 0 překryvů v blokujícím stavu | obě CONFIRMED, překryvů 0 | dotaz na překryv (self-join, `a.start < b.end AND b.start < a.end`) |
| 5 | reset a zopakování 1–4 | stejný výsledek | stejný výsledek | výstup skriptu, běh 2 |

- **Oracle:** stav přečtený novým spojením přímo ze SQLite, ne z odpovědi aplikace.
- **Verdikt:** **PASS** (16. 11. 2026, `v0.8.0`).
- **Co to nepokrývá:** souběh dvou schválení (ten pokrývá `test_op03_req04_concurrent_confirms_allow_exactly_one` jen pro potvrzení), skutečný server jako samostatný proces (běží Flask test client ve stejném procesu, DB je ale skutečný soubor).

## C08 / TC-C08-02 — kolize při potvrzení

- **Test basis:** BR-02 v0.2, OP-03 REQ-03 (v0.2): potvrzení kolidujícího návrhu se odmítne a návrh zůstává DRAFT. ADR-01: odmítnutá operace nezapíše událost.
- **Baseline:** viz výše, `python scripts/verify_c08.py`.
- **Předpoklady:** izolovaná DB; B1 = `ridic_ev` HPC 08 [10:00, 11:00) CONFIRMED (jako v TC-01); B2 = `ridic_ev2` HPC 08 [10:30, 11:30) DRAFT. `ridic_ev2` je vlastník B2, takže oprávnění je splněné.
- **Data:** 15. 9. 2026, 08:00, lokální čas.

| # | Akce | Očekáváno (napsáno před během) | Skutečně | Důkaz |
| --- | --- | --- | --- | --- |
| 1 | reset a příprava B1, B2 | jen B1 CONFIRMED, B2 DRAFT | B1 #1 CONFIRMED, B2 #2 DRAFT | výstup skriptu |
| 2 | nezávislé čtení z DB | časy se překrývají, blokující je jen B1 | #1 [10:00, 11:00) CONFIRMED, #2 [10:30, 11:30) DRAFT, 0 překryvů v blokujícím stavu | SQL dotaz |
| 3 | `ridic_ev2` `POST /reservations/2/confirm` | odmítnuto, 409 | 409 „Termín se na této stanici překrývá s potvrzenou rezervací…“ | odpověď JSON |
| 4 | nezávislé čtení po rollbacku | B1 beze změny (i `updated_at`), B2 DRAFT, 0 překryvů, v outboxu žádná událost pro B2 | přesně tak | SQL dotazy na `reservations` a `outbox` |
| 5 | reset a zopakování 1–4 | stejný výsledek | stejný výsledek | výstup skriptu, běh 2 |

- **Verdikt:** **PASS** (16. 11. 2026, `v0.8.0`). Celkem 18/18 kroků, exit 0.
- **Co to nepokrývá:** souběžné potvrzení dvou návrhů (pokrývá `test_op03_req04_concurrent_confirms_allow_exactly_one` a měření C04), přímý zápis do DB mimo aplikaci (pokrývá `test_db_triggers_enforce_approval_policy_overlap_and_transitions`).

## Kontrola celého projektu (C01–C07) při C08

Před C08 jsme prošli specifikaci, kód a zadání C01–C07 proti sobě: operace OP-01 až OP-05, pravidla BR-01 až BR-06, hranice lhůt (5 min, 30 min, T_start − 15 min), stavové přechody v triggerech, kódy chyb v API, odkazy na testy a soubory v dokumentaci.

- Specifikace v0.2 a kód si odpovídají, `pytest` 102 passed, `verify_c02.py` 14/14, spike z C01 (`scripts/spike_persistence.py`) dál projde.
- Nalezený nesoulad: evidence C01 odkazovala na test `test_db_trigger_blocks_overlapping_confirmed_even_without_service`, který jsme v C02 nahradili. Opraveno poznámkou v CV1.
- Registr rizik C07: opatření pro R-01 a R-04 měla termín 9. 11. 2026 a hotová nebyla. R-01 (P1) řeší CR-08, plánovač (P2, R-04) zůstává otevřený – viz varianta B v CR-08.

---

# CV9 (C09)

## C09 — Integration, CI and Release Record

**C08 Change Card a rozhodnutí:** [intent-and-change.md](intent-and-change.md), CV8, CR-08 – ACCEPT varianta A (zpráva žadateli při líném propadnutí žádosti), varianta B (plánovač) DEFER.

**Before / after:** `v0.8.0` (`505c3e5`) → `v0.9.0` (`9e81db9`), větev `c08-c10`. Schéma DB je v obou verzích 5, konfigurace stejná.

**Dotčená tvrzení, nové AC a regrese:** nové AC-01 až AC-04 z CR-08. Beze změny musí zůstat BR-02 (TC-C08-01/02), BR-06 včetně hranice 09:45:00 a uvolnění slotu (V6), ADR-01 (outbox, výpadek Notification Service) a kritérium C04.

**Co se změnilo:**
- `services/reservations.py` – `expire_overdue()`: když něco propadlo, v jedné transakci najde propadlé žádosti, přepne je do EXPIRED a zapíše do outboxu `reservation_expired`. Když nic nepropadlo, transakci neotevírá (viz C04 níže).
- `services/notifications.py` – `reservation_expired()` (text zprávy), `services/outbox.py` – doručení nové události.
- `db.py` – odstraněné `write_lock()`, které po změně nikdo nevolal (zámek bere `transaction()`).
- testy: 5 nových `test_cr08_*` v `tests/test_reservations.py`, nová událost v `tests/test_architecture.py`, TC-C08-03 ve `scripts/verify_c08.py`.
- dokumenty: BR-06, tabulka ověření, D5, D7, D9, README. CI: `.github/workflows/ci.yml`.
- N/A: schéma a migrace (outbox už existuje, události jsou text), HTTP API a UI (žádný nový endpoint), konfigurace (žádný nový parametr), worker (neexistuje, varianta B odložená).

**Review:**
Reviewer: **DOPLNIT** (Š. Exner nebo D. Volný – porovnat diff `git diff v0.8.0 v0.9.0` s AC-01 až AC-04). Při vlastní kontrole diffu jsme ověřovali: zpráva vzniká ve stejné transakci jako přechod do EXPIRED (ADR-01), hranice 09:45:00 se nezměnila, výběr a UPDATE jsou v jedné transakci (žádná dvojí zpráva), pozdní approve vrací 409 a nepošle zprávu o schválení. Při přeměření C04 jsme našli zhoršení doby odpovědi a opravili ho (viz níže) – zůstali jsme ve variantě A, chování se nezměnilo.

| Test | Oracle (přijaté očekávání) | Příkaz / data | Očekáváno | Skutečně | Výsledek | Důkaz |
| --- | --- | --- | --- | --- | --- | --- |
| Nové chování – unit/component | AC-01 až AC-04 z CR-08 | `python -m pytest -k cr08`, DB v dočasném adresáři, hodiny 15. 9. 2026 | na `v0.8.0` padají, na `v0.9.0` projdou | `v0.8.0`: 4 failed, 1 passed (AC-02 je negativní, projde i bez změny); `v0.9.0`: 5 passed | PASS | výstup pytest |
| Nové chování – TC-C08-03 (integrace) | AC-01, AC-03, AC-04 | `python scripts/verify_c08.py`, viz kroky níže | viz kroky | na `v0.8.0` 3 kroky FAIL, na `v0.9.0` vše OK | PASS | výstup skriptu |
| C08 regrese TC-C08-01 | BR-01, BR-02, REQ-07 | `verify_c08.py` | 2× schválení navazující žádosti 200, obě CONFIRMED, 0 překryvů | stejně | PASS | výstup skriptu |
| C08 regrese TC-C08-02 | BR-02, REQ-03 | `verify_c08.py` | 2× 409, B2 DRAFT, B1 beze změny, žádná událost | stejně | PASS | výstup skriptu |
| Ostatní regrese | specifikace v0.2, ADR-01, architektonické pravidlo | `python -m pytest`, `python scripts/verify_c02.py` | vše projde | 107 passed (102 starých + 5 nových), 14/14 | PASS | výstup |
| C04 (kvalita) | K1–K3 při 50 klientech | `python scripts/measure_c04.py` | exit 0, p95 OP-03 ≤ 500 ms | viz tabulka níže | PASS po opravě | výstup skriptu |

**TC-C08-03 – kroky** (očekávání napsané podle AC před během, běží 2× s resetem):

| # | Akce | Očekáváno | Skutečně (`v0.9.0`) |
| --- | --- | --- | --- |
| 1 | reset, `ridic_ev2` vytvoří a potvrdí HPC 09 [10:00, 10:30) | PENDING_APPROVAL, lhůta 09:45:00 | PENDING_APPROVAL, lhůta 09:45:00 |
| 2 | 09:45:00 admin `POST /reservations/1/approve`, pak čtení z DB | 200, CONFIRMED, 0 událostí `reservation_expired` | 200, CONFIRMED, 0 |
| 3 | reset na stejný stav; 09:45:01 `ridic_ev2` `GET /reservations`, pak čtení z DB | EXPIRED, 1 nedoručená událost `reservation_expired` | EXPIRED, 1, nedoručená |
| 4 | 09:50 admin schvaluje pozdě, pak čtení z DB | 409, EXPIRED, žádná CONFIRMED na HPC 09, událost doručená (dispatch po POST), 0 překryvů | 409, EXPIRED, 0, doručená, 0 překryvů |
| 5 | `ridic_ev2` `GET /notifications` + čtení tabulky `notifications` | právě 1 zpráva o propadnutí #1, žádná o schválení, zpráva je na stránce | 1, 0, ano |

Na `v0.8.0` (před změnou) kroky 3, 4 a 5 FAIL – událost ani zpráva nevznikne. Celkem `verify_c08.py` na `v0.9.0`: 29/29 kroků, exit 0.

**Skutečná integrační hranice:** HTTP rozhraní aplikace (Flask, přihlášení, CSRF, JSON) nad skutečným SQLite souborem; stav se po každém kroku čte novým `sqlite3` spojením přímo z DB, ne z odpovědi aplikace. Aplikace v `verify_c08.py` běží ve stejném procesu (Flask test client), aby šly řídit hodiny. Jako samostatný proces (`python run.py`) přes skutečné HTTP běží `measure_c04.py` a smoke test v čistém checkoutu.

**C04 přeměření** (16. 11. 2026, stejný notebook jako v C04, p95 OP-03 při 50 / 100 klientech):

| Verze | p95 při 50 klientech [ms] | K3 | p95 při 100 [ms] |
| --- | --- | --- | --- |
| `v0.8.0` (před změnou) | 352, 328 | OK | 634, 616 |
| první verze změny (`expire_overdue()` vždy v transakci) | 424, **505**, 346 | 1× **FAIL** | 657, 626, 644 |
| `v0.9.0` (transakce jen když něco propadlo) | 303, 289, 281, 295, 288 | OK | 533, 763, 528, 531, 525 |

První verze změny držela zámek zápisu u každého requestu déle (čtyři příkazy místo jednoho `UPDATE`) a jeden běh z tří kritérium nesplnil. Po úpravě je p95 dokonce o něco lepší než před změnou, protože když nic nepropadlo, request nezapisuje vůbec. K1 a K2 ve všech bězích OK. Při 100 klientech (mimo kritérium) jsme v jednom z pěti běhů viděli jednu chybu spojení u klienta, v logu serveru výjimka nebyla a znovu se to neukázalo.

**Závislosti, runtime, schéma, konfigurace:**
- Python 3.14.7 (Windows 11), SQLite 3.50.4, schéma `user_version = 5`.
- `requirements.txt` zamyká jen `Flask==3.1.3` a `pytest==9.1.1`. Čistá instalace 16. 11. stáhla: blinker 1.9.0, click 8.5.1, colorama 0.4.6, iniconfig 2.3.1, itsdangerous 2.2.0, Jinja2 3.1.6, MarkupSafe 3.0.4, packaging 26.3, pluggy 1.7.0, Pygments 2.21.0, Werkzeug 3.1.9. Vývojové `.venv` má starší Werkzeug 3.1.8, click 8.5.0, MarkupSafe 3.0.3, pluggy 1.6.0, iniconfig 2.3.0 – nepřímé závislosti nejsou zamčené (omezení, viz rozhodnutí).
- Konfigurace (nic tajného v repu): `DATABASE_PATH` (výchozí `instance/charging.db`), `SECRET_KEY` (jinak se vygeneruje do `instance/secret_key`), `PORT` (5000), `FLASK_DEBUG`.

**Čistý checkout** (16. 11. 2026 10:24–10:27, nový adresář mimo repo, stejný notebook, Windows 11, Python 3.14.7):
```
git clone <repo> clean_v090 && cd clean_v090 && git checkout v0.9.0
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt      -> exit 0
.venv\Scripts\python.exe -m pytest                              -> 107 passed, exit 0
.venv\Scripts\python.exe scripts\verify_c02.py                  -> 14/14, exit 0
.venv\Scripts\python.exe scripts\verify_c08.py                  -> 29/29, exit 0
python run.py + GET http://127.0.0.1:<port>/login               -> 200, instance/charging.db se založila sama
```
Nic jsme ručně nedoplňovali, stačilo README. Provedl J. Staněk (autor změny), takže to není nezávislá reprodukce. **DOPLNIT:** stejný postup od druhého člena týmu na jeho počítači (kdo, kdy, výsledek).

**CI:** `.github/workflows/ci.yml` (push + PR, Ubuntu, Python 3.14: pip install, `pytest -v`, `verify_c02.py`, `verify_c08.py`). Stav: **PASS** – po push tagu `v0.9.0` (16. 11. 2026 09:42 UTC) běh [43842142344](https://github.com/Gangyss67/SWI_Charger_stations/actions/runs/43842142344) na commitu `9e81db9`: všechny kroky (závislosti, pytest, `verify_c02.py`, `verify_c08.py`) success, job trval 24 s. Stejně prošel i běh [43842141889](https://github.com/Gangyss67/SWI_Charger_stations/actions/runs/43842141889) na hlavě větve `c08-c10` (`ca0909a`). Je to druhé prostředí (Ubuntu na GitHubu), takže testy nezávisí na našem Windows.

**Artefakt:** tag `v0.9.0`, commit `9e81db930095fbedcf4822503e52cd6d5c5eddce`, strom `a86fb9cd23c47feb7afdfa6ae175a3f7540ab2b8`. Balík zdrojáků `git archive --format=zip --prefix=voltbook-v0.9.0/ v0.9.0` → SHA-256 `36aefa4602b372ec78f372b40f30bafcd9964beed4564659b49302aaf63e62b9` (vytvořený dvakrát, hash stejný). Build krok Python aplikace nemá, artefakt = zdrojáky + závislosti výše + schéma v5. Migrace není potřeba.

**Návrat a rizika:**
- Návrat na `v0.8.0`: schéma je stejné, DB funguje dál. Události `reservation_expired`, které už vznikly, ale stará verze nezná – dispatcher na nich spadne („Neznámá událost“), zůstanou nedoručené a skript je bude zkoušet pořád. Pak je potřeba je ručně smazat nebo označit jako odeslané. Nezkoušeli jsme to.
- Propadání je pořád líné (I-02) a plánovač nemáme (I-01, varianta B) – v noci bez provozu přijde zpráva pozdě.
- Souběh aplikace a skriptu `dispatch_notifications.py` ve dvou procesech při propadnutí jsme zvlášť netestovali (NOT VERIFIED), spoléháme na `BEGIN IMMEDIATE` jako u REQ-04.
- R-01 z C07: opatření P1 je IMPLEMENTED a VERIFIED (testy výše), P2 (plánovač) DEFERRED. Reziduální riziko odhadujeme na L=2, I=1 → LOW (2) přes den, v noci bez provozu zůstává jako dřív.

**Rozhodnutí o release candidate:** **CONDITIONAL** – owner J. Staněk, 16. 11. 2026.
Nové AC i regrese prošly lokálně, v čistém checkoutu i v CI na GitHubu a C04 platí. Chybí ale: (1) review diffu druhým členem, (2) čistý checkout od druhého člena. Když budou oba v pořádku, bude to READY. Když CI nebo review najde problém s AC, HOLD a vracíme se k CR-08. Nezamčené nepřímé závislosti bereme jako známé omezení.

---

# CV10 (C10)

## C10 — Provozní situace a logy

**Testovaný commit:** první běh na `30d4645` (kód = `v0.9.0`), po doplnění logu znovu na `50100c6`. 16. 11. 2026, Windows 11, Python 3.14.7.

**Jak:** aplikace spuštěná jako `python run.py` (samostatný proces, vývojový server) s vlastní testovací DB přes `DATABASE_PATH` v dočasném adresáři, `SECRET_KEY` testovací, stdout/stderr do souboru `server.log`. Operace jsme posílali přes HTTP jako prohlížeč (přihlášení demo účtů `ridic_ev` a `ridic_phev`, CSRF, JSON), rezervace na zítřek 17. 11. Stav jsme po každé situaci četli zvlášť přímo z SQLite (`sqlite3`, nové spojení). Výpadek DB jsme udělali tak, že jiný proces otevřel stejnou DB a držel ji 25 s zamčenou (`BEGIN EXCLUSIVE`), pak zámek pustil – jen na testovacích datech.

| Situace | Očekávaný a skutečný výsledek | Jak jsme správnost ověřili | Co ukazují logy / je log potřeba? |
| --- | --- | --- | --- |
| Úspěšné potvrzení | Oček.: AC 01 [10:00, 11:00) vytvořit (201 DRAFT) a potvrdit (200 CONFIRMED). Skut.: 201 DRAFT → 200 CONFIRMED. | odpověď JSON + `SELECT` z DB: #1 AC 01 [10:00, 11:00) CONFIRMED | jen řádek přístupového logu `POST /reservations/1/confirm … 200`. Víc logovat není potřeba, správnost dokazuje odpověď a stav v DB. |
| Zamítnutá kolize | Oček.: PHEV potvrzuje AC 01 [10:30, 11:00) → 409, návrh zůstane DRAFT. Skut.: 409 „Termín se na této stanici překrývá…“, #2 DRAFT. | DB: #1 CONFIRMED beze změny, #2 DRAFT, 0 překryvů v blokujícím stavu | přístupový log `… /reservations/2/confirm … 409`. Je to běžné doménové zamítnutí, ne chyba – uživatel dostane důvod v odpovědi a stav je v DB. Zvláštní záznam nepřidáváme, byl by to jen šum. Audit by dával smysl spíš u rozhodnutí admina (R-05), to tu nebylo. |
| Technická porucha a obnova | Oček.: během výpadku vytvoření rezervace AC 02 selže chybou serveru a nic se nezapíše; po obnovení po kontrole stavu projde. Skut.: 500 Internal Server Error po 21 s; DB po obnovení: rezervací pořád 2, na AC 02 žádná → zopakováno: 201 DRAFT → 200 CONFIRMED. | před opakováním jsme nejdřív přečetli DB (počet rezervací a AC 02), abychom zápis neopakovali naslepo | `ERROR charging: Exception on /reservations [POST]` s časem, místem (`auth.py`, `load_logged_in_user` – první čtení z DB) a typem `sqlite3.OperationalError: database is locked`, pak přístupový log `500`. Z logu je poznat kdy, kde a jak aplikace reagovala. Že se nic nezapsalo, z logu poznat nejde – to jsme ověřili v DB. |
| Restart aplikace | Oček.: po zastavení a novém startu potvrzené rezervace #1 a #3 zůstanou CONFIRMED a další operace projde. Skut.: #1 a #3 CONFIRMED (API i DB), nová rezervace AC 03 201 → 200 CONFIRMED. | `GET /api/reservations/<id>` + `SELECT` z DB před a po restartu | v prvním běhu byl start vidět jen jako `* Serving Flask app 'charging'` bez času a bez informace o DB, zastavení vůbec. Doplnili jsme záznam o startu (viz níže) a pokus zopakovali. Zastavení v logu není dál – proces jsme ukončili natvrdo (jako zavření okna), aplikace nemá šanci nic zapsat. Restart poznáme podle nového řádku „start“. |

**Doplněný log (commit `d008833` a `50100c6`, `run.py`):** logger aplikace má čas u každého záznamu a při startu se zapíše jeden řádek. Do logu jde jen název souboru DB, plná cesta obsahuje jméno uživatele. První verze nastavila formát globálně a Werkzeug pak psal do souboru ANSI barvy, proto `50100c6` formát omezuje jen na logger `charging`.

Výřez logu po doplnění (běh na `50100c6`, cesty zkrácené):
```
2026-11-16 10:32:04,538 INFO charging: VoltBook start: databáze c10.db, schéma v5
127.0.0.1 - - [16/Nov/2026 10:32:05] "POST /reservations/2/confirm HTTP/1.1" 409 -
2026-11-16 10:32:16,509 ERROR charging: Exception on /reservations [POST]
  File "src\charging\auth.py", line 29, in load_logged_in_user
sqlite3.OperationalError: database is locked
2026-11-16 10:32:26,994 ERROR charging: Outbox dispatch failed
sqlite3.OperationalError: database is locked
127.0.0.1 - - [16/Nov/2026 10:32:26] "POST /reservations HTTP/1.1" 500 -
2026-11-16 10:32:33,162 INFO charging: VoltBook start: databáze c10.db, schéma v5
127.0.0.1 - - [16/Nov/2026 10:32:33] "GET /api/reservations/1 HTTP/1.1" 200 -
```

**Nález z logu (I-03, otevřené):** po chybě 500 ještě běží doručování outboxu po POST (`after_request`), které na zamčené DB čeká dalších 10 s. Uživatel tak na chybu čekal 21 s místo 10 s a v logu jsou k jednomu requestu dvě chyby. Oprava by byla malá (po odpovědi 5xx outbox nedoručovat), ale zatím jsme ji nedělali – jen zapisujeme.

Ověřeno i po změně `run.py`: `pytest` 107 passed, `verify_c02.py` 14/14, `verify_c08.py` 29/29, `measure_c04.py` (spouští `run.py`) splněno, p95 OP-03 280 ms, 0 výjimek v logu. Nic nebylo BLOCKED. Neověřili jsme řádné zastavení přes Ctrl+C.

**Shrnutí:**
- *Co logovat:* technické chyby (výjimky, nedoručená notifikace z outboxu) a start aplikace s tím, nad jakou DB běží. Úspěch a doménové zamítnutí (409) stačí přístupový log a odpověď, vlastní záznam by byl jen šum. Rozhodnutí admina by časem chtělo audit (R-05), ale to je jiná věc než provozní log.
- *Co jsme z logů opravdu zjistili:* kdy a kde výpadek DB nastal (první čtení uživatele v `auth.py`), jakou chybou (`database is locked`) a že aplikace odpověděla 500. Taky že outbox po chybě zbytečně čeká (I-03). Po doplnění i kdy aplikace startovala a s jakou DB.
- *Co jsme museli ověřit jinak:* že se při výpadku nic nezapsalo, že kolize nechala stav beze změny a že rezervace přežily restart – to log neřekne, to jsme četli přímo z DB.
