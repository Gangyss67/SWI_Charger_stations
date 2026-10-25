# C06 — Assessment Form (CZ)

## Projekt

```text
Hodnotící tým: ParkingCrew
Hodnocený tým: VoltBook
Projekt: SWI_Charger_stations – rezervace nabíjecích stanic
Assessed commit/tag: main (v0.5.0)
Datum: 25. 10. 2026
```

## Stavy

`YES / PARTLY / NO / N/A`

Každý řádek musí obsahovat Evidence.

## Baseline

| ID | Odpověď | Evidence / komentář |
|---|---|---|
| B1 Stejná identifikovaná baseline pro docs/code/tests | YES | Docs, kód i testy jsou v jedné verzi. Jen text specifikace C02 (docx) tým drží mimo repo. |
| B2 Relevantní produkt nebo testy lze spustit | YES | Čistý venv, `python -m pytest`: 99 testů OK. Skript s příklady C02 14/14 OK. |

## C02 — Engineering Specification

Vybraná operace: OP-03 Confirm Reservation (DRAFT → CONFIRMED / PENDING_APPROVAL)  
Vybraný business rule / invariant: BR-02 – blokující rezervace (CONFIRMED, PENDING_APPROVAL) téže stanice se nepřekrývají

| ID | Odpověď | Evidence / komentář |
|---|---|---|
| R1 Operation: input + precondition + postcondition/state | YES | Aktivitní diagram OP-03 a README: vstup, podmínky (BR-05, BR-06, BR-02), výsledný stav CONFIRMED / PENDING_APPROVAL, jinak zůstává DRAFT. |
| R2 Business rule/invariant je jednoznačný | YES | Dvě blokující rezervace jedné stanice se nesmí překrývat, interval [start, end). DRAFT neblokuje. |
| R3 Operation/rule je konzistentní se state modelem | YES | Stavový diagram v0.2 sedí: DRAFT neblokuje, PENDING_APPROVAL i CONFIRMED blokují. Přechody hlídá i DB. |
| R4 Implementace odpovídá specifikované semantics | YES | `confirm()` → ConfirmationPolicy + AvailabilityPolicy v jedné transakci, navíc pojistka v DB triggeru. |
| R5 Existuje vhodná verification evidence | YES | Příklady C02 (V2, V2b) a test souběhu: 10 vláken potvrzuje stejný slot, projde právě jedno. |

## C03 — Architecture

Vybraná responsibility: doručení notifikace + retry (Notification Integration)  
Vybraná interaction: H1 Approve  
Vybrané ADR/decision: ADR-01 – transakční outbox

| ID | Odpověď | Evidence / komentář |
|---|---|---|
| A1 Responsibility + owner jsou explicitní | YES | G2: owner je Notification Integration (OutboxDispatcher), vlastní stav doručení. |
| A2 Skutečný code ownership odpovídá architektuře | YES | V kódu stejně, OutboxDispatcher. Hranici hlídá test architektury. |
| A3 Dynamic interaction odpovídá skutečnému call path | YES | Sekvence H1 sedí s kódem: transakce, změna stavu, zápis do outboxu, COMMIT, pak dispatch. |
| A4 ADR/decision odpovídá současné implementaci | PARTLY | Outbox v kódu je, ale ADR-01 slibuje, že se zpráva neztratí. Po 5 neúspěšných pokusech se už nedoručí → F1. |

## C04 — Quality & Measurement

Quality concern: v projektu není  
Criterion: není (vlastní nevymýšlíme)

| ID | Odpověď | Evidence / komentář |
|---|---|---|
| Q1 Criterion je skutečně evaluable | NO | C04 v projektu chybí, concern ani criterion jsme nenašli. |
| Q2 Measurement měří criterion ve správném contextu | NO |Neměřeno |
| Q3 Decision odpovídá výsledku měření | NO | Bez měření nemá rozhodnutí oporu → F2. |

## C05 — Detailed Design & Unit Verification

Vybraná software unit: rozhodnutí o potvrzení OP-03 (ConfirmationPolicy + AvailabilityPolicy)

| ID | Odpověď | Evidence / komentář |
|---|---|---|
| D1 Design intent je explicitní a pochopitelný | YES | Popsané odpovědnosti, kontrakty `decide()` / `evaluate()` a pseudokód. |
| D2 DD odpovídá skutečné implementaci | YES | `decide()` jde krok po kroku podle pseudokódu, `evaluate()` podle tabulky priorit. |
| D3 Unit/component tests jsou odvoditelné z DD | YES | Testy pojmenované podle prvků návrhu, 22 OK. |
| D4 Relevantní branches/boundaries/states/failures jsou ověřeny | YES | Hranice lhůty, navazující intervaly, priority, pořadí kontrol. Naše mutace: 9 z 10 chycených, přežila jen hranice start = teď. |
| D5 Vybraný test má smysluplný oracle/expected result | YES | Test hranice lhůty má oracle z BR-06 (10:00 − 15 min), ne z kódu. |

## Candidate findings

### F1

```text
Source: C03 – ADR-01 (outbox)
Engineering claim: zpráva se neztratí ani při výpadku Notification Service
Expected: neúspěšná událost zůstane v outboxu a zkusí se znovu (ADR-01, sekvence H1)
Observed: po 5 neúspěšných pokusech ji dispatcher už nevybere, zpráva se nedoručí
Evidence: OutboxDispatcher, MAX_ATTEMPTS = 5, pending() bere jen attempts < 5
Verification performed: dočasný test na kopii repa: 5× výpadek → nic nedoručeno, nic nečeká
Result: VERIFIED FINDING
```

### F2

```text
Source: C04 – Quality & Measurement
Engineering claim: future pressure 10× souběh, riziko SQLite při souběhu
Expected: concern → criterion → měření → rozhodnutí (zadání C04)
Observed: nic z toho v projektu není, evidence má jen CV1, CV2, CV3 a CV5
Evidence: zátěž nebyla měřena
Verification performed: prošli jsme docs, tagy a skripty
Result: VERIFIED FINDING
```

### F3

```text
Source: C02 – Engineering Specification
Engineering claim: specifikace v0.2 je v docx
Expected: text OP-03 a REQ-xx dohledatelné v repu
Observed: docx v repu není, REQ-xx jsou jen odkazy
Evidence: OP-03 je i tak popsané v diagramech, README a kontraktech C05
Verification performed: R1–R5 jsme ověřili bez docx
Result: REJECTED CANDIDATE
```

## Owner disposition

| Finding | ACCEPTED / REJECTED / DEFERRED | Reason/evidence | Action / no-change |
|---|---|---|---|
| F1 | ACCEPTED – change required | Potvrzeno i naším testem: `pending()` bere jen `attempts < 5`, takže po 5 neúspěšných pokusech se událost už nikdy nedoručí. ADR-01 přitom slibuje, že se zpráva neztratí. | Opraveno. Dispatch po POST dál zkouší jednu událost nejvýš 5× (aby nedoručitelná zpráva nebrzdila každý request), ale `scripts/dispatch_notifications.py` bere všechny nedoručené (`dispatch(max_attempts=None)`). Zpráva tak v outboxu čeká, dokud se nedoručí. Nový test `test_adr01_event_over_attempt_limit_is_not_lost_and_script_delivers_it` (bez opravy padá), ADR-01 a D5 doplněné. |
| F2 | ACCEPTED – change required | C04 jsme na cvičení nedělali, v projektu opravdu chybělo. | C04 jsme doplnili zpětně: quality concern (souběh, future pressure Q), kritérium K1–K3, měření `scripts/measure_c04.py`, rozhodnutí D9. Evidence C04 v `docs/evidence-and-evolution.md`, tag `v0.4.0`. |
| F3 | ACCEPTED – change required (hodnotitelé ho zamítli, ale souhlasíme, že specifikace má být v repu) | Specifikace v0.1 / v0.2 byla jen v `cv2.docx` mimo repo. | Specifikaci jsme z docx přepsali do Markdownu do `docs/intent-and-change.md` (sekce CV2), včetně diagramů v0.1 a tabulky, kde jsou požadavky ověřené. |
