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
