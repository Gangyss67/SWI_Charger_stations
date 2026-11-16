"""Kontrola a sjednocení SPZ (registrační značky) podle země.

Česká SPZ může mít tři tvary:
  běžná        1AB 2345   číslice 1–9, kód kraje, písmeno nebo číslice, 4 číslice
  elektromobil EL 123AB   písmena EL, 3 číslice, 2 písmena
  na přání     TESLA3     5–8 písmen a číslic, aspoň jedna číslice
Zahraniční SPZ (sousední státy) kontrolujeme jen obecně – písmena, číslice, mezera a pomlčka.
"""
from __future__ import annotations

import re

# Mezinárodní rozlišovací znaky států, ze kterých SPZ přijímáme (CZ + sousedé).
COUNTRIES = {"CZ": "Česko", "SK": "Slovensko", "PL": "Polsko", "D": "Německo", "A": "Rakousko", "H": "Maďarsko"}
DEFAULT_COUNTRY = "CZ"
MAX_LENGTH = 12

# 2. znak české SPZ je kód kraje: A Praha, B Jihomoravský, C Jihočeský, E Pardubický, H Královéhradecký,
# J Vysočina, K Karlovarský, L Liberecký, M Olomoucký, P Plzeňský, S Středočeský, T Moravskoslezský,
# U Ústecký, Z Zlínský.
CZ_REGIONS = "ABCEHJKLMPSTUZ"
_CZ_STANDARD = re.compile(rf"[1-9][{CZ_REGIONS}][A-Z0-9]\d{{4}}")
_CZ_STANDARD_SHAPE = re.compile(r"\d[A-Z][A-Z0-9]\d{4}")  # tvar běžné SPZ, ale se špatným krajem / číslicí
_CZ_ELECTRIC = re.compile(r"EL\d{3}[A-Z]{2}")
_CZ_CUSTOM = re.compile(r"(?=[A-Z0-9]*\d)[A-Z0-9]{5,8}")
_FOREIGN = re.compile(r"[A-Z0-9](?:[A-Z0-9 -]*[A-Z0-9])?")

CZ_HELP = "Česká SPZ má tvar 1AB 2345, u elektromobilu EL 123AB, značka na přání 5–8 písmen a číslic (aspoň jedna číslice)."


class PlateError(ValueError):
    """Neplatná SPZ – text je určený pro uživatele."""


def normalize_country(value) -> str:
    country = str(value or DEFAULT_COUNTRY).strip().upper()
    if country not in COUNTRIES:
        raise PlateError("SPZ přijímáme z Česka a sousedních států (CZ, SK, PL, D, A, H).")
    return country


def normalize_plate(value, country=DEFAULT_COUNTRY) -> str:
    """Vrátí SPZ v jednotném tvaru (velká písmena, česká s mezerou: '1AB 2345', 'EL 123AB'),
    nebo vyhodí PlateError s vysvětlením."""
    country = normalize_country(country)
    plate = " ".join(str(value or "").upper().split())
    if not plate:
        raise PlateError("Vyplňte SPZ vozidla.")
    if country != "CZ":
        if len(plate) > MAX_LENGTH or not _FOREIGN.fullmatch(plate):
            raise PlateError(f"SPZ smí obsahovat jen písmena A–Z, číslice, mezeru a pomlčku (max. {MAX_LENGTH} znaků).")
        return plate

    compact = plate.replace(" ", "").replace("-", "")
    if _CZ_STANDARD.fullmatch(compact):
        return f"{compact[:3]} {compact[3:]}"
    if _CZ_ELECTRIC.fullmatch(compact):
        return f"EL {compact[2:]}"
    if _CZ_STANDARD_SHAPE.fullmatch(compact):
        if compact[0] == "0":
            raise PlateError("Česká SPZ nezačíná nulou. " + CZ_HELP)
        raise PlateError(f"Kraj „{compact[1]}“ v české SPZ neexistuje (kraje: {', '.join(CZ_REGIONS)}).")
    if _CZ_CUSTOM.fullmatch(compact):
        return compact
    raise PlateError("Neplatná SPZ. " + CZ_HELP)
