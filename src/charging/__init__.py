"""Rezervační systém nabíjecích stanic (Flask + SQLite)."""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask, g, render_template

from . import db
from .timeutil import format_range


def _load_or_create_secret(instance_path: str) -> str:
    path = Path(instance_path) / "secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    return key


def create_app(test_config: dict | None = None) -> Flask:
    project_root = Path(__file__).resolve().parents[2]
    app = Flask(__name__, instance_path=str(project_root / "instance"))
    app.config.from_mapping(
        DATABASE=os.environ.get("DATABASE_PATH", str(Path(app.instance_path) / "charging.db")),
        SEED_DEMO_USERS=True,
        CLOCK=datetime.now,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or _load_or_create_secret(app.instance_path)

    db.init_app(app)

    from . import auth, security, views

    security.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)

    app.jinja_env.filters["timerange"] = format_range
    app.jinja_env.filters["hm"] = lambda value: value[11:16]
    app.jinja_env.filters["dmhm"] = lambda value: f"{value[8:10]}.{value[5:7]}. {value[11:16]}"  # 15.09. 09:45

    @app.context_processor
    def inject_unread():
        from .services.notifications import unread_count

        count = unread_count(db.get_db(), g.user["id"]) if g.get("user") else 0
        return {"unread_notifications": count}

    for code, title in ((400, "Neplatný požadavek"), (403, "Přístup odepřen"), (404, "Stránka nenalezena")):
        app.register_error_handler(
            code, lambda _e, code=code, title=title: (render_template("error.html", code=code, title=title), code)
        )

    return app
