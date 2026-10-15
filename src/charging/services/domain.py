"""Doménový slovník rezervací: stavy, typy vozů a parametry pravidel BR-04 a BR-06.

Sdílí ho ReservationService i policies.py, aby se pravidla nedefinovala na dvou místech.
"""
from __future__ import annotations

from datetime import timedelta

DRAFT = "DRAFT"
PENDING_APPROVAL = "PENDING_APPROVAL"
CONFIRMED = "CONFIRMED"
CANCELLED = "CANCELLED"
CANCELLED_BY_ADMIN = "CANCELLED_BY_ADMIN"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
STATES = (DRAFT, PENDING_APPROVAL, CONFIRMED, CANCELLED, CANCELLED_BY_ADMIN, REJECTED, EXPIRED)
BLOCKING_STATES = (CONFIRMED, PENDING_APPROVAL)  # BR-02 v0.2

VEHICLE_BEV = "BEV"
VEHICLE_PHEV = "PHEV"
VEHICLE_TYPES = (VEHICLE_BEV, VEHICLE_PHEV)

APPROVAL_LEAD = timedelta(minutes=15)  # BR-06: rozhodnout nejpozději T_start − 15 min
APPROVAL_POWER_KW = 50  # BR-06: DC (>= 50 kW) a HPC vyžadují schválení


def requires_approval(station) -> bool:
    """BR-06: DC (>= 50 kW) a HPC stanice vyžadují schválení administrátorem."""
    return station["power_kw"] >= APPROVAL_POWER_KW


def vehicle_allowed(station, vehicle_type: str) -> bool:
    """BR-04: HPC stanice jen pro BEV."""
    return not station["is_high_speed"] or vehicle_type == VEHICLE_BEV
