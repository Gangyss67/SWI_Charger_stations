"""Ochrana proti CSRF pro všechny POST požadavky (formuláře i JSON API)."""
from __future__ import annotations

import hmac
import secrets

from flask import abort, request, session

CSRF_SESSION_KEY = "_csrf_token"


def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def init_app(app) -> None:
    @app.before_request
    def csrf_protect():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        expected = session.get(CSRF_SESSION_KEY)
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        if not expected or not hmac.compare_digest(expected, sent):
            abort(400)
        return None

    app.jinja_env.globals["csrf_token"] = get_csrf_token
