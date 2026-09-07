import sys
import os
import time
import json
import logging
import uuid
import importlib.util
from typing import TYPE_CHECKING
from sqlalchemy import event
import redis

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, send_from_directory, session, redirect, url_for, request, make_response, g, flash
from flask_limiter.errors import RateLimitExceeded
from flask_login import logout_user, current_user
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge
from config import Config
from extensions import db, mail, login_manager, migrate, csrf, babel, limiter, cache, server_session
# Ensure models are registered with SQLAlchemy metadata for migrations
import models  # noqa: F401
from models import Role
from services.rbac import user_role_names

# Import blueprints
from blueprints.public.routes import public_bp
from blueprints.auth.routes import auth_bp
from blueprints.admin import admin_bp
from blueprints.hr.routes import hr_bp
from blueprints.ops_coordinator.routes import ops_coordinator_bp
from blueprints.ops_manager.routes import ops_manager_bp
from blueprints.ops_supervisor.routes import ops_supervisor_bp
from blueprints.fleet.routes import fleet_bp
from blueprints.finance.routes import finance_bp
from blueprints.reports.routes import reports_bp
from blueprints.sso import sso_bp
from blueprints.driver import driver_bp



def _resolve_runtime_backends(app: Flask):
    """Use Redis when configured and reachable, otherwise keep legacy-safe local backends."""
    redis_url = (app.config.get("REDIS_URL") or "").strip()
    use_redis_runtime = bool(app.config.get("USE_REDIS_RUNTIME", True)) and bool(redis_url)

    if use_redis_runtime:
        try:
            redis_client = redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)
            redis_client.ping()

            app.config["SESSION_TYPE"] = "redis"
            app.config["SESSION_REDIS"] = redis_client
            app.config["CACHE_TYPE"] = "RedisCache"
            app.config["CACHE_REDIS_URL"] = redis_url

            ratelimit_storage = app.config.get("RATELIMIT_STORAGE_URI") or app.config.get("RATELIMIT_STORAGE_URL")
            if ratelimit_storage and str(ratelimit_storage).startswith("redis://"):
                limiter_storage_uri = ratelimit_storage
            else:
                limiter_storage_uri = redis_url

            app.logger.info("Redis runtime enabled for cache/session/limiter")
            app.config["RUNTIME_BACKENDS"] = {
                "mode": "redis",
                "limiter": "redis",
                "session": "redis",
                "cache": "redis",
                "redis_url_set": bool(redis_url),
            }
            return limiter_storage_uri
        except Exception as exc:
            app.logger.warning("Redis runtime unavailable, falling back to local backends: %s", exc)

    app.config["SESSION_TYPE"] = app.config.get("SESSION_TYPE") or "filesystem"
    app.config["CACHE_TYPE"] = app.config.get("CACHE_TYPE") or "SimpleCache"
    app.config["RUNTIME_BACKENDS"] = {
        "mode": "fallback",
        "limiter": "memory",
        "session": app.config["SESSION_TYPE"],
        "cache": app.config["CACHE_TYPE"],
        "redis_url_set": bool(redis_url),
    }
    return "memory://"


def create_app():
    # --- Project root ---
    base_dir = os.path.dirname(os.path.abspath(__file__))

    # --- Flask app initialization ---
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static")
    )
    app.config.from_object(Config)
    app.secret_key = app.config.get("SECRET_KEY", "fallback_secret_key")

    limiter_storage_uri = _resolve_runtime_backends(app)

    # Ensure upload root exists
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    # --- Initialize extensions ---
    db.init_app(app)
    mail.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    cache.init_app(app)
    server_session.init_app(app)

    # Configure rate limiting from config/env for flexibility
    default_limits_raw = app.config.get("RATELIMIT_DEFAULTS")
    if default_limits_raw:
        app.config["RATELIMIT_DEFAULT"] = default_limits_raw

    # Configure rate limiter storage based on runtime backend resolution.
    storage_uri = limiter_storage_uri
    for key in [
        "RATELIMIT_STORAGE_URI",
        "RATELIMIT_STORAGE_URL",
        "RATELIMIT_STORAGE",
        "FLASK_LIMITER_STORAGE_URI",
        "FLASK_LIMITER_STORAGE_URL",
    ]:
        os.environ[key] = storage_uri
        app.config[key] = storage_uri

    # Hard override the limiter instance to avoid any env-driven redis config.
    limiter._storage_uri = storage_uri  # type: ignore[attr-defined]
    limiter._storage_options = {}
    limiter.init_app(app)

    # --- Sentry (optional) ---
    if os.getenv("SENTRY_DSN"):
        if importlib.util.find_spec("sentry_sdk"):
            try:
                import sentry_sdk  # type: ignore
                from sentry_sdk.integrations.flask import FlaskIntegration  # type: ignore

                sentry_sdk.init(
                    dsn=os.getenv("SENTRY_DSN"),
                    integrations=[FlaskIntegration()],
                    environment=os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "production",
                    traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
                )
            except Exception:  # pragma: no cover
                app.logger.exception("Failed to initialize Sentry")
        else:
            app.logger.warning("Sentry SDK not installed; skipping Sentry initialization")

    # --- Slow query logging ---
    slow_threshold_ms = app.config.get("SLOW_QUERY_MS", 500)

    def _setup_query_timing():
        with app.app_context():
            engine = db.get_engine()

            @event.listens_for(engine, "before_cursor_execute")
            def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                context._query_start_time = time.perf_counter()

            @event.listens_for(engine, "after_cursor_execute")
            def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                start = getattr(context, "_query_start_time", None)
                if start is None:
                    return
                duration_ms = int((time.perf_counter() - start) * 1000)
                if duration_ms >= slow_threshold_ms:
                    app.logger.warning(
                        "[SLOW QUERY] %sms %s params=%s", duration_ms, statement.strip().split("\n")[0][:200], parameters
                    )

    _setup_query_timing()

    # --- Localization (Babel) ---
    def select_locale():
        return session.get("lang") or request.accept_languages.best_match(app.config["LANGUAGES"]) or "en"

    babel.init_app(app, locale_selector=select_locale)

    # --- Basic logging setup (structured JSON with request id) ---
    class RequestIdFilter(logging.Filter):
        def filter(self, record):
            record.request_id = getattr(g, "request_id", None)
            return True

    class JsonFormatter(logging.Formatter):
        def format(self, record):  # pragma: no cover - formatting only
            payload = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if getattr(record, "request_id", None):
                payload["request_id"] = record.request_id
            # Flask logs unhandled exceptions via app.logger.error(..., exc_info=True)
            # (see log_exception() in flask/app.py) -- record.exc_info carries the
            # real traceback, but this formatter used to build its JSON payload by
            # hand and never looked at it, so every "Exception on /dashboard/ [GET]"
            # entry since April had NO stack trace at all, just that bare message.
            # Include it whenever present so the next occurrence is diagnosable.
            if record.exc_info:
                payload["exc_info"] = self.formatException(record.exc_info)
            elif record.exc_text:
                payload["exc_info"] = record.exc_text
            return json.dumps(payload)

    if not app.debug:
        from logging.handlers import RotatingFileHandler

        log_path = os.path.join(base_dir, "logs")
        os.makedirs(log_path, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(log_path, "app.jsonl"), maxBytes=5_000_000, backupCount=5)
        handler.setLevel(logging.INFO)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RequestIdFilter())
        app.logger.handlers = [handler]
        app.logger.setLevel(logging.INFO)

    # --- Register blueprints ---
    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/dashboard")
    app.register_blueprint(hr_bp, url_prefix="/dashboard/hr")
    app.register_blueprint(ops_coordinator_bp, url_prefix="/ops_coordinator") 
    app.register_blueprint(ops_manager_bp, url_prefix="/dashboard/ops")
    app.register_blueprint(ops_supervisor_bp, url_prefix="/dashboard/ops_supervisor")
    app.register_blueprint(fleet_bp, url_prefix="/dashboard/fleet")
    app.register_blueprint(finance_bp, url_prefix="/dashboard/finance")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(sso_bp)
    app.register_blueprint(driver_bp)

    # --- Default language before request ---
    @app.before_request
    def set_default_lang():
        g.request_started_at = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        if "lang" not in session:
            preferred = request.accept_languages.best_match(["en", "ar"])
            session["lang"] = preferred or "en"
        g.current_lang = session.get("lang", "en")
        g.text_direction = "rtl" if g.current_lang == "ar" else "ltr"

    @app.after_request
    def add_timing_header(response):
        started = getattr(g, "request_started_at", None)
        if started is not None:
            duration_ms = int((time.perf_counter() - started) * 1000)
            response.headers["X-Request-Duration-ms"] = str(duration_ms)
        if getattr(g, "request_id", None):
            response.headers["X-Request-ID"] = g.request_id

        # Avoid stale dashboard HTML/JS in browsers after hotfix deployments.
        if request.path.startswith("/dashboard"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.errorhandler(RateLimitExceeded)
    def handle_ratelimit(exc):
        app.logger.warning(
            "Rate limit exceeded", extra={"path": request.path, "ip": request.remote_addr, "endpoint": request.endpoint}
        )
        if request.accept_mimetypes.best == "application/json":
            return {"message": "Too many requests. Please slow down."}, 429
        return "Too many requests. Please slow down.", 429

    @app.errorhandler(CSRFError)
    def handle_csrf_error(exc):
        app.logger.info(
            "CSRF error", extra={"path": request.path, "endpoint": request.endpoint, "reason": getattr(exc, "description", "")}
        )
        logout_user()
        session.clear()
        return redirect(url_for("auth.login"))

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_entity_too_large(exc):
        max_len = app.config.get("MAX_CONTENT_LENGTH", 100 * 1024 * 1024)
        max_mb = int(max_len / (1024 * 1024))
        message = f"Uploaded files are too large. Please keep the full request under {max_mb} MB."
        app.logger.warning(
            "Request entity too large",
            extra={"path": request.path, "endpoint": request.endpoint, "max_mb": max_mb},
        )
        if request.accept_mimetypes.best == "application/json":
            return {"message": message}, 413
        if request.path == "/register":
            flash(message, "danger")
            return redirect(url_for("public.register"))
        return message, 413

    @app.errorhandler(403)
    def handle_forbidden(exc):
        app.logger.info(
            "Forbidden", extra={"path": request.path, "endpoint": request.endpoint}
        )
        if request.accept_mimetypes.best == "application/json":
            return {"message": "Access denied."}, 403
        flash("Access denied. You don't have permission to do that.", "danger")
        return redirect(request.referrer or url_for("auth.login"))

    @app.errorhandler(401)
    def handle_unauthorized(exc):
        if request.accept_mimetypes.best == "application/json":
            return {"message": "Please log in."}, 401
        return redirect(url_for("auth.login"))

    @app.context_processor
    def inject_lang():
        return {
            "current_lang": getattr(g, "current_lang", "en"),
            "text_direction": getattr(g, "text_direction", "ltr"),
        }

    def tr(en_text, ar_text):
        """Inline bilingual text helper, available in every template as
        {{ tr('English', 'Arabic') }} - promoted from what was previously a
        local macro duplicated per-template (see templates/reports.html)."""
        return en_text if getattr(g, "current_lang", "en") == "en" else ar_text

    app.jinja_env.globals["tr"] = tr

    # Sidebar nav, keyed by Role.dashboard_endpoint rather than a hardcoded
    # role-string elif-chain - a user holding multiple roles (granted via
    # the admin Access page) gets every held role's section merged into one
    # sidebar instead of only ever seeing their single legacy primary role.
    _NAV_CATALOG = {
        "admin.dashboard": [
            ("Overview", "نظرة عامة", [
                ("Dashboard", "لوحة التحكم", "admin.dashboard", "admin", None, "grid"),
            ]),
            ("Manage", "الإدارة", [
                ("Users", "المستخدمون", "admin.users", None, "admin.users", "users"),
                ("Workflow Configuration", "إعداد سير العمل", "admin.workflow_config", None, "admin.workflow_config", "workflow"),
                ("Driver Contract Templates", "قوالب عقود السائقين", "admin.contract_templates", None, "admin.contract_templates", "clipboard"),
                ("Promissory Note Templates", "قوالب سند لأمر", "admin.promissory_note_templates", None, "admin.promissory_note_templates", "clipboard"),
                ("Roles & Permissions", "الأدوار والصلاحيات", "admin.roles", None, "admin.roles", "shield"),
                ("Reports", "التقارير", "reports_bp.reports", "reports_bp", None, "chart"),
            ]),
        ],
        "hr.dashboard_hr": [
            ("Overview", "نظرة عامة", [
                ("HR Dashboard", "لوحة الموارد البشرية", "hr.dashboard_hr", "hr", None, "users"),
            ]),
        ],
        "fleet.dashboard_fleet": [
            ("Overview", "نظرة عامة", [
                ("Fleet Dashboard", "لوحة الأسطول", "fleet.dashboard_fleet", "fleet", None, "truck"),
            ]),
        ],
        "finance.dashboard_finance": [
            ("Overview", "نظرة عامة", [
                ("Finance Dashboard", "لوحة المالية", "finance.dashboard_finance", "finance", None, "wallet"),
            ]),
        ],
        "ops_manager.dashboard_ops": [
            ("Overview", "نظرة عامة", [
                ("Ops Dashboard", "لوحة العمليات", "ops_manager.dashboard_ops", "ops_manager", None, "clipboard"),
            ]),
        ],
        "ops_supervisor.dashboard_ops_supervisor": [
            ("Overview", "نظرة عامة", [
                ("Supervisor Dashboard", "لوحة المشرف", "ops_supervisor.dashboard_ops_supervisor", "ops_supervisor", None, "shield"),
            ]),
        ],
        "ops_coordinator.dashboard_ops_coordinator": [
            ("Overview", "نظرة عامة", [
                ("Coordinator Dashboard", "لوحة المنسق", "ops_coordinator.dashboard_ops_coordinator", "ops_coordinator", None, "swap"),
            ]),
        ],
    }

    def current_user_nav_sections():
        if not current_user.is_authenticated:
            return []

        endpoints = {
            role.dashboard_endpoint
            for role in Role.query.filter(Role.name.in_(user_role_names(current_user))).all()
            if role.dashboard_endpoint
        }

        merged = []
        label_index = {}
        seen_link_endpoints = set()

        for endpoint in endpoints:
            for label_en, label_ar, links in _NAV_CATALOG.get(endpoint, []):
                if label_en not in label_index:
                    label_index[label_en] = len(merged)
                    merged.append({"label": tr(label_en, label_ar), "links": []})
                idx = label_index[label_en]
                for name_en, name_ar, link_endpoint, blueprint, match_endpoint, icon in links:
                    if link_endpoint in seen_link_endpoints:
                        continue
                    seen_link_endpoints.add(link_endpoint)
                    merged[idx]["links"].append({
                        "name": tr(name_en, name_ar),
                        "endpoint": link_endpoint,
                        "blueprint": blueprint,
                        "match_endpoint": match_endpoint,
                        "icon": icon,
                    })

        # Stable order: Overview-style sections before Manage-style ones,
        # matching what each role previously saw on its own.
        order = ["Overview", "نظرة عامة", "Manage", "الإدارة"]
        merged.sort(key=lambda s: order.index(s["label"]) if s["label"] in order else len(order))
        return merged

    app.jinja_env.globals["current_user_nav_sections"] = current_user_nav_sections

    # --- Front page route ---
    @app.route("/")
    def front_page():
        lang = session.get("lang", "en")
        filename = "rtl_index.html" if lang == "ar" else "index.html"
        return make_response(send_from_directory(base_dir, filename))

    # --- Health endpoint for ops ---
    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    @app.route("/ops/runtime-backends")
    def runtime_backends():
        return app.config.get("RUNTIME_BACKENDS", {"mode": "unknown"}), 200

    # --- Set language route ---
    @app.route("/set_language/<lang>")
    @limiter.limit("30 per minute")
    def set_language(lang):
        if lang in ["en", "ar"]:
            session["lang"] = lang
        next_page = request.args.get("next") or url_for("front_page")
        return redirect(next_page)

    # --- CLI: backfill driver IDs ---
    @app.cli.command("backfill-driver-ids")
    def backfill_driver_ids_cmd():
        """Assign sequential driver_id values for records missing one."""

        from services.admin_service import backfill_driver_ids

        updated = backfill_driver_ids()
        print(f"Backfilled driver_id for {updated} driver(s)")

    return app


# --- Run app directly ---
if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
