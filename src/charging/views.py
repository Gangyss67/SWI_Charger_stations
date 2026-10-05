"""Webové stránky a JSON API rezervačního systému (specifikace baseline v0.2)."""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .auth import admin_required, login_required
from .db import get_db
from .services import notifications
from .services.notifications import DatabaseChannel, NotificationService
from .services.outbox import OutboxDispatcher
from .services.reservations import (
    BOOKING_HORIZON_DAYS,
    CANCELLED,
    CANCELLED_BY_ADMIN,
    CONFIRMED,
    DRAFT,
    EXPIRED,
    MAX_SLOTS,
    PENDING_APPROVAL,
    REJECTED,
    VEHICLE_BEV,
    VEHICLE_TYPES,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
    ReservationError,
    ReservationService,
    parse_slot_start,
    requires_approval,
)
from .timeutil import ceil_to_slot, time_options

bp = Blueprint("main", __name__)

ADMIN_TABS = (PENDING_APPROVAL, CONFIRMED, DRAFT, REJECTED, EXPIRED, CANCELLED, CANCELLED_BY_ADMIN, "ALL")
TAB_LABELS = {
    PENDING_APPROVAL: "Ke schválení",
    CONFIRMED: "Potvrzené",
    DRAFT: "Návrhy",
    REJECTED: "Zamítnuté",
    EXPIRED: "Propadlé",
    CANCELLED: "Zrušené",
    CANCELLED_BY_ADMIN: "Zrušené administrátorem",
    "ALL": "Vše",
}
WEEKDAYS = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")
MONTHS = ("ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září", "října", "listopadu", "prosince")


def _service() -> ReservationService:
    return ReservationService(get_db(), current_app.config["CLOCK"])


def _dispatcher() -> OutboxDispatcher:
    conn, clock = get_db(), current_app.config["CLOCK"]
    return OutboxDispatcher(conn, NotificationService(conn, DatabaseChannel(conn, clock)), clock)


@bp.after_app_request
def dispatch_outbox(response):
    """ADR-01: po každém POST doručíme čekající události z outboxu. Nedoručené zůstanou
    v outboxu a zkusí se znovu příště (nebo skriptem scripts/dispatch_notifications.py)."""
    if request.method == "POST":
        try:
            _dispatcher().dispatch()
        except Exception:
            current_app.logger.exception("Outbox dispatch failed")
    return response


def _status(exc: ReservationError) -> int:
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, PermissionDeniedError):
        return 403
    if isinstance(exc, (ConflictError, InvalidStateError)):
        return 409
    return 400


def _parse_day(value: str | None, minimum: date, maximum: date) -> date:
    try:
        day = date.fromisoformat(value) if value else minimum
    except ValueError:
        day = minimum
    return min(max(day, minimum), maximum)


def _day_label(day: date, today: date) -> str:
    prefix = {0: "Dnes, ", 1: "Zítra, "}.get((day - today).days, "")
    weekday = WEEKDAYS[day.weekday()]
    return f"{prefix}{weekday if prefix else weekday.capitalize()} {day.day}. {MONTHS[day.month - 1]}"


def _default_vehicle() -> str:
    return g.user["vehicle_type"] if g.user else VEHICLE_BEV


@bp.app_context_processor
def inject_pending_approvals():
    if g.get("user") and g.user["is_admin"]:
        return {"pending_approvals": _service().pending_approval_count()}
    return {"pending_approvals": 0}


@bp.route("/")
def index():
    """Parkoviště + tok z aktivitního diagramu OP-01/OP-02: uživatel zadá interval, typ vozu a SPZ,
    systém zobrazí seznam volných stanic (PHEV bez HPC), uživatel vybere stanici a vytvoří návrh.
    Dostupnost (OP-01) se počítá jen z intervalu a typu vozu, SPZ se použije až v OP-02."""
    svc = _service()
    now = svc.now()
    suggested = ceil_to_slot(now + timedelta(minutes=30))
    form = {
        "date": request.args.get("date") or suggested.date().isoformat(),
        "time": request.args.get("time") or f"{suggested:%H:%M}",
        "slots": request.args.get("slots", type=int) or 2,
        "vehicle_type": request.args.get("vehicle_type") or _default_vehicle(),
        "license_plate": request.args.get("license_plate", ""),
    }
    free_stations, excluded = None, []
    if "time" in request.args:
        try:
            start = parse_slot_start(form["date"], form["time"])
            results = svc.search_availability(start, form["slots"], form["vehicle_type"])
        except ReservationError as exc:
            flash(str(exc), "error")
        else:
            free_stations = [r for r in results if r["available"]]
            excluded = [r["name"] for r in results if r["reason"] == "VEHICLE"]
    return render_template(
        "index.html",
        stations=svc.stations_overview(),
        now=now,
        form=form,
        free_stations=free_stations,
        excluded=excluded,
        time_options=time_options(),
        max_slots=MAX_SLOTS,
        vehicle_types=VEHICLE_TYPES,
        today=now.date(),
        last_day=now.date() + timedelta(days=BOOKING_HORIZON_DAYS),
    )


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
    selected_time = request.args.get("time", "")
    if selected_time not in time_options():
        selected_time = next((s["time"] for s in slots if s["state"] == "FREE"), "")
    vehicle_type = request.args.get("vehicle_type") or _default_vehicle()
    if station["is_high_speed"]:
        vehicle_type = VEHICLE_BEV
    return render_template(
        "station.html",
        station=station,
        needs_approval=requires_approval(station),
        day=day,
        day_label=_day_label(day, today),
        today=today,
        last_day=last_day,
        prev_day=day - timedelta(days=1) if day > today else None,
        next_day=day + timedelta(days=1) if day < last_day else None,
        slots=slots,
        time_options=time_options(),
        selected_time=selected_time,
        max_slots=MAX_SLOTS,
        vehicle_types=(VEHICLE_BEV,) if station["is_high_speed"] else VEHICLE_TYPES,
        vehicle_type=vehicle_type,
        license_plate=request.args.get("license_plate", ""),
        selected_slots=request.args.get("slots", type=int) or 1,
    )


def _confirm_message(svc: ReservationService, reservation_id: int, state: str) -> str:
    if state == PENDING_APPROVAL:
        deadline = svc.get_reservation(reservation_id)["approval_deadline"]
        return (
            f"Rezervace #{reservation_id} čeká na schválení administrátorem. Termín je pro vás zablokovaný "
            f"a administrátor rozhodne nejpozději {deadline[8:10]}.{deadline[5:7]}. v {deadline[11:16]}."
        )
    return f"Rezervace #{reservation_id} je potvrzená."


@bp.post("/reservations")
@login_required
def create_reservation():
    """OP-02 Create -> DRAFT. JSON {station_id, date, time, slots, vehicle_type, license_plate} -> 201 + rezervace.
    Potvrzení (OP-03) je samostatná akce uživatele."""
    svc = _service()
    if request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Očekáván JSON objekt."}), 400
        station_id = data.get("station_id")
        if not isinstance(station_id, int) or isinstance(station_id, bool):
            return jsonify({"error": "station_id musí být celé číslo."}), 400
        try:
            start = parse_slot_start(data.get("date"), data.get("time"))
            reservation_id = svc.create(
                g.user["id"],
                station_id,
                start,
                data.get("slots"),
                data.get("vehicle_type", g.user["vehicle_type"]),
                data.get("license_plate"),
            )
        except ReservationError as exc:
            return jsonify({"error": str(exc)}), _status(exc)
        return jsonify(svc.get_reservation(reservation_id)), 201

    station_id = request.form.get("station_id", type=int)
    fields = {k: request.form.get(k, "") for k in ("date", "time", "vehicle_type", "license_plate", "slots")}
    if station_id is None:
        flash("Neplatná stanice.", "error")
        return redirect(url_for("main.index"))
    try:
        start = parse_slot_start(fields["date"], fields["time"])
        reservation_id = svc.create(
            g.user["id"],
            station_id,
            start,
            request.form.get("slots", type=int),
            fields["vehicle_type"],
            fields["license_plate"],
        )
    except NotFoundError:
        abort(404)
    except ReservationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.station_detail", station_id=station_id, **fields))

    expires = svc.get_reservation(reservation_id)["draft_expires_at"][11:]
    flash(
        f"Návrh rezervace #{reservation_id} je vytvořený a platí do {expires}. Potvrďte ho, jinak propadne.",
        "success",
    )
    return redirect(url_for("main.my_reservations"))


@bp.route("/reservations")
@login_required
def my_reservations():
    return render_template("reservations.html", reservations=_service().list_for_user(g.user["id"]))


def _run_action(operation: str, reservation_id: int, success):
    """Společné provedení OP-03/04/05: JSON klient dostane rezervaci nebo chybu se status kódem,
    formulář flash zprávu a přesměrování."""
    svc = _service()
    try:
        state = getattr(svc, operation)(g.user["id"], reservation_id)
    except ReservationError as exc:
        if request.is_json:
            return jsonify({"error": str(exc)}), _status(exc)
        if isinstance(exc, NotFoundError):
            abort(404)
        if isinstance(exc, PermissionDeniedError):
            abort(403)
        flash(str(exc), "error")
    else:
        if request.is_json:
            return jsonify(svc.get_reservation(reservation_id))
        flash(success(svc, state), "success")
    target = "main.admin_reservations" if request.form.get("next") == "admin" and g.user["is_admin"] else "main.my_reservations"
    return redirect(url_for(target))


@bp.post("/reservations/<int:reservation_id>/confirm")
@login_required
def confirm_reservation(reservation_id: int):
    return _run_action("confirm", reservation_id, lambda svc, state: _confirm_message(svc, reservation_id, state))


@bp.post("/reservations/<int:reservation_id>/cancel")
@login_required
def cancel_reservation(reservation_id: int):
    return _run_action("cancel", reservation_id, lambda svc, state: f"Rezervace #{reservation_id} je zrušená.")


@bp.post("/reservations/<int:reservation_id>/approve")
@login_required
def approve_reservation(reservation_id: int):
    # roli Admin ověřuje služba (OP-05), ne-admin dostane 403
    return _run_action("approve", reservation_id, lambda svc, state: f"Rezervace #{reservation_id} je schválená.")


@bp.post("/reservations/<int:reservation_id>/reject")
@login_required
def reject_reservation(reservation_id: int):
    return _run_action(
        "reject", reservation_id, lambda svc, state: f"Rezervace #{reservation_id} je zamítnutá a termín je znovu volný."
    )


@bp.route("/admin/reservations")
@admin_required
def admin_reservations():
    state = request.args.get("state", PENDING_APPROVAL)
    if state not in ADMIN_TABS:
        state = PENDING_APPROVAL
    return render_template(
        "admin_reservations.html",
        reservations=_service().list_all(None if state == "ALL" else state),
        state=state,
        tabs=[(tab, TAB_LABELS[tab]) for tab in ADMIN_TABS],
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


@bp.get("/api/availability")
def api_availability():
    """OP-01: ?date=2026-09-15&time=10:00&slots=2&vehicle_type=BEV[&station_id=8]"""
    svc = _service()
    vehicle_type = request.args.get("vehicle_type") or (g.user["vehicle_type"] if g.user else None)
    station_id = request.args.get("station_id", type=int)
    try:
        start = parse_slot_start(request.args.get("date"), request.args.get("time"))
        stations = svc.search_availability(start, request.args.get("slots", type=int), vehicle_type, station_id)
    except ReservationError as exc:
        return jsonify({"error": str(exc)}), _status(exc)
    return jsonify(
        {
            "start": f"{start:%Y-%m-%d %H:%M}",
            "slots": request.args.get("slots", type=int),
            "vehicle_type": vehicle_type,
            "stations": stations,
        }
    )


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


@bp.get("/api/reservations/<int:reservation_id>")
@login_required
def api_reservation(reservation_id: int):
    svc = _service()
    svc.expire_overdue()
    try:
        res = svc.get_reservation(reservation_id)
    except NotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    if res["user_id"] != g.user["id"] and not g.user["is_admin"]:
        return jsonify({"error": "Rezervace patří jinému uživateli."}), 403
    return jsonify(res)
