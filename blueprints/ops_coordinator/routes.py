from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_required, current_user
from datetime import datetime
from models import Driver, User
from extensions import db, mail, limiter
from flask_mail import Message
from werkzeug.security import generate_password_hash
from utils.email_utils import send_password_change_email
from utils.passwords import verify_password_hash
from forms.common import ChangePasswordForm, OpsCoordinatorOffboardingForm
from services import offboarding_workflow
from services.rbac import require_permission, user_can_access_dashboard, user_has_permission


ops_coordinator_bp = Blueprint("ops_coordinator", __name__)


def _role_key(role_raw: str) -> str:
    return "".join(ch for ch in (role_raw or "").lower() if ch.isalnum())


def _is_ops_coordinator(user) -> bool:
    return _role_key(getattr(user, "role", "")) == "opscoordinator"


# -------------------------
# Dashboard - Ops Coordinator
# -------------------------
@ops_coordinator_bp.route("/dashboard")
@login_required
def dashboard_ops_coordinator():
    if not user_can_access_dashboard(current_user, "ops_coordinator.dashboard_ops_coordinator"):
        current_app.logger.warning(
            "[ops_coordinator] access denied: role=%s id=%s",
            current_user.role,
            getattr(current_user, "id", None),
        )
        flash("Access denied. Ops Coordinator role required.", "danger")
        return redirect(url_for("auth.login"))

    q = (request.args.get("q") or "").strip()

    completed_drivers_query = (
        Driver.query
        .filter(Driver.onboarding_stage == "Completed")
        .filter(~Driver.offboarding_records.any())
        .order_by(Driver.name.asc())
    )
    if q:
        pattern = f"%{q}%"
        completed_drivers_query = completed_drivers_query.filter(
            (Driver.name.ilike(pattern)) |
            (Driver.iqaama_number.ilike(pattern)) |
            (Driver.driver_id.ilike(pattern))
        )
    completed_drivers = completed_drivers_query.all()

    return render_template(
        "dashboard_ops_coordinator.html",
        completed_drivers=completed_drivers,
        count_completed=len(completed_drivers),
        q=q,
        can_request_offboarding=user_has_permission(current_user, "offboarding.request"),
    )


# -------------------------
# Initiate Offboarding Request
# -------------------------
@ops_coordinator_bp.route("/initiate_offboarding/<int:driver_id>", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
@require_permission("offboarding.request")
def initiate_offboarding(driver_id):
    form = OpsCoordinatorOffboardingForm()
    if not form.validate_on_submit():
        flash("Invalid or missing CSRF token. Please try again.", "danger")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "Completed":
        flash("Only completed drivers can be requested for offboarding.", "warning")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    existing = offboarding_workflow.get_open_offboarding(driver)
    if existing:
        flash(f"Offboarding already requested for {driver.name}.", "info")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    reason = (form.reason.data or "").strip()

    # Legacy display flags kept in sync for any dashboard still reading them
    # directly off Driver; the real workflow state now lives on Offboarding.
    driver.offboard_request = True
    driver.offboard_requested_by = current_user.name or current_user.username
    driver.offboard_reason = reason or "No reason provided"
    driver.offboard_requested_at = datetime.utcnow()

    try:
        offboarding_workflow.request_offboarding(driver, current_user, reason=reason)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to save offboarding request.")
        flash("An error occurred. Try again.", "danger")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    try:
        managers = User.query.filter_by(role="OpsManager").all()
        recipients = [m.email for m in managers if m.email]

        if recipients:
            driver_iqaama = driver.iqaama_number or "N/A"
            coordinator_name = current_user.name or current_user.username
            reason_text = reason or "No reason provided."
            driver_city = driver.city or "N/A"

            msg = Message(
                subject="Offboarding Request Submitted | تم تقديم طلب الخروج",
                recipients=recipients,
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                    <div style="text-align: left;">
                        <h2 style="color: #004aad;">Driver Onboarding System</h2>
                        <p>Dear Ops Manager,</p>
                        <p>Ops Coordinator <strong>{coordinator_name}</strong> has requested offboarding for the following driver:</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Driver Name</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>iqaama Number</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver_iqaama}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>City / Branch</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver_city}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Requested By</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{coordinator_name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Reason</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{reason_text}</td>
                            </tr>
                        </table>
                        <p><a href="https://dobs.dobs.cloud/login">Login to the system</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #004aad;">نظام إدخال السائقين</h2>
                        <p>عزيزي مدير العمليات،</p>
                        <p>قام منسق العمليات <strong>{coordinator_name}</strong> بطلب خروج للسائق التالي:</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">اسم السائق</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">رقم الإقامة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver_iqaama}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">المدينة / الفرع</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver_city}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تم الطلب بواسطة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{coordinator_name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">السبب</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{reason_text}</td>
                            </tr>
                        </table>
                        <p><a href="https://dobs.dobs.cloud/login">تسجيل الدخول إلى النظام</a></p>
                    </div>

                </div>
            </body>
            </html>
            """
            mail.send(msg)
    except Exception:
        current_app.logger.exception("Email notification failed.")
        flash("Offboarding request saved but email could not be sent.", "warning")

    flash(f"✅ Offboarding request submitted for {driver.name}.", "success")
    return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))


# -------------------------
# Change Password
# -------------------------
@ops_coordinator_bp.route("/change_password", methods=["POST"])
@limiter.limit("5 per minute")
@login_required
def change_password():
    if not _is_ops_coordinator(current_user):
        flash("Access denied. Ops Coordinator role required.", "danger")
        return redirect(url_for("auth.login"))

    form = ChangePasswordForm()
    if not form.validate_on_submit():
        flash("Invalid or missing CSRF token. Please try again.", "danger")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    new_password = form.new_password.data
    confirm_password = form.confirm_password.data

    if not verify_password_hash(current_user.password, form.current_password.data):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    if new_password != confirm_password:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))

    try:
        current_user.password = generate_password_hash(new_password)
        db.session.commit()

        if send_password_change_email(current_user, new_password):
            flash("✅ Password updated and email notification sent.", "success")
        else:
            flash("✅ Password updated, but email could not be sent.", "warning")

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"[ops_coordinator] Failed to change password: {e}")
        flash("Could not update password right now. Try again later.", "danger")

    return redirect(url_for("ops_coordinator.dashboard_ops_coordinator"))
