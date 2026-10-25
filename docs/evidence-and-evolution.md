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
