# Project Frame

## Reservation domain
Co konkrétně rezervujeme?

_TODO_

## Purpose
2–3 věty: komu systém slouží a proč.

_TODO_

## Users / Stakeholders
1–3 role.

- _TODO_

## Core concepts
Reservation, Resource, User + případně 0–3 další pojmy.

- **Reservation:** _TODO_
- **Resource:** _TODO_
- **User:** _TODO_

## Core operations
- Create reservation
- Confirm / approve reservation
- Cancel reservation
- Check availability

## Persistent state
Co ukládáme o Reservation a Resource.

- **Reservation:** _TODO_
- **Resource:** _TODO_

## State-changing operation
Např. DRAFT → CONFIRMED.

_TODO_

## Common business rule
Confirmed reservations for the same resource must not overlap.

## Domain-specific business rule
Jedno vlastní pravidlo.

_TODO_

## External / system boundary
Jedna dependency. Defaultně Notification Service.

_TODO_

## Assumption
Jedna věc, kterou nyní považujete za pravdivou, ale není jistota.

_TODO_

## Unknown
Jedna důležitá věc, kterou nyní nevíte.

_TODO_

---

## Selected future pressure
Category: Q

Concrete pressure:
10× více souběžných požadavků na vytvoření a schválení rezervací (např. firemní vozový park, který v pondělí ráno hromadně rezervuje sloty na celý týden) – desítky zápisů za sekundu místo jednotek.

Why it is relevant to our reservation system:
Jádrem systému je pravidlo, že se potvrzené rezervace stejné stanice nesmí překrývat, a to musí platit i tehdy, když dva požadavky kontrolují dostupnost ve stejný okamžik. Dnes to řešíme transakcí `BEGIN IMMEDIATE` a triggerem v SQLite, která ale povoluje jen jednoho zapisujícího najednou – při vyšší souběžnosti porostou čekání na zámek a hrozí chyby `database is locked`. Tlak tedy přímo míří na naše rozhodnutí o databázi a na správnost overlap pravidla. V C01 ho neimplementujeme.
