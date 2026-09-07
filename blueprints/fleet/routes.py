from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, session
from flask_login import login_required, current_user
from extensions import db, mail
from extensions import db, mail, limiter
from flask_mail import Message
from datetime import datetime, date
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from sqlalchemy.orm import joinedload
from sqlalchemy import or_
from models import Offboarding, Driver, User, Vehicle, AssignDriver, AssignDriverReport
from utils.email_utils import send_password_change_email
from utils.passwords import verify_password_hash
import os
from flask_wtf.csrf import validate_csrf, CSRFError
from forms.common import CSRFOnlyForm, ChangePasswordForm, FleetAssignForm, FleetOffboardingForm
from services import onboarding_workflow
from services.rbac import require_permission, user_can_access_dashboard, user_has_permission

fleet_bp = Blueprint("fleet", __name__)


def _validate_csrf():
    """Validate CSRF token from form or X-CSRFToken header."""
    header_token = request.headers.get("X-CSRFToken")
    if header_token:
        try:
            validate_csrf(header_token)
            return True
        except CSRFError as exc:
            current_app.logger.warning("[FLEET] Header CSRF failed: %s", exc)
            return False

    form = CSRFOnlyForm()
    return form.validate_on_submit()


def _release_driver_vehicle_assignments(driver, released_at):
    active_assignments = (
        AssignDriver.query
        .filter_by(driver_id=driver.id, status="active")
        .order_by(AssignDriver.id.desc())
        .all()
    )

    if not active_assignments:
        driver.car_details = None
        driver.assignment_date = None
        driver.tamm_authorized = False
        return 0

    vehicle_ids = []
    for assignment in active_assignments:
        assignment.status = "inactive"
        vehicle_ids.append(assignment.vehicle_id)

        if assignment.vehicle:
            assignment.vehicle.status = "available"

    unique_vehicle_ids = sorted(set(vehicle_ids))
    if unique_vehicle_ids:
        (
            AssignDriverReport.query
            .filter(
                AssignDriverReport.driver_id == driver.id,
                AssignDriverReport.vehicle_id.in_(unique_vehicle_ids),
                AssignDriverReport.status == "assign",
                AssignDriverReport.unassign_date.is_(None),
            )
            .update(
                {
                    AssignDriverReport.unassign_date: released_at,
                    AssignDriverReport.status: "unassign",
                },
                synchronize_session=False,
            )
        )

    driver.car_details = None
    driver.assignment_date = None
    driver.tamm_authorized = False

    return len(active_assignments)

# -------------------------
# Fleet Manager Dashboard
# -------------------------
@fleet_bp.route("/dashboard")
@login_required
def dashboard_fleet():
    if not user_can_access_dashboard(current_user, "fleet.dashboard_fleet"):
        flash("Access denied", "danger")
        return redirect(url_for("auth.login"))

    q = (request.args.get("q") or "").strip()

    total_drivers = Driver.query.filter_by(onboarding_stage="Fleet Manager").count()
    offboarding_fleet = Offboarding.query.filter_by(status="Fleet").count()
    offboarding_pending_tamm = Offboarding.query.filter_by(status="pending_tamm").count()

    onboarding_drivers_query = (
        Driver.query
        #.options(joinedload(Driver.platforms))
        .filter_by(onboarding_stage="Fleet Manager")
    )
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

    # Latest offboarding per driver
    latest_requests_subq = (
        db.session.query(
            Offboarding.driver_id,
            db.func.max(Offboarding.requested_at).label("latest_request")
        )
        .filter(Offboarding.status.in_(["Fleet",  "FinanceManager"]))
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
        .options(joinedload(Offboarding.driver))
    )
    if q:
        pattern = f"%{q}%"
        offboarding_requests_query = offboarding_requests_query.join(Offboarding.driver).filter(
            or_(
                Driver.name.ilike(pattern),
                Driver.iqaama_number.ilike(pattern),
                Driver.driver_id.ilike(pattern),
            )
        )
    offboarding_requests = offboarding_requests_query.order_by(Offboarding.requested_at.desc()).all()

    # Available vehicles (not assigned and marked available)
    assigned_vehicle_ids = (
        db.session.query(AssignDriver.vehicle_id)
        .filter(AssignDriver.status == "active")
        .subquery()
    )

    available_vehicles = (
        Vehicle.query
        .filter(
            Vehicle.status == "available",
            ~Vehicle.id.in_(assigned_vehicle_ids.select())
        )
        .order_by(Vehicle.registration_number)
        .all()
    )

    return render_template(
        "dashboard_fleet.html",
        onboarding_drivers=onboarding_drivers,
        offboarding_requests=offboarding_requests,
        total_drivers=total_drivers,
        total_users=offboarding_fleet,
        total_tamm=offboarding_pending_tamm,
        available_vehicles=available_vehicles,
        q=q,
        can_assign_vehicle=user_has_permission(current_user, "onboarding.fleet_manager.approve"),
        can_clear_offboarding=user_has_permission(current_user, "offboarding.fleet_manager.clear"),
    )
# -------------------------
# Assign Vehicle & Send to Next Stage
# -------------------------
@fleet_bp.route("/assign_vehicle/<int:driver_id>", methods=["POST"])
@login_required
@require_permission("onboarding.fleet_manager.approve")
def assign_vehicle(driver_id):
    """Fleet Manager assigns vehicle and sends driver to next stage."""
    form = FleetAssignForm()
    if not form.validate_on_submit():
        return jsonify({"success": False, "message": "Invalid CSRF token."}), 400

    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "Fleet Manager":
        return jsonify({
            "success": False,
            "message": f"Driver is not in Fleet Manager stage (current: {driver.onboarding_stage}).",
        }), 400

    # Gather form data
    vehicle_id = form.vehicle_id.data
    assignment_date = form.assignment_date.data
    tamm_authorized = bool(form.tamm_authorized.data)
    tamm_file = request.files.get("tamm_authorization_ss")

    # Validate required fields
    if not (vehicle_id and assignment_date and tamm_authorized):
        return jsonify({
            "success": False,
            "message": "Vehicle selection, assignment date, and TAMM Authorization are required."
        }), 400

    if not tamm_file or not tamm_file.filename:
        return jsonify({
            "success": False,
            "message": "TAMM Authorization Screenshot is required before approval."
        }), 400

    try:
        # Validate date
        if assignment_date > date.today():
            return jsonify({"success": False, "message": "Assignment date cannot be in the future."}), 400

        vehicle = Vehicle.query.get(vehicle_id)
        if not vehicle:
            return jsonify({"success": False, "message": "Selected vehicle not found."}), 404

        if vehicle.status != "available":
            return jsonify({"success": False, "message": "Vehicle is not available."}), 400

        vehicle_plate = vehicle.registration_number
        vehicle_details = f"{vehicle.make} {vehicle.model}" if vehicle.make and vehicle.model else "N/A"

        # Ensure driver and vehicle are not already actively assigned
        existing_driver_assignment = AssignDriver.query.filter_by(driver_id=driver.id, status="active").first()
        if existing_driver_assignment:
            return jsonify({"success": False, "message": "Driver is already assigned to a vehicle."}), 400

        existing_vehicle_assignment = AssignDriver.query.filter_by(vehicle_id=vehicle.id, status="active").first()
        if existing_vehicle_assignment:
            return jsonify({"success": False, "message": "Vehicle is already assigned to another driver."}), 400

        # Generate safe filename
        ext = os.path.splitext(tamm_file.filename)[1].lower()
        safe_name = secure_filename(
            f"{driver.name}_{driver.iqaama_number}_{vehicle.registration_number}_TAMM_Authorisation{ext}"
        )
        upload_path = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
        tamm_file.save(upload_path)

        # Update driver record and assignment tables
        driver.car_details = f"{vehicle.registration_number} - {vehicle.make}/{vehicle.model}"
        driver.assignment_date = assignment_date
        driver.tamm_authorized = True
        driver.tamm_authorization_ss = safe_name
        driver.mark_fleet_manager_approved()

        assignment_link = AssignDriver(
            vehicle_id=vehicle.id,
            driver_id=driver.id,
            assign_date=assignment_date,
            status="active",
            tam_authorization=bool(tamm_authorized)
        )
        db.session.add(assignment_link)

        assignment_report = AssignDriverReport(
            vehicle_id=vehicle.id,
            driver_id=driver.id,
            add_date=assignment_date,
            status="assign"
        )
        db.session.add(assignment_report)

        vehicle.status = "assigned"

        # Next stage is driver-type-driven (see services/onboarding_workflow.py):
        # Sponsor sequences continue to Finance; Freelancer/Manpower sequences
        # skip straight to HR Final since they have no Finance stage.
        next_stage = onboarding_workflow.advance(driver, from_stage="Fleet Manager")
        if next_stage == "Finance":
            recipients = [
                u.email for u in User.query.filter(User.role.in_(["Finance", "FinanceManager"])).all() if u.email
            ]
        else:
            recipients = [
                u.email for u in User.query.filter(User.role.in_(["HR", "HRManager"])).all() if u.email
            ]

        db.session.commit()

        # Send notification email; swallow network failures so assignment succeeds even if email fails.
        if recipients:
            msg = Message(
                subject=f"Driver Assigned Vehicle & Ready for next Stage | تم تعيين مركبة للسائق وجاهز لمرحلة  {driver.name}",
                recipients=recipients
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                    <!-- English -->
                    <div style="text-align: left;">
                        <h2 style="color: #713183;">Driver Vehicle Assignment</h2>
                        <p>Hello {next_stage} Team,</p>
                        <p>Driver <strong>{driver.name}</strong> has been assigned a vehicle by the Fleet Manager.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Plate</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{vehicle_plate or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Details</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{vehicle_details or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Assignment Date</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.assignment_date.strftime('%Y-%m-%d') if driver.assignment_date else "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>TAMM Authorized</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">✅ Yes (Screenshot uploaded)</td>
                            </tr>
                        </table>
                        <p>Please log in and complete your next stage: <a href="https://dobs.dobs.cloud/login</" target="_blank">Login Here</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <!-- Arabic -->
                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #713183;">تعيين مركبة للسائق</h2>
                        <p>فريق {next_stage} المحترم،</p>
                        <p>تم تعيين مركبة للسائق <strong>{driver.name}</strong> بواسطة مدير الأسطول.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">اللوحة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{vehicle_plate or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">التفاصيل</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{vehicle_details or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تاريخ التعيين</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.assignment_date.strftime('%Y-%m-%d') if driver.assignment_date else "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">TAMM مصرح</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">✅ نعم (تم رفع لقطة الشاشة)</td>
                            </tr>
                        </table>
                        <p>يرجى تسجيل الدخول وإتمام المرحلة التالية: <a href="https://dobs.dobs.cloud/login</" target="_blank">Login هنا</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <p style="text-align:center;">Regards / مع التحية,<br>Fleet Team / فريق الأسطول</p>

                </div>
            </body>
            </html>
            """

            try:
                mail.send(msg)
                current_app.logger.info(f"[FLEET] {next_stage} notification email sent successfully.")
            except Exception as mail_err:
                current_app.logger.error(f"[FLEET] Notification email failed: {mail_err}")
        else:
            current_app.logger.warning(f"[FLEET] No {next_stage} users with email found. Skipping notification.")

        return jsonify({
            "success": True,
            "message": f"✅ Vehicle assigned to {driver.name} and driver moved to {next_stage} stage."
        }), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[FLEET] Error assigning vehicle: {e}")
        return jsonify({"success": False, "message": f"❌ Error assigning vehicle: {str(e)}"}), 500


# -------------------------
# Fleet Manager Offboarding / TAMM Revocation (Unified)
# -------------------------
@fleet_bp.route("/api/offboarding_action/<int:offboarding_id>", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
@require_permission("offboarding.fleet_manager.clear")
def offboarding_action(offboarding_id):
    if not _validate_csrf():
        return jsonify({"success": False, "message": "Invalid CSRF token."}), 400

    record = Offboarding.query.get_or_404(offboarding_id)

    if record.status != "Fleet":
        return jsonify({"success": False, "message": f"Offboarding is not in Fleet stage (current: {record.status})."}), 400

    data = request.get_json(silent=True) or {}
    driver_name = record.driver.name if record.driver else "Driver"

    try:
        now = datetime.utcnow()
        fleet_damage_report = data.get("fleet_damage_report")
        fleet_damage_cost = float(data.get("fleet_damage_cost") or 0)
        car_returned = bool(data.get("car_returned"))
        tamm_revoked = bool(data.get("tamm_revoked")) or bool(record.tamm_revoked)
        tamm_revoked_at = now if data.get("tamm_revoked") else record.tamm_revoked_at
        active_assignment_count = 0
        released_assignment_count = 0

        if record.driver_id:
            active_assignment_count = (
                AssignDriver.query
                .filter_by(driver_id=record.driver_id, status="active")
                .count()
            )

        if active_assignment_count and not car_returned:
            return jsonify({
                "success": False,
                "message": "Car must be marked returned before fleet offboarding can continue for a driver with an active vehicle assignment.",
            }), 400

        if record.driver and car_returned:
            released_assignment_count = _release_driver_vehicle_assignments(record.driver, now)
            current_app.logger.info(
                "[FLEET OFFBOARDING] Released %s active assignment(s) for driver_id=%s during offboarding_id=%s",
                released_assignment_count,
                record.driver_id,
                record.id,
            )

        base_updates = {
            "fleet_cleared": True,
            "fleet_cleared_at": now,
            "fleet_damage_report": fleet_damage_report,
            "fleet_damage_cost": fleet_damage_cost,
            "tamm_revoked": tamm_revoked,
            "tamm_revoked_at": tamm_revoked_at,
        }

        # Fleet always hands off to Finance next - Finance does the
        # calculations, then HR gives the final verdict. There is no
        # shortcut from Fleet straight to "fully offboarded."
        Offboarding.query.filter_by(id=record.id).update(
            {**base_updates, "status": "Finance"},
            synchronize_session=False,
        )
        db.session.commit()

        # Notify Finance
        finance_users = User.query.filter(User.role.in_(["FinanceManager", "Finance"])).all()
        emails = [f.email for f in finance_users if f.email]
        if emails and record.driver:
            msg = Message(
                subject=f"Driver Sent to Finance Manager | تحويل السائق إلى المالية: {driver_name}",
                recipients=emails
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                    <!-- English -->
                    <div style="text-align: left;">
                        <h2 style="color: #713183;">Driver Sent to Finance Manager</h2>
                        <p>Dear Finance Team,</p>
                        <p>Driver <strong>{driver_name}</strong> (Iqama: <strong>{record.driver.iqaama_number or "N/A"}</strong>) has been cleared by the Fleet Manager.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Fleet Damage Report</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{fleet_damage_report or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Damage Cost</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{fleet_damage_cost or 0} SAR</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>TAMM Revoked</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{"Yes" if tamm_revoked else "No"}</td>
                            </tr>
                        </table>
                        <p>Please proceed with the settlement.</p>
                        <p>Login here to review: <a href="https://dobs.dobs.cloud/login</" target="_blank">Finance Dashboard</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <!-- Arabic -->
                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #713183;">تم تحويل السائق إلى المالية</h2>
                        <p>السادة فريق المالية،</p>
                        <p>تمت الموافقة على السائق <strong>{driver_name}</strong> (رقم الإقامة: <strong>{record.driver.iqaama_number or "N/A"}</strong>) من قبل مدير الأسطول.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تقرير أضرار الأسطول</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{fleet_damage_report or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تكلفة الأضرار</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{fleet_damage_cost or 0} ريال سعودي</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">إلغاء صلاحية TAMM</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{"نعم" if tamm_revoked else "لا"}</td>
                            </tr>
                        </table>
                        <p>يرجى متابعة التسوية حسب الإجراءات.</p>
                        <p>Login هنا لمراجعة: <a href="https://dobs.dobs.cloud/login</e" target="_blank">لوحة المالية</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <p style="text-align:center;">Regards / مع التحية,<br>Fleet Team / فريق الأسطول</p>

                </div>
            </body>
            </html>
            """

            try:
                mail.send(msg)
            except Exception as mail_err:
                current_app.logger.error(f"[FLEET] Finance notification email failed: {mail_err}")

        return jsonify({
            "success": True,
            "message": f"Driver {driver_name} cleared by Fleet and sent to Finance Manager.",
            "cleared_at": now.strftime("%Y-%m-%d %H:%M"),
            "damage_cost": fleet_damage_cost,
            "damage_report": fleet_damage_report,
            "tamm_revoked_at": tamm_revoked_at.strftime("%Y-%m-%d %H:%M") if tamm_revoked_at else None,
            "released_assignment_count": released_assignment_count,
        })

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[FLEET OFFBOARDING ERROR] {e}")
        return jsonify({"success": False, "message": str(e)}), 500


# -------------------------
# Change Password
# -------------------------
@fleet_bp.route("/change_password", methods=["POST"])
@limiter.limit("5 per minute")
@login_required
def change_password():
    if current_user.role != "FleetManager":
        flash("Access denied. Fleet Manager role required.", "danger")
        return redirect(url_for("auth.login"))

    form = ChangePasswordForm()
    if not form.validate_on_submit():
        flash("Invalid or missing CSRF token. Please try again.", "danger")
        return redirect(url_for("fleet.dashboard_fleet"))

    new_password = form.new_password.data
    confirm_password = form.confirm_password.data

    if not verify_password_hash(current_user.password, form.current_password.data):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("fleet.dashboard_fleet"))

    if new_password != confirm_password:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("fleet.dashboard_fleet"))

    try:
        current_user.password = generate_password_hash(new_password)
        db.session.commit()

        if send_password_change_email(current_user, new_password):
            flash("✅ Password updated and email notification sent.", "success")
        else:
            flash("✅ Password updated, but email could not be sent.", "warning")

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"[FLEET] Failed to change password: {e}")
        flash("Could not update password right now. Try again later.", "danger")

    return redirect(url_for("fleet.dashboard_fleet"))
