"""Webové stránky a JSON API rezervačního systému."""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .auth import admin_required, login_required
from .db import get_db
from .services import notifications
from .services.notifications import DatabaseChannel, NotificationService
from .services.reservations import (
    BOOKING_HORIZON_DAYS,
    CANCELLED,
    CONFIRMED,
    DRAFT,
    MAX_SLOTS,
    PENDING,
    VEHICLE_BEV,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ReservationError,
    ReservationService,
    parse_slot_start,
)
from .timeutil import time_options

bp = Blueprint("main", __name__)


def _service() -> ReservationService:
    conn = get_db()
    clock = current_app.config["CLOCK"]
    notifier = NotificationService(conn, DatabaseChannel(conn, clock))
    return ReservationService(conn, notifier, clock)


def _parse_day(value: str | None, minimum: date, maximum: date) -> date:
    try:
        day = date.fromisoformat(value) if value else minimum
    except ValueError:
        day = minimum
    return min(max(day, minimum), maximum)


@bp.route("/")
def index():
    svc = _service()
    return render_template("index.html", stations=svc.stations_overview())


@bp.route("/stations/<int:station_id>")
def station_detail(station_id: int):
    svc = _service()
    try:
        station = svc.get_station(station_id)
    except NotFoundError:
        abort(404)
    today = svc.now().date()
    last_day = today + timedelta(days=BOOKING_HORIZON_DAYS)
    day = _parse_day(request.args.get("date"), today, last_day)
    slots = svc.availability(station_id, day, viewer_id=g.user["id"] if g.user else None)
    vehicle_blocked = bool(g.user and station["is_high_speed"] and g.user["vehicle_type"] != VEHICLE_BEV)
    selected_time = request.args.get("time", "")
    if selected_time not in time_options():
        selected_time = next((s["time"] for s in slots if s["state"] == "FREE"), "")
    return render_template(
        "station.html",
        station=station,
        day=day,
        today=today,
        last_day=last_day,
        prev_day=day - timedelta(days=1) if day > today else None,
        next_day=day + timedelta(days=1) if day < last_day else None,
        slots=slots,
        time_options=time_options(),
        selected_time=selected_time,
        max_slots=MAX_SLOTS,
        vehicle_blocked=vehicle_blocked,
    )


@bp.post("/reservations")
@login_required
def create_reservation():
    """Create reservation. Formulář -> redirect, JSON ({station_id, date, time, slots}) -> 201 + ID."""
    svc = _service()
    if request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Očekáván JSON objekt."}), 400
        station_id, slots = data.get("station_id"), data.get("slots")
        if not isinstance(station_id, int) or isinstance(station_id, bool):
            return jsonify({"error": "station_id musí být celé číslo."}), 400
        try:
            start = parse_slot_start(data.get("date"), data.get("time"))
            reservation_id = svc.create(g.user["id"], station_id, start, slots)
        except NotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ConflictError as exc:
            return jsonify({"error": str(exc)}), 409
        except ReservationError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(svc.get_reservation(reservation_id)), 201

    station_id = request.form.get("station_id", type=int)
    date_str = request.form.get("date", "")
    time_str = request.form.get("time", "")
    slots = request.form.get("slots", type=int)
    if station_id is None:
        flash("Neplatná stanice.", "error")
        return redirect(url_for("main.index"))
    try:
        start = parse_slot_start(date_str, time_str)
        reservation_id = svc.create(g.user["id"], station_id, start, slots)
    except NotFoundError:
        abort(404)
    except ReservationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.station_detail", station_id=station_id, date=date_str, time=time_str))

    if request.form.get("action") == "submit":
        try:
            svc.submit(g.user["id"], reservation_id)
        except ReservationError as exc:
            flash(f"Rezervace #{reservation_id} uložena jako koncept, ale odeslání selhalo: {exc}", "error")
        else:
            flash(f"Rezervace #{reservation_id} byla odeslána ke schválení (PENDING).", "success")
    else:
        flash(f"Rezervace #{reservation_id} uložena jako koncept (DRAFT). Nezapomeňte ji odeslat.", "success")
    return redirect(url_for("main.my_reservations"))


@bp.route("/reservations")
@login_required
def my_reservations():
    return render_template("reservations.html", reservations=_service().list_for_user(g.user["id"]))


def _run_action(action, reservation_id: int, success_message: str):
    try:
        action(g.user["id"], reservation_id)
    except NotFoundError:
        abort(404)
    except PermissionDeniedError:
        abort(403)
    except ReservationError as exc:
        flash(str(exc), "error")
    else:
        flash(success_message, "success")
    target = "main.admin_reservations" if request.form.get("next") == "admin" and g.user["is_admin"] else "main.my_reservations"
    return redirect(url_for(target))


@bp.post("/reservations/<int:reservation_id>/submit")
@login_required
def submit_reservation(reservation_id: int):
    return _run_action(_service().submit, reservation_id, f"Rezervace #{reservation_id} odeslána ke schválení.")


@bp.post("/reservations/<int:reservation_id>/approve")
@admin_required
def approve_reservation(reservation_id: int):
    return _run_action(_service().approve, reservation_id, f"Rezervace #{reservation_id} potvrzena.")


@bp.post("/reservations/<int:reservation_id>/cancel")
@login_required
def cancel_reservation(reservation_id: int):
    return _run_action(_service().cancel, reservation_id, f"Rezervace #{reservation_id} zrušena.")


@bp.route("/admin/reservations")
@admin_required
def admin_reservations():
    state = request.args.get("state", PENDING)
    if state not in (DRAFT, PENDING, CONFIRMED, CANCELLED, "ALL"):
        state = PENDING
    reservations = _service().list_all(None if state == "ALL" else state)
    return render_template(
        "admin_reservations.html",
        reservations=reservations,
        state=state,
        states=(PENDING, CONFIRMED, DRAFT, CANCELLED, "ALL"),
    )


@bp.route("/notifications")
@login_required
def notifications_page():
    return render_template("notifications.html", items=notifications.list_for_user(get_db(), g.user["id"]))


@bp.post("/notifications/read")
@login_required
def notifications_mark_read():
    notifications.mark_all_read(get_db(), g.user["id"])
    return redirect(url_for("main.notifications_page"))


# ------------------------------------------------------------------ JSON API


@bp.get("/api/stations/<int:station_id>/availability")
def api_station_availability(station_id: int):
    svc = _service()
    try:
        day = date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        return jsonify({"error": "Parametr date musí být ve formátu YYYY-MM-DD."}), 400
    try:
        slots = svc.availability(station_id, day)
    except NotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(
        {
            "station_id": station_id,
            "date": day.isoformat(),
            "slots": [{"start": s["time"], "end": f"{s['end']:%H:%M}", "state": s["state"]} for s in slots],
        }
    )


@bp.get("/api/availability/check")
def api_check_availability():
    """Check availability: ?station_id=1&date=2026-09-15&time=10:00&slots=2"""
    svc = _service()
    station_id = request.args.get("station_id", type=int)
    slots = request.args.get("slots", type=int)
    if station_id is None:
        return jsonify({"error": "Chybí station_id."}), 400
    try:
        start = parse_slot_start(request.args.get("date"), request.args.get("time"))
        available, message = svc.check_availability(station_id, start, slots, g.user["id"] if g.user else None)
    except NotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ReservationError as exc:
        return jsonify({"available": False, "message": str(exc)})
    return jsonify({"available": available, "message": message})
