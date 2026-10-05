"""Registrace, přihlášení a odhlášení uživatelů."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .timeutil import format_ts

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
# Hash pro neexistujícího uživatele – ověření trvá stejně dlouho, nelze tak zjišťovat existující účty.
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


@bp.before_app_request
def load_logged_in_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT u.id, u.username, u.vehicle_model_id, u.is_admin, vm.name AS vehicle_name"
            " FROM users u JOIN vehicle_models vm ON vm.id = u.vehicle_model_id WHERE u.id = ?",
            (user_id,),
        ).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Nejste přihlášeni."}), 401
            flash("Pro tuto akci se musíte přihlásit.", "error")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@bp.route("/register", methods=("GET", "POST"))
def register():
    if g.user is not None:
        return redirect(url_for("main.index"))
    models = get_db().execute("SELECT * FROM vehicle_models ORDER BY vehicle_type, max_dc_kw DESC, name").fetchall()
    form = {"username": "", "vehicle_model_id": None}
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password_again = request.form.get("password_again", "")
        vehicle_model_id = request.form.get("vehicle_model_id", type=int)
        form = {"username": username, "vehicle_model_id": vehicle_model_id}

        error = None
        if not USERNAME_RE.match(username):
            error = "Uživatelské jméno musí mít 3–32 znaků (písmena bez diakritiky, číslice, _ . -)."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků."
        elif password != password_again:
            error = "Hesla se neshodují."
        elif vehicle_model_id not in {m["id"] for m in models}:
            error = "Vyberte své vozidlo z katalogu."

        if error is None:
            try:
                get_db().execute(
                    "INSERT INTO users (username, password_hash, vehicle_model_id, is_admin, created_at)"
                    " VALUES (?, ?, ?, 0, ?)",
                    (username, generate_password_hash(password), vehicle_model_id, format_ts(datetime.now())),
                )
            except sqlite3.IntegrityError:
                error = "Toto uživatelské jméno je již obsazené."
            else:
                flash("Registrace proběhla úspěšně, můžete se přihlásit.", "success")
                return redirect(url_for("auth.login"))
        flash(error, "error")
    return render_template("auth/register.html", form=form, models=models)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user is not None:
        return redirect(url_for("main.index"))
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        password_ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, password)
        if user is not None and password_ok:
            session.clear()  # ochrana proti session fixation
            session["user_id"] = user["id"]
            flash("Přihlášení proběhlo úspěšně.", "success")
            return redirect(url_for("main.index"))
        flash("Nesprávné uživatelské jméno nebo heslo.", "error")
    return render_template("auth/login.html", username=username)


@bp.post("/logout")
def logout():
    session.clear()
    flash("Byli jste odhlášeni.", "success")
    return redirect(url_for("main.index"))
