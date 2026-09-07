from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_required, current_user
from models import Driver, User, BusinessDriver, BusinessID, Offboarding, DriverBusinessIDS
from extensions import db, mail, limiter
from flask_mail import Message
from werkzeug.security import generate_password_hash
from datetime import datetime
from utils.email_utils import send_password_change_email
from utils.passwords import verify_password_hash
from sqlalchemy import func, or_
from flask_wtf.csrf import validate_csrf, CSRFError
from forms.common import CSRFOnlyForm, ChangePasswordForm, OpsSupervisorApproveForm
from services import onboarding_workflow, offboarding_workflow
from services.rbac import require_permission, user_can_access_dashboard, user_has_permission

ops_supervisor_bp = Blueprint("ops_supervisor", __name__)


def _driver_assigned_business_choices(driver_id):
    """Businesses this driver was already committed to by Ops Manager
    (BusinessDriver), each with its pool of active/unassigned account IDs
    (BusinessID) to choose from - Ops Supervisor picks the specific account,
    it doesn't choose the platform itself."""
    assigned_ids_subq = (
        db.session.query(DriverBusinessIDS.business_id_id)
        .filter(DriverBusinessIDS.transferred_at.is_(None))
        .subquery()
    )

    links = BusinessDriver.query.filter_by(driver_id=driver_id).all()
    choices = []
    for link in links:
        if not link.business:
            continue
        available_ids = (
            db.session.query(BusinessID)
            .filter(
                BusinessID.business_id == link.business_id,
                BusinessID.is_active == True,
                ~BusinessID.id.in_(assigned_ids_subq.select())
            )
            .all()
        )
        choices.append({
            "id": link.business.id,
            "name": link.business.name,
            "available_ids": [{"id": bid.id, "value": bid.value} for bid in available_ids],
        })
    return choices


def _validate_csrf():
    """Validate CSRF token from form or X-CSRFToken header."""
    header_token = request.headers.get("X-CSRFToken")
    if header_token:
        try:
            validate_csrf(header_token)
            return True
        except CSRFError as exc:
            current_app.logger.warning("[OPS_SUPERVISOR] Header CSRF failed: %s", exc)
            return False

    form = CSRFOnlyForm()
    return form.validate_on_submit()


# -------------------------
# Dashboard
# -------------------------
@ops_supervisor_bp.route("/dashboard")
@login_required
def dashboard_ops_supervisor():
    if not user_can_access_dashboard(current_user, "ops_supervisor.dashboard_ops_supervisor"):
        flash("Access denied", "danger")
        return redirect(url_for("auth.login"))

    q = (request.args.get("q") or "").strip()

    onboarding_drivers_query = Driver.query.filter_by(onboarding_stage="Ops Supervisor")
    if q:
        pattern = f"%{q}%"
        onboarding_drivers_query = onboarding_drivers_query.filter(
            or_(
                Driver.name.ilike(pattern),
                Driver.iqaama_number.ilike(pattern),
                Driver.driver_id.ilike(pattern),
            )
        )
    onboarding_drivers = onboarding_drivers_query.all()

    # Latest offboarding requests. Only "OpsSupervisor" stage records belong
    # here - "Requested" is still awaiting Ops Manager approval and has no
    # action Ops Supervisor can take on it yet.
    latest_requests_subq = (
        db.session.query(
            Offboarding.driver_id,
            func.max(Offboarding.requested_at).label("latest_request")
        )
        .filter(Offboarding.status == "OpsSupervisor")
        .group_by(Offboarding.driver_id)
        .subquery()
    )

    offboarding_requests_query = (
        db.session.query(Offboarding)
        .join(
            latest_requests_subq,
            (Offboarding.driver_id == latest_requests_subq.c.driver_id) &
            (Offboarding.requested_at == latest_requests_subq.c.latest_request)
        )
        .join(Offboarding.driver)
    )
    if q:
        pattern = f"%{q}%"
        offboarding_requests_query = offboarding_requests_query.filter(
            or_(
                Driver.name.ilike(pattern),
                Driver.iqaama_number.ilike(pattern),
                Driver.driver_id.ilike(pattern),
            )
        )
    offboarding_requests = offboarding_requests_query.order_by(Offboarding.requested_at.desc()).all()

    total_drivers = len(onboarding_drivers)
    total_offboardings = len(offboarding_requests)

    # Ops Supervisor only assigns the specific account ID for whichever
    # platform(s) Ops Manager already committed the driver to (BusinessDriver)
    # - not a free choice of any platform, and never an additional one.
    driver_businesses = {
        driver.id: _driver_assigned_business_choices(driver.id)
        for driver in onboarding_drivers
    }

    return render_template(
        "dashboard_ops_supervisor.html",
        total_drivers=total_drivers,
        total_offboardings=total_offboardings,
        onboarding_drivers=onboarding_drivers,
        offboarding_requests=offboarding_requests,
        driver_businesses=driver_businesses,
        bool=bool,
        q=q,
        can_approve_onboarding=user_has_permission(current_user, "onboarding.ops_supervisor.approve"),
        can_clear_offboarding=user_has_permission(current_user, "offboarding.ops_supervisor.clear"),
    )



# -------------------------
# Approve Driver (POST only)
# -------------------------
@ops_supervisor_bp.route("/approve_driver/<int:driver_id>", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
@require_permission("onboarding.ops_supervisor.approve")
def approve_driver(driver_id):
    form = OpsSupervisorApproveForm()
    if not form.validate_on_submit():
        flash("Invalid or missing CSRF token. Please try again.", "danger")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "Ops Supervisor":
        flash(f"Driver is not in Ops Supervisor stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    current_app.logger.info(f"[OPS_SUPERVISOR][START] driver_id={driver_id} form={dict(request.form)}")

    csv_platform_ids = [pid.strip() for pid in (form.platform_ids_csv.data or "").split(",") if pid.strip()]
    list_platform_ids = [pid.strip() for pid in request.form.getlist("platform_id[]") if str(pid).strip()]

    # Accept both sources to avoid silent drops when one is missing/stale in templates.
    platform_ids = []
    for pid in csv_platform_ids + list_platform_ids:
        if pid and pid not in platform_ids:
            platform_ids.append(pid)
    mobile_issued = bool(form.mobile_issued.data)

    # Ops Manager already committed this driver to specific platform(s)
    # (BusinessDriver) - Ops Supervisor only picks the account ID for those,
    # never a different platform. A driver with none assigned (e.g. no
    # platform work involved) has nothing to require here.
    assigned_business_ids = {bd.business_id for bd in BusinessDriver.query.filter_by(driver_id=driver.id).all()}

    if assigned_business_ids and not platform_ids:
        flash("Please choose the platform ID for the business(es) assigned by Ops Manager.", "danger")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    try:
        selected_platform_ints = [int(pid) for pid in platform_ids]

        selected_business_ids = (
            BusinessID.query
            .filter(
                BusinessID.id.in_(selected_platform_ints),
                BusinessID.is_active == True,
            )
            .all()
        )
        selected_business_id_ids = [int(bid.id) for bid in selected_business_ids]
        selected_business_type_ids = {int(bid.business_id) for bid in selected_business_ids if bid.business_id is not None}

        if platform_ids and not selected_business_id_ids:
            raise ValueError("No valid active business IDs found for submitted selection")

        if selected_business_type_ids - assigned_business_ids:
            raise ValueError("Can only assign IDs for the platform(s) selected by Ops Manager for this driver.")

        # -------------------------
        # Update DriverBusinessIDS (history table)
        # -------------------------
        active_links = DriverBusinessIDS.query.filter_by(
            driver_id=driver.id,
            transferred_at=None
        ).all()
        active_id_set = {link.business_id_id for link in active_links}
        new_id_set = set(selected_business_id_ids)

        # Close removed assignments for this driver
        for link in active_links:
            if link.business_id_id not in new_id_set:
                link.transferred_at = datetime.utcnow()

        # Insert new assignments / reassign if needed
        for bid_int in selected_business_id_ids:
            # If already active for this driver, keep history as-is
            if bid_int in active_id_set:
                continue

            existing = DriverBusinessIDS.query.filter_by(
                business_id_id=bid_int,
                transferred_at=None
            ).first()

            previous_driver_id = None
            if existing and existing.driver_id != driver.id:
                existing.transferred_at = datetime.utcnow()
                previous_driver_id = existing.driver_id

            new_link = DriverBusinessIDS(
                driver_id=driver.id,
                business_id_id=bid_int,  # FK to BusinessID
                previous_driver_id=previous_driver_id,
                assigned_at=datetime.utcnow(),
                transferred_at=None
            )
            db.session.add(new_link)

        # -------------------------
        # Sync business_driver using Business type IDs (businesses.id)
        # -------------------------
        selected_business_type_ids = list(selected_business_type_ids)
        if selected_business_type_ids:
            BusinessDriver.query.filter(
                BusinessDriver.driver_id == driver.id,
                ~BusinessDriver.business_id.in_(selected_business_type_ids)
            ).delete(synchronize_session=False)
        else:
            BusinessDriver.query.filter_by(driver_id=driver.id).delete(synchronize_session=False)

        existing_business_links = {
            row.business_id for row in BusinessDriver.query.filter_by(driver_id=driver.id).all()
        }
        for business_type_id in selected_business_type_ids:
            if business_type_id not in existing_business_links:
                db.session.add(BusinessDriver(driver_id=driver.id, business_id=business_type_id))

        # -------------------------
        # Keep legacy driver platform fields in sync
        # -------------------------
        business_id_map = {int(bid.id): bid for bid in selected_business_ids}

        business_names = []
        for bid_int in selected_business_id_ids:
            bid = business_id_map.get(bid_int)
            if not bid or not bid.business or not bid.business.name:
                continue
            if bid.business.name not in business_names:
                business_names.append(bid.business.name)

        driver.platform_id = ",".join(str(v) for v in selected_business_id_ids) if selected_business_id_ids else None
        driver.platform = ", ".join(business_names) if business_names else None

        # -------------------------
        # Update driver info
        # -------------------------
        driver.mobile_issued = mobile_issued
        driver.ops_supervisor_approved_at = datetime.utcnow()
        onboarding_workflow.advance(driver, from_stage="Ops Supervisor")

        db.session.commit()
        current_app.logger.info(
            f"[OPS_SUPERVISOR][SAVED] driver_id={driver.id} assigned_ids={platform_ids} "
            f"resolved_business_id_ids={selected_business_id_ids} "
            f"resolved_business_type_ids={selected_business_type_ids} "
            f"legacy_platform_id={driver.platform_id}"
        )
        flash(f"✅ Driver {driver.name} processed and sent to Fleet Manager.", "success")

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("[OPS_SUPERVISOR][ERROR] saving driver:")
        flash(f"❌ Error saving driver data: {str(e)}", "danger")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    # -------------------------
    # Notify Fleet Managers
    # -------------------------
    try:
        fleet_users = User.query.filter_by(role="FleetManager").all()
        recipients = [u.email for u in fleet_users if u.email]
        if recipients:
            msg = Message(
                subject=f"Driver Ready for Fleet Assignment | السائق جاهز لمرحلة الأسطول: {driver.name}",
                recipients=recipients
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                    <!-- English -->
                    <div style="text-align: left;">
                        <h2 style="color: #713183;">Driver Ready for Fleet Assignment</h2>
                        <p>Hello Fleet Team,</p>
                        <p>Driver <strong>{driver.name}</strong> has been processed by the Ops Supervisor and is ready for Fleet stage processing.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Assigned Business IDs</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{', '.join(platform_ids) if platform_ids else 'N/A'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Mobile Issued</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{'✅ Yes' if mobile_issued else '❌ No'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Approved At (UTC)</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.ops_supervisor_approved_at.strftime('%Y-%m-%d %H:%M')}</td>
                            </tr>
                        </table>
                        <p>Please assign a vehicle and complete the Fleet stage processing.</p>
                        <p>Login to your dashboard: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <!-- Arabic -->
                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #713183;">السائق جاهز لمرحلة الأسطول</h2>
                        <p>السادة فريق الأسطول،</p>
                        <p>تمت معالجة السائق <strong>{driver.name}</strong> من قبل مشرف العمليات وهو جاهز لمتابعة إجراءات مرحلة الأسطول.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">أرقام الأعمال المخصصة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{', '.join(platform_ids) if platform_ids else 'N/A'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تم تسليم جهاز جوال</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{'✅ نعم' if mobile_issued else '❌ لا'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تاريخ الموافقة (UTC)</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.ops_supervisor_approved_at.strftime('%Y-%m-%d %H:%M')}</td>
                            </tr>
                        </table>
                        <p>يرجى تخصيص المركبة واستكمال إجراءات مرحلة الأسطول.</p>
                        <p>تسجيل الدخول إلى لوحة القيادة: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                    </div>

                </div>
            </body>
            </html>
            """
            mail.send(msg)

    except Exception as e:
        current_app.logger.warning("[OPS_SUPERVISOR] fleet email error: %s", e)

    return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

# -------------------------
# Change Password
# -------------------------
@ops_supervisor_bp.route("/change_password", methods=["POST"])
@limiter.limit("5 per minute")
@login_required
def change_password():
    if current_user.role != "OpsSupervisor":
        flash("Access denied. Ops Supervisor role required.", "danger")
        return redirect(url_for("auth.login"))

    form = ChangePasswordForm()
    if not form.validate_on_submit():
        flash("Invalid or missing CSRF token. Please try again.", "danger")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    new_password = form.new_password.data
    confirm_password = form.confirm_password.data

    if not verify_password_hash(current_user.password, form.current_password.data):
        flash("❌ Current password is incorrect.", "danger")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    if new_password != confirm_password:
        flash("❌ New passwords do not match.", "danger")
        return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))

    try:
        current_user.password = generate_password_hash(new_password)
        db.session.commit()
        if send_password_change_email(current_user, new_password):
            flash("✅ Password updated and email notification sent.", "success")
        else:
            flash("✅ Password updated, but email could not be sent.", "warning")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[OPS_SUPERVISOR] Password update failed: {e}")
        flash("❌ Could not update password. Please try again.", "danger")

    return redirect(url_for("ops_supervisor.dashboard_ops_supervisor"))


# -------------------------
# Offboarding Dashboard
# -------------------------
@ops_supervisor_bp.route("/offboarding")
@login_required
def offboarding_dashboard():
    if current_user.role != "OpsSupervisor":
        flash("Access denied", "danger")
        return redirect(url_for("auth.login"))

    latest_requests_subq = (
        db.session.query(
            Offboarding.driver_id,
            func.max(Offboarding.requested_at).label("latest_request")
        )
        .filter(Offboarding.status == "OpsSupervisor")
        .group_by(Offboarding.driver_id)
        .subquery()
    )

    offboarding_requests = (
        db.session.query(Offboarding)
        .join(
            latest_requests_subq,
            (Offboarding.driver_id == latest_requests_subq.c.driver_id) &
            (Offboarding.requested_at == latest_requests_subq.c.latest_request)
        )
        .order_by(Offboarding.requested_at.desc())
        .all()
    )

    return render_template("dashboard_ops_supervisor.html", offboarding_requests=offboarding_requests)


# -------------------------
# Handle Ops Supervisor Clearance (API)
# -------------------------
# -------------------------
# Handle Ops Supervisor Clearance (API)
# -------------------------
@ops_supervisor_bp.route("/api/clear_offboarding/<int:offboarding_id>", methods=["POST"])
@login_required
@require_permission("offboarding.ops_supervisor.clear")
def api_clear_offboarding(offboarding_id):
    if not _validate_csrf():
        return {"success": False, "message": "Invalid CSRF token"}, 400

    record = Offboarding.query.get_or_404(offboarding_id)

    if record.status != "OpsSupervisor":
        return {"success": False, "message": f"Offboarding is not in Ops Supervisor stage (current: {record.status})."}, 400

    try:
        data = request.get_json(silent=True) or {}
        driver_name = record.driver.name if record.driver else "Driver"
        company_mobile_returned = bool(data.get("company_mobile_returned"))
        company_sim_returned = bool(data.get("company_sim_returned"))
        platform_returned = bool(data.get("platform_returned"))
        base_note = (data.get("ops_supervisor_note") or "").strip()

        # Persist returned-assets state in the existing note field because
        # the Offboarding model/table does not have dedicated boolean columns.
        assets_summary = (
            f"Company Mobile Returned: {'Yes' if company_mobile_returned else 'No'}; "
            f"Company SIM Returned: {'Yes' if company_sim_returned else 'No'}; "
            f"Platform Returned: {'Yes' if platform_returned else 'No'}"
        )
        combined_note = f"{base_note}\n{assets_summary}".strip() if base_note else assets_summary

        penalty_amount_raw = data.get("penalty_amount")
        try:
            penalty_amount = float(penalty_amount_raw) if penalty_amount_raw not in (None, "") else None
        except (TypeError, ValueError):
            penalty_amount = None
        penalty_note = (data.get("penalty_note") or "").strip() or None

        cleared_at = datetime.utcnow()
        next_status = (
            offboarding_workflow.next_status_after_ops_supervisor(record.driver)
            if record.driver else "Fleet"
        )
        update_values = {
            "ops_supervisor_cleared": True,
            "ops_supervisor_cleared_at": cleared_at,
            "ops_supervisor_note": combined_note,
            "ops_supervisor_penalty_amount": penalty_amount,
            "ops_supervisor_penalty_note": penalty_note,
            "status": next_status,
        }

        if record.driver:
            # Close active BusinessID history links for this driver.
            DriverBusinessIDS.query.filter_by(
                driver_id=record.driver_id,
                transferred_at=None,
            ).update({"transferred_at": cleared_at}, synchronize_session=False)

            # Remove active business-driver links so businesses are fully unassigned.
            BusinessDriver.query.filter_by(driver_id=record.driver_id).delete(synchronize_session=False)

            # Keep legacy fields aligned for views still reading driver.platform/platform_id.
            record.driver.platform = None
            record.driver.platform_id = None

            # If company assets were returned, clear issued mobile/device data on driver.
            if company_mobile_returned or company_sim_returned:
                record.driver.mobile_issued = False
                record.driver.issued_mobile_number = None
                record.driver.issued_device_id = None

        Offboarding.query.filter_by(id=record.id).update(update_values, synchronize_session=False)
        db.session.commit()

        # Notify whichever team is next - Fleet only applies if the driver
        # has ever had a vehicle assignment; everyone else skips straight to Finance.
        try:
            next_role = "FleetManager" if next_status == "Fleet" else "FinanceManager"
            next_team_label = "Fleet Manager" if next_status == "Fleet" else "Finance Manager"
            next_team_label_ar = "مديرو الأسطول" if next_status == "Fleet" else "المالية"
            next_users = User.query.filter_by(role=next_role).all()
            emails = [u.email for u in next_users if u.email]
            if emails and record.driver:
                msg = Message(
                    subject=f"Driver Offboarding Ready for {next_team_label} Clearance | جاهز لإجراءات {next_team_label_ar}: {driver_name}",
                    recipients=emails
                )
                msg.html = f"""
                <html>
                <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                    <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                        <!-- English -->
                        <div style="text-align: left;">
                            <h2 style="color: #713183;">Driver Offboarding Ready for {next_team_label} Clearance</h2>
                            <p>Dear {next_team_label},</p>
                            <p>Driver <strong>{driver_name}</strong> has been cleared by the Ops Supervisor and is ready for {next_team_label} processing.</p>
                            <p>Please log in to your dashboard to continue processing: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                        </div>

                        <hr style="margin: 30px 0;">

                        <!-- Arabic -->
                        <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                            <h2 style="color: #713183;">السائق جاهز لإجراءات {next_team_label_ar}</h2>
                            <p>السادة {next_team_label_ar}،</p>
                            <p>تمت الموافقة على السائق <strong>{driver_name}</strong> من قبل مشرف العمليات وهو جاهز للمرحلة التالية.</p>
                            <p>يرجى تسجيل الدخول إلى لوحة القيادة لمتابعة الإجراءات: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                        </div>

                    </div>
                </body>
                </html>
                """
                mail.send(msg)

        except Exception as e:
            current_app.logger.warning("[OPS_SUPERVISOR] Next-stage email error: %s", e)

        return {
            "success": True,
            "driver_name": driver_name,
            "cleared_at": cleared_at.strftime("%Y-%m-%d %H:%M"),
            "status": update_values["status"],
        }

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("[OPS_SUPERVISOR][API CLEAR ERROR]")
        return {"success": False, "message": str(e)}, 500
