# blueprints/auth/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta
import re
import secrets

from forms.auth import LoginForm
from forms.common import CSRFOnlyForm
from models import User, ResetToken, Role  # dob_user model
from extensions import db, login_manager, limiter
from flask_mail import Message
from services.rbac import user_role_names
from services import impersonation
from services.impersonation import ImpersonationError
from utils.passwords import verify_password_hash

auth_bp = Blueprint("auth", __name__)


def _post_login_redirect(user):
    """Multi-role users (granted extra roles via the admin Access page) land
    on a role picker; single-role users go straight to their dashboard.
    Dashboard routing is data (Role.dashboard_endpoint), not a hardcoded
    role-string dict, so a newly created custom role works here too as soon
    as an admin assigns it a dashboard via the Roles & Permissions screen."""
    role_names = user_role_names(user)
    if len(role_names) > 1:
        return redirect(url_for("auth.choose_role"))

    role_name = next(iter(role_names), None)
    role = Role.query.filter_by(name=role_name).first() if role_name else None
    target = role.dashboard_endpoint if role else None
    if not target:
        current_app.logger.warning(
            "login_role_unmapped", extra={"user": user.username, "role": user.role}
        )
        return redirect(url_for("auth.no_dashboard"))

    current_app.logger.info(
        "login_redirect", extra={"user": user.username, "role": user.role, "target": target}
    )
    return redirect(url_for(target))


# Load user callback
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        password = form.password.data

        try:
            user = User.query.filter_by(username=username).first()
        except OperationalError:
            db.session.rollback()
            current_app.logger.exception("Database unavailable during login")
            flash("Database is unavailable. Please try again in a moment.", "danger")
            return render_template("login.html", form=form), 503

        if user and _lockout_active(user):
            flash("Account locked. Try again later.", "danger")
            return render_template("login.html", form=form), 429

        if not user or not verify_password_hash(user.password, password):
            if user:
                locked = _register_failed_login(user)
                if locked:
                    flash("Too many attempts. Account locked for 15 minutes.", "danger")
                    return render_template("login.html", form=form), 429
            flash("Invalid username or password", "danger")
            current_app.logger.info(
                "login_failed", extra={"username": username, "ip": request.remote_addr, "path": request.path}
            )
            return render_template("login.html", form=form)

        # Handle 2FA via email OTP when enabled and user has email
        if _two_fa_enabled() and user.email:
            _issue_otp(user)
            flash("Enter the code sent to your email.", "info")
            session["otp_pending"] = True
            return redirect(url_for("auth.verify_otp"))

        login_user(user)
        _clear_lockout(user)
        flash("Login successful!", "success")
        current_app.logger.info(
            "login_success", extra={"username": user.username, "ip": request.remote_addr, "path": request.path}
        )

        return _post_login_redirect(user)

    return render_template("login.html", form=form)


@auth_bp.route("/choose-role", methods=["GET"])
@login_required
def choose_role():
    role_names = sorted(user_role_names(current_user))
    endpoint_by_name = {
        r.name: r.dashboard_endpoint
        for r in Role.query.filter(Role.name.in_(role_names)).all()
    }
    roles = [{"name": name, "endpoint": endpoint_by_name.get(name)} for name in role_names]
    return render_template("choose_role.html", roles=roles)


@auth_bp.route("/no-dashboard", methods=["GET"])
@login_required
def no_dashboard():
    """Landing page for a user whose only role has no dashboard configured
    yet - a data-entry gap (admin created a role but hasn't assigned it a
    dashboard via Roles & Permissions), not something the user can fix."""
    role_names = sorted(user_role_names(current_user))
    return render_template("no_dashboard.html", role_names=role_names)


@auth_bp.route("/logout")
@login_required
@limiter.limit("30 per minute")
def logout():
    if session.get("impersonator_id"):
        impersonation.close_dangling_log(session)
    logout_user()
    flash("You have been logged out.", "info")
    current_app.logger.info(
        "logout", extra={"username": getattr(current_user, 'username', None), "ip": request.remote_addr, "path": request.path}
    )
    return redirect(url_for("auth.login"))


@auth_bp.route("/stop-impersonation", methods=["POST"])
@login_required
def stop_impersonation():
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.dashboard"))

    try:
        admin_user = impersonation.stop(session)
    except ImpersonationError as e:
        flash(str(e), "danger")
        return redirect(url_for("admin.dashboard"))

    login_user(admin_user)
    flash("Back to your own account.", "info")
    return redirect(url_for("admin.dashboard"))


@auth_bp.route("/login/otp", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def verify_otp():
    if not session.get("otp_pending"):
        flash("No OTP pending.", "warning")
        return redirect(url_for("auth.login"))

    if request.method == "GET":
        return render_template("login.html")

    code = request.form.get("code") or (request.json.get("code") if request.is_json else "")
    user_id = session.get("otp_user_id")
    if not _otp_valid(code) or not user_id:
        flash("Invalid or expired code.", "danger")
        return redirect(url_for("auth.verify_otp")), 400

    user = User.query.get(user_id)
    if not user:
        flash("Account not found.", "danger")
        return redirect(url_for("auth.login")), 400

    login_user(user)
    _clear_lockout(user)
    current_app.logger.info(
        "login_success_2fa", extra={"username": user.username, "ip": request.remote_addr, "path": request.path}
    )

    # Clear OTP session
    for key in ["otp_pending", "otp_user_id", "otp_code", "otp_expires_at"]:
        session.pop(key, None)

    flash("Login successful!", "success")
    return _post_login_redirect(user)


def _json_or_html(payload, status=200):
    if request.accept_mimetypes.best == "application/json":
        return jsonify(payload), status
    # Fallback: flash message and redirect to login
    if "message" in payload:
        flash(payload["message"], "info" if status < 400 else "danger")
    return redirect(url_for("auth.login")), status


_PW_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^\w\s]).{12,}$")


def _validate_password_complexity(pw: str) -> bool:
    return bool(_PW_PATTERN.match(pw or ""))


def _two_fa_enabled():
    return current_app.config.get("TWO_FA_EMAIL_ENABLED", False)


def _issue_otp(user):
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.utcnow() + timedelta(minutes=current_app.config.get("TWO_FA_EMAIL_EXPIRY_MIN", 10))
    session["otp_user_id"] = user.id
    session["otp_code"] = code
    session["otp_expires_at"] = expires_at.isoformat()
    if user.email:
        try:
            msg = Message(subject="Your login code", recipients=[user.email])
            msg.body = f"Your login code is: {code}\nIt expires in {current_app.config.get('TWO_FA_EMAIL_EXPIRY_MIN', 10)} minutes."
            current_app.extensions["mail"].send(msg)
        except Exception:
            current_app.logger.exception("Failed to send OTP email")
    return code


def _otp_valid(code):
    stored = session.get("otp_code")
    exp_raw = session.get("otp_expires_at")
    if not stored or not exp_raw:
        return False
    try:
        expires_at = datetime.fromisoformat(exp_raw)
    except ValueError:
        return False
    if datetime.utcnow() > expires_at:
        return False
    return secrets.compare_digest(code or "", stored)


@auth_bp.route("/reset/request", methods=["POST"])
@limiter.limit("5 per minute")
def request_password_reset():
    identifier = (request.form.get("identifier") or request.json.get("identifier") if request.is_json else "").strip()
    if not identifier:
        return _json_or_html({"message": "Identifier (username or email) is required."}, 400)

    user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()

    # Always respond success to avoid user enumeration
    success_message = "If the account exists, a reset link has been sent."

    if not user:
        return _json_or_html({"message": success_message})

    expires_at = datetime.utcnow() + timedelta(hours=2)
    token = ResetToken(user_id=user.id, expires_at=expires_at)
    db.session.add(token)
    db.session.commit()

    reset_link = url_for("auth.reset_password", token=token.token, _external=True)
    try:
        from flask_mail import Message

        msg = Message(
            subject="Password reset instructions",
            recipients=[user.email] if user.email else [],
        )
        msg.body = f"Use the link to reset your password (valid for 2 hours):\n{reset_link}"
        msg.html = f"<p>Use the link to reset your password (valid for 2 hours):</p><p><a href='{reset_link}'>{reset_link}</a></p>"
        if msg.recipients:
            current_app.extensions["mail"].send(msg)
    except Exception:
        current_app.logger.exception("Failed to send reset email")

    return _json_or_html({"message": success_message})


@auth_bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password(token):
    reset = ResetToken.query.filter_by(token=token).first()
    now = datetime.utcnow()

    if not reset or reset.is_used or reset.expires_at < now:
        return _json_or_html({"message": "Invalid or expired reset link."}, 400)

    if request.method == "GET":
        return _json_or_html({"message": "Reset link is valid."})

    new_password = (request.form.get("new_password") or request.json.get("new_password") if request.is_json else "")
    if not _validate_password_complexity(new_password):
        return _json_or_html({"message": "Password must be 12+ chars and include upper, lower, digit, and symbol."}, 400)

    reset.user.password = generate_password_hash(new_password)
    reset.user.failed_logins = 0
    reset.user.locked_until = None
    reset.mark_used()
    db.session.commit()

    return _json_or_html({"message": "Password updated. You can now log in."})


_LOCK_THRESHOLD = 5
_LOCK_WINDOW_MINUTES = 15


def _lockout_active(user):
    now = datetime.utcnow()
    return bool(user.locked_until and user.locked_until > now)


def _register_failed_login(user):
    now = datetime.utcnow()
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= _LOCK_THRESHOLD:
        user.locked_until = now + timedelta(minutes=_LOCK_WINDOW_MINUTES)
        user.failed_logins = 0  # reset counter after locking
        db.session.commit()
        current_app.logger.warning(
            "login_locked", extra={"username": user.username, "ip": request.remote_addr, "path": request.path}
        )
        return True
    db.session.commit()
    return False


def _clear_lockout(user):
    user.failed_logins = 0
    user.locked_until = None
    db.session.commit()
