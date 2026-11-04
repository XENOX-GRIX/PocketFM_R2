"""Flask HTTP adapter. Identity/admin hooks can be replaced independently of quotas."""

import argparse
import math
import os
from typing import Optional

from flask import Blueprint, Flask, abort, jsonify, request
from werkzeug.exceptions import HTTPException

from .config import ALGORITHMS, DEFAULT_CONFIG, Settings, load_settings
from .service import CapacityExceeded, LimiterService, PolicyConflict


def invalid(message):
    abort(422, description=message)


def json_object(required_fields):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != set(required_fields):
        invalid("Expected a JSON object with exactly: " + ", ".join(required_fields))
    return payload


def resolve_identity() -> str:
    """MVP trusts the body; later resolve a verified token/API key here."""
    value = json_object(["user_id"])["user_id"]
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        invalid("user_id must be a string of 1–128 characters")
    value = value.strip()
    if not value or not value.isprintable():
        invalid("user_id must contain printable, non-whitespace characters")
    return value


def require_admin() -> None:
    """MVP allows access; later abort with 401/403 unless the principal is an admin."""


def query_number(name, default, cast, minimum, maximum=None):
    try:
        value = cast(request.args.get(name, default))
        valid = value >= minimum and (maximum is None or value <= maximum) and math.isfinite(value)
    except (ValueError, TypeError, OverflowError):
        invalid(f"Invalid {name}")
    if not valid:
        invalid(f"{name} is outside its supported range")
    return value


def create_app(settings: Optional[Settings] = None, clock=None,
               identity_resolver=None, admin_guard=None) -> Flask:
    settings = settings or load_settings(os.environ.get("RATE_LIMIT_CONFIG", DEFAULT_CONFIG))
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    service = LimiterService(settings, clock)
    # These callables execute in a request context, so auth can inspect headers/cookies.
    app.config["IDENTITY_RESOLVER"] = identity_resolver or resolve_identity
    app.config["ADMIN_GUARD"] = admin_guard or require_admin

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(detail=exc.description), exc.code

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "storage": "memory"}

    @app.post("/v1/rate-limit/check")
    def check():
        user_id = app.config["IDENTITY_RESOLVER"]()
        try:
            result = service.check(user_id)
        except CapacityExceeded as exc:
            return jsonify(detail=str(exc)), 503
        response = jsonify(result)
        response.status_code = 200 if result["allowed"] else 429
        if not result["allowed"]:
            response.headers["Retry-After"] = str(max(1, math.ceil(result["retry_after_ms"] / 1000)))
        return response

    admin = Blueprint("admin", __name__, url_prefix="/admin")

    @admin.before_request
    def check_admin():
        return app.config["ADMIN_GUARD"]()

    @admin.get("/config")
    def configuration():
        return service.configuration()

    @admin.put("/algorithm")
    def switch():
        payload = json_object(["algorithm", "expected_version"])
        if payload["algorithm"] not in ALGORITHMS:
            invalid("Unknown algorithm")
        if type(payload["expected_version"]) is not int or payload["expected_version"] < 1:
            invalid("expected_version must be a positive integer")
        try:
            return service.switch(payload["algorithm"], payload["expected_version"])
        except PolicyConflict as exc:
            return jsonify(detail=str(exc)), 409

    @admin.get("/dashboard")
    def dashboard():
        search = request.args.get("search", "")
        if len(search) > 128:
            invalid("Search must not exceed 128 characters")
        lookback = (query_number("lookback_seconds", 3600, float, 0.001, 86400)
                    if "lookback_seconds" in request.args else None)
        limit = query_number("limit", 1000, int, 1, 1000)
        offset = query_number("offset", 0, int, 0, 1000000)
        return service.dashboard(search, lookback, limit, offset)

    app.register_blueprint(admin)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local in-memory rate limiter")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    create_app().run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
