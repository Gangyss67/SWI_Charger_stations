"""Stálé téma pro notifikace na telefon (ntfy). Každý uživatel dostane jedno při založení účtu
(nebo při migraci DB) a nemění se – zapnutí/vypnutí posílání téma nemění, takže v telefonu stačí
téma přidat jednou. Nové téma vznikne jen na výslovnou žádost uživatele (když ho zná někdo cizí)."""
from __future__ import annotations

import secrets

# Téma se opisuje do telefonu a ntfy rozlišuje velikost písmen, proto jen malá písmena a číslice
# bez záměnných znaků (l/1, o/0, i) ve skupinách po 4: "voltbook-k7m3-q9xp-h2td". 31^12 ≈ 2^59 možností.
TOPIC_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_push_topic() -> str:
    groups = ("".join(secrets.choice(TOPIC_ALPHABET) for _ in range(4)) for _ in range(3))
    return "voltbook-" + "-".join(groups)
