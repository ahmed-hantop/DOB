# blueprints/hr/routes.py
from functools import wraps

from flask import Blueprint, jsonify, render_template, request, redirect, url_for, flash, current_app, send_from_directory, abort
from flask_login import login_required, current_user
from models import Driver, User, Offboarding, DriverTypeSettings, DriverDocument, SharedGeneratedDriverContract
from extensions import db, mail, csrf
from services.hr_service import process_hr_approval, save_transfer_proof, send_rejection_email
from services.rbac import require_permission, user_can_access_dashboard, user_has_permission, user_has_role
from services.file_storage import save_to_shared_storage
from flask_mail import Message
from datetime import datetime
import os
import sqlalchemy as sa
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from utils.passwords import verify_password_hash
from sqlalchemy.exc import IntegrityError

hr_bp = Blueprint("hr", __name__, url_prefix='/hr')

ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.pdf'}
DEFAULT_DRIVER_PASSWORD = "12344321"


def _upload_root() -> str:
    path = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(path, exist_ok=True)
    return path

def _allowed_filename(fn):
    _, ext = os.path.splitext(fn.lower())
    return ext in ALLOWED_EXT


def _qiwa_required(driver: Driver) -> bool:
    settings = DriverTypeSettings.query.get(driver.driver_type_id)
    return bool(settings and settings.requires_qiwa_contract)


def _static_upload_url(relative_path: str | None) -> str:
    if not relative_path:
        return ""
    return url_for("static", filename=f"uploads/{relative_path}")


def _shared_document_url(relative_path: str | None) -> str:
    """Auto-generated/manually-added contracts and promissory notes live in
    DRIVER_DOCUMENT_PATH (shared storage), not static/uploads - unlike the
    HR-approval-upload fields (company_contract_file etc.), which are
    dual-written to static/uploads and use _static_upload_url() above."""
    if not relative_path:
        return ""
    return url_for("hr.serve_shared_document", relative_path=relative_path)


@hr_bp.route("/shared-document/<path:relative_path>")
@login_required
def serve_shared_document(relative_path):
    shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH") or current_app.config.get("UPLOAD_FOLDER")
    full_path = os.path.realpath(os.path.join(shared_root, relative_path))
    if not full_path.startswith(os.path.realpath(shared_root) + os.sep):
        abort(404)
    directory, filename = os.path.split(full_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(directory, filename)


def _generated_contract_links(driver: Driver) -> list[dict]:
    from services import contracts as contracts_service
    return contracts_service.generated_contract_links(driver)


def _generated_promissory_link(driver: Driver) -> dict | None:
    from services import contracts as contracts_service
    return contracts_service.generated_promissory_link(driver)


def _signed_promissory_link(driver: Driver) -> dict | None:
    from services import contracts as contracts_service
    return contracts_service.signed_promissory_link(driver)


def _can_manage_completed_contracts(user) -> bool:
    """Retroactive contract-pack generation/upload for drivers who already
    finished onboarding (e.g. pre-dating the system) is gated by a dedicated
    permission for HR, OR the legacy Admin/SuperAdmin role check Admin's
    dashboard already uses everywhere else - not onboarding.hr.approve,
    which is semantically tied to the in-flight HR approval step."""
    return (
        user_has_permission(user, "onboarding.completed.manage_contracts")
        or user_has_role(user, "Admin", "SuperAdmin")
    )


def _require_completed_contract_access(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not _can_manage_completed_contracts(current_user):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

def _driver_assigned_platforms(driver_id):
    """Distinct platforms (businesses) this driver holds an active platform ID
    for -- via driver_business_ids -> business_ids -> businesses. Used to drive
    the per-platform Shirt/Bag checkboxes at HR Final."""
    from models import DriverBusinessIDS, BusinessID, Business
    rows = (
        db.session.query(Business.id, Business.name)
        .join(BusinessID, BusinessID.business_id == Business.id)
        .join(DriverBusinessIDS, DriverBusinessIDS.business_id_id == BusinessID.id)
        .filter(DriverBusinessIDS.driver_id == driver_id)
        .filter(DriverBusinessIDS.transferred_at.is_(None))
        .distinct()
        .all()
    )
    return [{"id": r[0], "name": r[1]} for r in rows]


def _apply_hr_final_kit(driver):
    """HR Final: read the per-platform Shirt/Bag + Driver Card checkboxes from
    request.form, write issuances + decrement stock. Shirt+Bag are required
    for every assigned platform. Returns None on success, or an error string
    (nothing written). Does NOT commit -- the caller does."""
    from services.inventory_service import issue_kit_at_hr_final
    try:
        platforms = _driver_assigned_platforms(driver.id)
        issuer_name = getattr(current_user, "name", None) or getattr(current_user, "username", None)
        return issue_kit_at_hr_final(driver, platforms, request.form, current_user.id, issuer_name)
    except Exception as e:
        # Never let a stock-bookkeeping problem 500 the HR Final action --
        # roll back the half-built issuance and surface it as a blocking
        # message the HR user can retry past.
        db.session.rollback()
        current_app.logger.exception("HR Final kit issuance failed for driver_id=%s", driver.id)
        return f"Could not record the driver kit ({e}). Nothing was saved — please try again."


def _driver_bank_prefill(driver_id):
    """bank_name / account_number / stc_pay_number for the HR approve modal."""
    from models import DriverBankAccount
    out = {"bank_name": None, "account_number": None, "stc_pay_number": None}
    rows = DriverBankAccount.query.filter_by(driver_id=driver_id).filter(DriverBankAccount.deleted_at.is_(None)).all()
    for r in rows:
        if r.account_type == DriverBankAccount.TYPE_STC_PAY:
            out["stc_pay_number"] = r.stc_pay_number
        else:
            out["bank_name"] = r.bank_name
            out["account_number"] = r.account_number
    return out


def _serialize_driver(d, with_onboarding_extras=False):
    payload = {
        "id": d.id,
        "driver_id": d.driver_id,
        "driver_type_id": d.driver_type_id,
        "name": d.name,
        "address": getattr(d, "address", None),
        "iqaama_number": d.iqaama_number,
        "iqaama_expiry": d.iqaama_expiry.isoformat() if d.iqaama_expiry else None,
        "nationality": d.nationality,
        "absher_number": d.absher_number,
        "previous_sponsor_number": d.previous_sponsor_number,
        "saudi_driving_license": bool(d.saudi_driving_license),
        "city": d.city,
        "car_details": d.car_details,
        "assignment_date": d.assignment_date.isoformat() if d.assignment_date else None,
        "issued_mobile_number": d.issued_mobile_number,
        "issued_device_id": d.issued_device_id,
        "mobile_issued": bool(d.mobile_issued),
        "iqama_card_upload": d.iqama_card_upload,
        "qiwa_required": _qiwa_required(d),
        "qiwa_contract_created": bool(d.qiwa_contract_created),
        "company_contract_created": bool(d.company_contract_created),
        "qiwa_contract_status": d.qiwa_contract_status,
        "ops_manager_approved_at": d.ops_manager_approved_at.isoformat() if d.ops_manager_approved_at else None,
        "ops_supervisor_approved_at": d.ops_supervisor_approved_at.isoformat() if d.ops_supervisor_approved_at else None,
        "fleet_manager_approved_at": d.fleet_manager_approved_at.isoformat() if d.fleet_manager_approved_at else None,
        "finance_approved_at": d.finance_approved_at.isoformat() if d.finance_approved_at else None,
        "hr_approved_at": d.hr_approved_at.isoformat() if d.hr_approved_at else None,
        "transfer_fee_paid": bool(d.transfer_fee_paid),
        "transfer_fee_amount": float(d.transfer_fee_amount) if d.transfer_fee_amount else None,
        "transfer_fee_paid_at": d.transfer_fee_paid_at.isoformat() if d.transfer_fee_paid_at else None,
        "transfer_fee_receipt": d.transfer_fee_receipt,
        "sponsorship_transfer_proof": d.sponsorship_transfer_proof,
        "tamm_authorization_ss": d.tamm_authorization_ss,
        "tamm_authorized": bool(d.tamm_authorized),
        "sponsorship_transfer_status": d.sponsorship_transfer_status,
        "onboarding_stage": d.onboarding_stage,
        "company_contract_file": d.company_contract_file,
        "promissory_note_file": d.promissory_note_file,
        "qiwa_contract_file": d.qiwa_contract_file,
        "generated_contracts": _generated_contract_links(d),
        "generated_promissory_note": _generated_promissory_link(d),
        "signed_promissory_note": _signed_promissory_link(d),
        "generate_contract_pack_url": url_for("hr.generate_contract_pack_completed", driver_id=d.id),
        "upload_signed_promissory_url": url_for("hr.upload_signed_promissory_note", driver_id=d.id),
    }

    # The bank prefill + assigned-platforms lookups are 2 extra queries per
    # driver -- only worth running for the small onboarding list (whose cards
    # open the HR / HR Final modals), never for the full Completed tab.
    if with_onboarding_extras:
        _bank = _driver_bank_prefill(d.id)
        payload["bank_name"] = _bank["bank_name"]
        payload["account_number"] = _bank["account_number"]
        payload["stc_pay_number"] = _bank["stc_pay_number"]
        payload["assigned_platforms"] = _driver_assigned_platforms(d.id)

    return payload


def _next_driver_code():
    """Generate next driver code like D0001 using a locked read to reduce race risk."""
    prefix = "D"
    row = db.session.execute(
        sa.text(
            "SELECT driver_id FROM drivers WHERE driver_id LIKE :pref ORDER BY driver_id DESC LIMIT 1 FOR UPDATE"
        ),
        {"pref": f"{prefix}%"},
    ).first()
    last_val = row[0] if row else None
    try:
        last_num = int(last_val[1:]) if last_val and last_val.startswith(prefix) else 0
    except ValueError:
        last_num = 0
    return f"{prefix}{last_num + 1:04d}"


def _ensure_driver_code(driver: Driver, retries: int = 5):
    if driver.driver_id:
        return
    for attempt in range(1, retries + 1):
        candidate = _next_driver_code()
        driver.driver_id = candidate
        try:
            with db.session.begin_nested():
                db.session.flush()
            return
        except IntegrityError:
            db.session.rollback()
            driver.driver_id = None
            if attempt == retries:
                raise ValueError("Could not assign unique driver code after retries")


def _ensure_driver_password(driver: Driver):
    if driver.password:
        return

    driver.password = generate_password_hash(DEFAULT_DRIVER_PASSWORD)


def _set_default_driver_password(driver: Driver):
    driver.password = generate_password_hash(DEFAULT_DRIVER_PASSWORD)


def _generate_driver_contracts_safe(driver: Driver, uploaded_by_id=None):
    """Business-specific Driver Contract(s), generated at HR Final now that
    Ops Supervisor has already assigned platform IDs. Logged, not raised, so
    a template/rendering issue never blocks completing onboarding."""
    try:
        from services import contracts
        contracts.generate_driver_contracts(driver, uploaded_by_id=uploaded_by_id)
    except Exception:
        current_app.logger.exception("Driver contract generation failed for driver_id=%s", driver.id)


def _serialize_offboarding(o):
    return {
        "offboarding_id": o.id,
        "driver_id": o.driver.id if o.driver else None,
        "name": o.driver.name if o.driver else "",
        "iqaama_number": o.driver.iqaama_number if o.driver else "",
        "status": o.status or "",
        "hr_note": o.hr_note or "",
        "tamm_revoked": bool(o.tamm_revoked),
        "company_contract_cancelled": bool(o.company_contract_cancelled),
        "qiwa_contract_cancelled": bool(o.qiwa_contract_cancelled),
        "pending_salary": float(getattr(o, "finance_adjustments", 0) or 0),
        "finance_note": getattr(o, "finance_note", "") or "",
        "fleet_damage_report": getattr(o, "fleet_damage_report", "") or "",
        "fleet_damage_cost": float(getattr(o, "fleet_damage_cost", 0) or 0),
        "salary_paid": bool(o.salary_paid),
        "dms_salary_balance": float(getattr(o, "dms_salary_balance", 0) or 0),
        "net_settlement_amount": float(getattr(o, "net_settlement_amount", 0) or 0),
        "settlement_direction": getattr(o, "settlement_direction", None) or "even",
        "promissory_note_issued": bool(getattr(o, "promissory_note_issued", False)),
        "payment_letter_issued": bool(getattr(o, "payment_letter_issued", False)),
        "payment_letter_due_days": getattr(o, "payment_letter_due_days", None),
    }

 
# -------------------------
# HR Dashboard
# -------------------------
@hr_bp.route("/dashboard")
@login_required
def dashboard_hr():
    if not user_can_access_dashboard(current_user, "hr.dashboard_hr"):
        flash("Access denied. HR role required.", "danger")
        return redirect(url_for("auth.login"))

    q = (request.args.get("q") or "").strip()

    def _driver_search(query):
        if not q:
            return query
        pattern = f"%{q}%"
        return query.filter(
            sa.or_(
                Driver.name.ilike(pattern),
                Driver.iqaama_number.ilike(pattern),
                Driver.driver_id.ilike(pattern),
            )
        )

    def _offboarding_search(query):
        if not q:
            return query
        pattern = f"%{q}%"
        return query.join(Offboarding.driver).filter(
            sa.or_(
                Driver.name.ilike(pattern),
                Driver.iqaama_number.ilike(pattern),
                Driver.driver_id.ilike(pattern),
            )
        )

    offboarding_driver_ids = {o.driver_id for o in Offboarding.query.all()}

    # Onboarding tab: drivers actively at the HR or HR Final stage.
    onboarding_drivers = _driver_search(
        Driver.query.filter(Driver.onboarding_stage.in_(["HR", "HR Final"]))
    ).order_by(Driver.name.asc()).all()
    total_drivers = len([d for d in onboarding_drivers if d.onboarding_stage == "HR"])
    total_users = len([d for d in onboarding_drivers if d.onboarding_stage == "HR Final"])

    # Completed Onboarded tab: onboarding finished and never entered offboarding at all
    # (once offboarding starts - even mid-process - the driver belongs under the
    # offboarding tabs instead, not here).
    completed_onboarded_driver_records = [
        d for d in _driver_search(Driver.query.filter(Driver.onboarding_stage == "Completed"))
            .order_by(Driver.name.asc()).all()
        if d.id not in offboarding_driver_ids
    ]
    completed_onboarded_drivers = [_serialize_driver(d) for d in completed_onboarded_driver_records]
    total_completed_onboarded = len(completed_onboarded_drivers)

    # Rejected tab: drivers rejected during onboarding (Ops Manager or HR first pass).
    rejected_drivers = _driver_search(
        Driver.query.filter(Driver.onboarding_stage == "Rejected")
    ).order_by(Driver.name.asc()).all()
    total_rejected = len(rejected_drivers)

    # Offboarding tab: offboarding requests that have reached HR (not yet finalized).
    offboarding_hr = _offboarding_search(
        Offboarding.query.filter(Offboarding.status == "HR")
    ).all()
    total_pending_onboarded = len(offboarding_hr)

    # Completed Offboarded tab: offboarding fully finalized by HR.
    completed_offboarded = _offboarding_search(
        Offboarding.query.filter(Offboarding.status == "Completed")
    ).all()
    total_completed_offboarded = len(completed_offboarded)

    qiwa_driver_type_ids = [
        row.driver_type_id for row in DriverTypeSettings.query.filter_by(requires_qiwa_contract=True).all()
    ]

    return render_template(
        "dashboard_hr.html",
        drivers=[_serialize_driver(d, with_onboarding_extras=True) for d in onboarding_drivers],
        completed_onboarded_drivers=completed_onboarded_drivers,
        rejected_drivers=rejected_drivers,
        offboarding_hr=[_serialize_offboarding(o) for o in offboarding_hr],
        completed_offboarded=[_serialize_offboarding(o) for o in completed_offboarded],
        total_drivers=total_drivers,
        total_users=total_users,
        total_completed_onboarded=total_completed_onboarded,
        total_pending_onboarded=total_pending_onboarded,
        total_completed_offboarded=total_completed_offboarded,
        total_rejected=total_rejected,
        q=q,
        can_approve_onboarding=user_has_permission(current_user, "onboarding.hr.approve"),
        can_complete_transfer=user_has_permission(current_user, "onboarding.hr_final.complete"),
        can_finalize_offboarding=user_has_permission(current_user, "offboarding.hr.finalize"),
        can_manage_completed_contracts=_can_manage_completed_contracts(current_user),
        qiwa_driver_type_ids=qiwa_driver_type_ids,
    )



# -------------------------
# Approve Driver & Upload Contracts
# -------------------------
@hr_bp.route("/generate_contract_pack/<int:driver_id>", methods=["POST"])
@login_required
@require_permission("onboarding.hr.approve")
def generate_contract_pack(driver_id):
    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "HR":
        flash(f"Driver is not in HR stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    try:
        from services import contracts
        contracts.generate_driver_contracts(driver, uploaded_by_id=current_user.id)
        contracts.generate_promissory_note(driver, uploaded_by_id=current_user.id)
        db.session.commit()
        flash(f"Contract pack generated for {driver.name}. Print the PDFs, collect signatures, then upload signed copies in Step 2.", "success")
    except contracts.ContractTemplateMissingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("HR contract pack generation failed for driver_id=%s", driver.id)
        flash(f"Error generating contract pack: {exc}", "danger")

    return redirect(url_for("hr.dashboard_hr"))


MANUAL_CONTRACT_ALLOWED_EXT = {'.pdf', '.doc', '.docx'}


@hr_bp.route("/add_contract_manual/<int:driver_id>", methods=["POST"])
@login_required
@require_permission("onboarding.hr.approve")
def add_contract_manual(driver_id):
    """Let HR attach a contract file they already have (e.g. prepared
    outside the system while the auto-generated pack has a rendering issue)
    without going through generate_contract_pack / services.contracts. This
    still satisfies the "a contract link exists" check in approve_driver."""
    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "HR":
        flash(f"Driver is not in HR stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    uploaded_file = request.files.get("manual_contract_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose a contract file to upload.", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    _, ext = os.path.splitext(uploaded_file.filename.lower())
    if ext not in MANUAL_CONTRACT_ALLOWED_EXT:
        flash("Invalid file type. Only PDF, DOC, or DOCX are allowed.", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    if size > max_bytes:
        flash(f"File too large. Maximum {max_bytes // (1024 * 1024)} MB.", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    try:
        from services import contracts as contracts_service

        shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH") or current_app.config.get("UPLOAD_FOLDER")
        filename = secure_filename(uploaded_file.filename)
        relative_path = save_to_shared_storage(uploaded_file, driver.id, "contract", shared_root, filename=filename)

        doc = DriverDocument(
            driver_id=driver.id,
            document_type="contract",
            file_path=relative_path,
            original_name=filename,
            file_size=size,
            uploaded_from="dobs",
            uploaded_by=current_user.id,
            notes="Manually added contract",
        )
        db.session.add(doc)
        db.session.flush()

        record = SharedGeneratedDriverContract(
            reference_number=contracts_service._next_reference_number(),
            driver_id=driver.id,
            template_id=None,
            driver_document_id=doc.id,
            business_id=None,
            generated_by=current_user.id,
            driver_type_id=driver.driver_type_id,
            generated_from_system="dobs_manual",
            generated_at=datetime.utcnow(),
            file_path=relative_path,
            original_name=filename,
            storage_disk="shared_driver_documents",
            signed_status="pending",
            notes="Manually added contract",
        )
        db.session.add(record)
        db.session.commit()
        flash(f"Contract added for {driver.name}. Print it, collect signatures, then upload signed copies in Step 2.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Manual contract add failed for driver_id=%s", driver.id)
        flash(f"Error adding contract: {exc}", "danger")

    return redirect(url_for("hr.dashboard_hr"))


# -------------------------
# Retroactive Contract Pack (drivers who already finished onboarding, e.g.
# pre-dating the system, without ever going through the HR-stage flow above)
# -------------------------
@hr_bp.route("/completed/<int:driver_id>/generate_contract_pack", methods=["POST"])
@login_required
@_require_completed_contract_access
def generate_contract_pack_completed(driver_id):
    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "Completed":
        flash(f"Driver is not fully onboarded yet (current: {driver.onboarding_stage}).", "warning")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    try:
        from services import contracts
        contracts.generate_driver_contracts(driver, uploaded_by_id=current_user.id)
        contracts.generate_promissory_note(driver, uploaded_by_id=current_user.id)
        db.session.commit()
        flash(f"Contract pack generated for {driver.name}. Print it, collect signatures, then upload the signed copies here.", "success")
    except contracts.ContractTemplateMissingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Retroactive contract pack generation failed for driver_id=%s", driver.id)
        flash(f"Error generating contract pack: {exc}", "danger")

    return redirect(request.referrer or url_for("hr.dashboard_hr"))


@hr_bp.route("/completed/<int:driver_id>/upload_signed_contract/<int:contract_id>", methods=["POST"])
@login_required
@_require_completed_contract_access
def upload_signed_contract(driver_id, contract_id):
    driver = Driver.query.get_or_404(driver_id)
    record = SharedGeneratedDriverContract.query.filter_by(id=contract_id, driver_id=driver.id).first_or_404()

    uploaded_file = request.files.get("signed_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose the signed copy to upload.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    if not _allowed_filename(uploaded_file.filename):
        flash("Invalid file type. Only JPG, PNG, or PDF are allowed.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    if size > max_bytes:
        flash(f"File too large. Maximum {max_bytes // (1024 * 1024)} MB.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    try:
        shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH") or current_app.config.get("UPLOAD_FOLDER")
        filename = secure_filename(uploaded_file.filename)
        relative_path = save_to_shared_storage(uploaded_file, driver.id, "contract", shared_root, filename=filename)

        record.signed_status = "signed"
        record.signed_at = datetime.utcnow()
        record.signed_copy_path = relative_path
        db.session.commit()
        flash(f"Signed contract copy saved for {driver.name}.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Signed contract upload failed for driver_id=%s contract_id=%s", driver.id, contract_id)
        flash(f"Error uploading signed contract: {exc}", "danger")

    return redirect(request.referrer or url_for("hr.dashboard_hr"))


@hr_bp.route("/completed/<int:driver_id>/upload_signed_promissory_note", methods=["POST"])
@login_required
@_require_completed_contract_access
def upload_signed_promissory_note(driver_id):
    driver = Driver.query.get_or_404(driver_id)

    if not _generated_promissory_link(driver):
        flash("Generate the contract pack first before uploading a signed promissory note.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    uploaded_file = request.files.get("signed_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose the signed copy to upload.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    if not _allowed_filename(uploaded_file.filename):
        flash("Invalid file type. Only JPG, PNG, or PDF are allowed.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    if size > max_bytes:
        flash(f"File too large. Maximum {max_bytes // (1024 * 1024)} MB.", "danger")
        return redirect(request.referrer or url_for("hr.dashboard_hr"))

    try:
        shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH") or current_app.config.get("UPLOAD_FOLDER")
        filename = secure_filename(uploaded_file.filename)
        relative_path = save_to_shared_storage(uploaded_file, driver.id, "contract", shared_root, filename=filename)

        doc = DriverDocument(
            driver_id=driver.id,
            document_type="other",
            file_path=relative_path,
            original_name=filename,
            file_size=size,
            uploaded_from="dobs",
            uploaded_by=current_user.id,
            notes="PromissoryNote-Signed",
        )
        db.session.add(doc)
        db.session.commit()
        flash(f"Signed promissory note saved for {driver.name}.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Signed promissory note upload failed for driver_id=%s", driver.id)
        flash(f"Error uploading signed promissory note: {exc}", "danger")

    return redirect(request.referrer or url_for("hr.dashboard_hr"))


@hr_bp.route("/approve_driver/<int:driver_id>", methods=["POST"])
@login_required
@require_permission("onboarding.hr.approve")
def approve_driver(driver_id):
    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "HR":
        flash(f"Driver is not in HR stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    files = request.files
    max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    qiwa_required = _qiwa_required(driver)

    if not _generated_contract_links(driver) or not _generated_promissory_link(driver):
        flash("Generate the contract pack first, print it, collect signatures, then upload signed copies in Step 2.", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    required_files = {
        "company_contract_file": "Company contract signed copy",
        "promissory_note_file": "Promissory note signed copy",
    }
    if qiwa_required:
        required_files["qiwa_contract_file"] = "Qiwa contract signed copy"

    for field_name, label in required_files.items():
        uploaded_file = files.get(field_name)
        if not uploaded_file or not uploaded_file.filename:
            flash(f"{label} is required before HR can approve this driver.", "danger")
            return redirect(url_for("hr.dashboard_hr"))

    # Bank details are captured (and mandatory) at this first HR pass -- one
    # bank account + one STC Pay number -- and written to the shared
    # driver_bank_accounts table that DMS reads.
    required_bank = {
        "bank_name": "Bank name",
        "account_number": "IBAN / account number",
        "stc_pay_number": "STC Pay number",
    }
    for field_name, label in required_bank.items():
        if not (request.form.get(field_name) or "").strip():
            flash(f"{label} is required before HR can approve this driver.", "danger")
            return redirect(url_for("hr.dashboard_hr"))

    try:
        process_hr_approval(driver, files, request.form, _upload_root(), max_bytes, db=db, uploaded_by_id=current_user.id)
        _ensure_driver_password(driver)
        db.session.commit()
        current_app.logger.info(
            "hr_approve_driver",
            extra={"user": current_user.username, "driver_id": driver.id, "iqama": driver.iqaama_number},
        )
    except ValueError as ve:
        db.session.rollback()
        flash(str(ve), "danger")
        return redirect(url_for("hr.dashboard_hr"))
    except Exception as e:
        db.session.rollback()
        flash(f"Error approving driver: {e}", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    # Notify Ops Supervisors
    try:
        ops_supervisors = User.query.filter_by(role="OpsSupervisor").all()
        recipients = [op.email for op in ops_supervisors if op.email]
        if recipients:
            msg = Message(
                subject=f"Driver Ready for Ops Supervisor Stage | السائق جاهز لمرحلة مشرف العمليات: {driver.name}",
                recipients=recipients
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                    <!-- English LTR -->
                    <div style="text-align: left;">
                        <h2 style="color: #713183;">Driver Processing Notification</h2>
                        <p>Hello Ops Supervisor Team,</p>
                        <p>Driver <strong>{driver.name}</strong> (iqaama: <strong>{driver.iqaama_number or "N/A"}</strong>) has been moved to your stage for processing.</p>
                        <p>Please review and complete the necessary actions.</p>
                        <p>Login to your dashboard: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <!-- Arabic RTL -->
                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #713183;">إشعار متابعة السائق</h2>
                        <p>السادة فريق مشرف العمليات،</p>
                        <p>تم نقل السائق <strong>{driver.name}</strong> (رقم الإقامة: <strong>{driver.iqaama_number or "N/A"}</strong>) إلى مرحلتكم لمتابعة الإجراءات.</p>
                        <p>يرجى مراجعة البيانات واستكمال الإجراءات المطلوبة.</p>
                        <p>تسجيل الدخول إلى لوحة القيادة: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <p style="text-align:center;">Regards / مع التحية,<br>Operations Team / فريق العمليات</p>

                </div>
            </body>
            </html>
            """

            mail.send(msg)

    except Exception as e:
        print("[EMAIL ERROR]", e)

    flash(f"Driver {driver.name} approved and sent to Ops Supervisor stage.", "success")
    return redirect(url_for("hr.dashboard_hr"))



@hr_bp.route("/reject_driver/<int:driver_id>", methods=["POST"])
@login_required
@require_permission("onboarding.hr.approve")
def reject_driver(driver_id):

    driver = Driver.query.get_or_404(driver_id)

    # Rejection is only available on HR's first pass at a driver - once
    # they've moved past HR, platform IDs/contracts are already in motion
    # and unwinding that goes through offboarding, not a simple reject.
    if driver.onboarding_stage != "HR":
        flash(f"Driver is not in HR stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    reason = request.form.get("rejection_reason", "").strip()

    if not reason:
        flash("Please provide a rejection reason.", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    driver_name = driver.name
    iqaama_number = driver.iqaama_number
    nationality = driver.nationality

    try:
        # Soft-reject: preserve the record (audit trail, consistent with
        # Ops Manager's reject) instead of deleting it outright.
        driver.onboarding_stage = "Rejected"
        driver.rejected_by_stage = "HR"
        driver.rejected_at = datetime.utcnow()
        driver.reject_reason = reason
        db.session.commit()
        current_app.logger.info(
            "hr_reject_driver",
            extra={"user": current_user.username, "driver_id": driver.id, "iqama": driver.iqaama_number, "reason": reason},
        )

        # Collect recipients (Ops, HR, Admin)
        recipients = []
        ops_managers = User.query.filter_by(role="OpsManager").all()
        hr_users = User.query.filter_by(role="HR").all()
        admins = User.query.filter_by(role="SuperAdmin").all()

        for group in (ops_managers, hr_users, admins):
            recipients.extend([u.email for u in group if u.email])

        send_rejection_email(
            mail,
            recipients,
            driver_name,
            iqaama_number,
            nationality,
            reason,
            current_user.name,
        )

        flash(f"Driver {driver_name} rejected. Notifications sent.", "success")

    except Exception as e:
        db.session.rollback()
        print("[HR REJECT DRIVER ERROR]", e)
        flash("❌ Error rejecting driver. Please try again.", "danger")

    return redirect(url_for("hr.dashboard_hr"))


# -------------------------
# HR Final Stage: Complete Sponsorship Transfer (handles Qiwa / non-Qiwa cases)
# -------------------------
@hr_bp.route("/complete_transfer/<int:driver_id>", methods=["POST"])
@login_required
@require_permission("onboarding.hr_final.complete")
def complete_sponsorship_transfer(driver_id):

    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "HR Final":
        flash(f"Driver is not in HR Final stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    kit_error = _apply_hr_final_kit(driver)
    if kit_error:
        flash(kit_error, "danger")
        return redirect(url_for("hr.dashboard_hr"))

    try:
        completed_at = datetime.utcnow()

        # Determine current case
        if not driver.qiwa_contract_created:
            # 🔹 Case 1: Driver has NO Qiwa contract
            driver.mark_completed_onboarding(completed_at)
            _ensure_driver_code(driver)
            _set_default_driver_password(driver)
            driver.sponsorship_transfer_status = "Not Required"
            driver.sponsorship_transfer_proof = None
            driver.sponsorship_transfer_completed_at = completed_at
            _generate_driver_contracts_safe(driver, uploaded_by_id=current_user.id)
            db.session.commit()

            flash(f"✅ Driver {driver.name} marked as completed (no Qiwa contract).", "success")

        else:
            transfer_status = request.form.get("sponsorship_transfer_status")
            file = request.files.get("sponsorship_transfer_proof")
            try:
                max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
                save_transfer_proof(driver, file, _upload_root(), transfer_status, max_bytes, db=db, uploaded_by_id=current_user.id)
                driver.sponsorship_transfer_completed_at = completed_at
                driver.mark_completed_onboarding(completed_at)
                _ensure_driver_code(driver)
                _set_default_driver_password(driver)
                _generate_driver_contracts_safe(driver, uploaded_by_id=current_user.id)
                db.session.commit()
                current_app.logger.info(
                    "hr_complete_transfer",
                    extra={"user": current_user.username, "driver_id": driver.id, "iqama": driver.iqaama_number},
                )
                flash(f"✅ Sponsorship transfer completed for {driver.name}.", "success")
            except ValueError as ve:
                db.session.rollback()
                flash(str(ve), "warning")
                return redirect(url_for("hr.dashboard_hr"))
            except Exception as e:
                db.session.rollback()
                flash(f"Error completing transfer: {e}", "danger")
                return redirect(url_for("hr.dashboard_hr"))

        # 📧 Notify SuperAdmins after completion
        superadmins = User.query.filter_by(role="SuperAdmin").all()
        recipients = [sa.email for sa in superadmins if sa.email]
        if recipients:
            msg = Message(
                subject=f"Driver Onboarding Completed | اكتمال انضمام السائق: {driver.name}",
                recipients=recipients
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">
                    
                    <!-- English -->
                    <div style="text-align: left;">
                        <h2 style="color: #713183;">Driver Onboarding Completed</h2>
                        <p>Dear Team,</p>
                        <p>Driver <strong>{driver.name}</strong> has successfully completed the onboarding process.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>iqaama</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.iqaama_number or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Qiwa Contract Created</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{'✅ Yes' if driver.qiwa_contract_created else '❌ No'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Sponsorship Transfer Completed At</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.sponsorship_transfer_completed_at.strftime('%Y-%m-%d %H:%M') if driver.sponsorship_transfer_completed_at else 'N/A'}</td>
                            </tr>
                        </table>
                        <p>Please update your records accordingly.</p>
                        <p>Login to your dashboard: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <!-- Arabic -->
                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #713183;">اكتمل انضمام السائق</h2>
                        <p>السادة الفريق،</p>
                        <p>لقد أكمل السائق <strong>{driver.name}</strong> عملية الانضمام بنجاح.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">رقم الإقامة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.iqaama_number or "N/A"}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">إنشاء عقد قوى</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{'✅ نعم' if driver.qiwa_contract_created else '❌ لا'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تاريخ إكمال نقل الكفالة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{driver.sponsorship_transfer_completed_at.strftime('%Y-%m-%d %H:%M') if driver.sponsorship_transfer_completed_at else 'N/A'}</td>
                            </tr>
                        </table>
                        <p>يرجى تحديث سجلاتكم وفقاً لذلك.</p>
                        <p>تسجيل الدخول إلى لوحة القيادة: <a href="https://dobs.dobs.cloud/login" target="_blank">https://dobs.dobs.cloud/login</a></p>
                    </div>

                </div>
            </body>
            </html>
            """

            mail.send(msg)




    except Exception as e:
        db.session.rollback()
        print("[HR COMPLETE TRANSFER ERROR]", e)
        flash(f"❌ Error completing sponsorship transfer: {str(e)}", "danger")

    return redirect(url_for("hr.dashboard_hr"))

@hr_bp.route('/complete_driver/<int:driver_id>', methods=['POST'])
@login_required
@require_permission("onboarding.hr_final.complete")
def complete_driver(driver_id):
    driver = Driver.query.get_or_404(driver_id)

    if driver.onboarding_stage != "HR Final":
        flash(f"Driver is not in HR Final stage (current: {driver.onboarding_stage}).", "warning")
        return redirect(url_for("hr.dashboard_hr"))

    # Only for drivers without a Qiwa contract
    if not driver.qiwa_contract_created:
        # Kit issuance runs only on the path that actually completes the
        # driver and commits -- the else branch below is a no-op, so calling
        # _apply_hr_final_kit there would leave orphaned pending session rows.
        kit_error = _apply_hr_final_kit(driver)
        if kit_error:
            flash(kit_error, "danger")
            return redirect(url_for("hr.dashboard_hr"))

        completed_at = datetime.utcnow()
        driver.mark_completed_onboarding(completed_at)
        _ensure_driver_code(driver)
        _set_default_driver_password(driver)
        driver.sponsorship_transfer_status = "Not Required"
        driver.sponsorship_transfer_proof = None
        driver.sponsorship_transfer_completed_at = completed_at
        _generate_driver_contracts_safe(driver, uploaded_by_id=current_user.id)
        db.session.commit()
        flash(f"✅ Driver {driver.name} marked as completed (no Qiwa contract).", "success")
    else:
        flash(f"⚠️ Driver {driver.name} has a Qiwa contract. Use normal transfer workflow.", "warning")

    return redirect(request.referrer or url_for("hr.dashboard_hr"))

# -------------------------
# Change HR Password
# -------------------------
@hr_bp.route("/change_password", methods=["POST"])
@login_required
def change_password():
    if current_user.role != "HR":
        flash("Access denied. HR role required.", "danger")
        return redirect(url_for("auth.login"))

    current_password = request.form["current_password"]
    new_password = request.form["new_password"]
    confirm_password = request.form["confirm_password"]

    if not verify_password_hash(current_user.password, current_password):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    if new_password != confirm_password:
        flash("New passwords do not match.", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    current_user.password = generate_password_hash(new_password)
    db.session.commit()

    flash("Password updated successfully.", "success")
    return redirect(url_for("hr.dashboard_hr"))


# -------------------------
# Start Offboarding (contract cancellation)
# -------------------------
@hr_bp.route("/start_offboarding/<int:driver_id>", methods=["POST"])
@login_required
def start_offboarding(driver_id):
    if current_user.role != "HR":
        flash("Access denied", "danger")
        return redirect(url_for("auth.login"))

    driver = Driver.query.get_or_404(driver_id)
    driver.offboarding_stage = "HR"
    driver.offboarding_reason = request.form.get("offboarding_reason", "Not specified")
    driver.offboarding_requested_at = datetime.utcnow()

    db.session.commit()
    flash(f"Offboarding process started for {driver.name}.", "info")
    return redirect(url_for("hr.offboarding_hr"))


# -------------------------
# Complete Offboarding
#
# The old /complete_offboarding/<id> route called a mark_hr_cleared() method
# that never existed on the Offboarding model (a live AttributeError bug)
# and wasn't referenced by any template. finalize_offboarding below is the
# real, working HR-clear endpoint - it's the only one that's ever been used.
# -------------------------

@hr_bp.route("/offboarding/finalize", methods=["POST"])
@login_required
@require_permission("offboarding.hr.finalize")
def finalize_offboarding():
    # Determine if request is JSON or form
    if request.is_json:
        data = request.get_json()
        offboarding_id = data.get("offboarding_id")
        company_cancelled = data.get("company_contract_cancelled") == "yes"
        qiwa_cancelled = data.get("qiwa_contract_cancelled") == "yes"
        salary_paid = data.get("salary_paid") == "yes"
        promissory_note_issued = data.get("promissory_note_issued") == "yes"
        payment_letter_issued = data.get("payment_letter_issued") == "yes"
        payment_letter_due_days = data.get("payment_letter_due_days")
    else:
        offboarding_id = request.form.get("offboarding_id")
        company_cancelled = request.form.get("company_contract_cancelled") == "yes"
        qiwa_cancelled = request.form.get("qiwa_contract_cancelled") == "yes"
        salary_paid = request.form.get("salary_paid") == "yes"
        promissory_note_issued = request.form.get("promissory_note_issued") == "yes"
        payment_letter_issued = request.form.get("payment_letter_issued") == "yes"
        payment_letter_due_days = request.form.get("payment_letter_due_days")

    # Fetch Offboarding record
    offboarding = Offboarding.query.get_or_404(offboarding_id)

    if offboarding.status != "HR":
        message = f"Offboarding is not in HR stage (current: {offboarding.status})."
        if request.is_json:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "warning")
        return redirect(url_for("hr.dashboard_hr"))

    # Update fields
    offboarding.company_contract_cancelled = company_cancelled
    offboarding.qiwa_contract_cancelled = qiwa_cancelled
    offboarding.salary_paid = salary_paid

    # The settlement direction was computed by Finance (dms payroll balance
    # combined with Ops Supervisor's penalty and Fleet's damage cost) - HR's
    # resolution requirement depends on which way the money flows.
    direction = offboarding.settlement_direction or "even"
    settlement_resolved = True
    due_days_int = None

    if direction == "company_owes_driver":
        if payment_letter_issued:
            try:
                due_days_int = int(payment_letter_due_days)
                settlement_resolved = due_days_int > 0
            except (TypeError, ValueError):
                settlement_resolved = False
        else:
            settlement_resolved = salary_paid
    elif direction == "driver_owes_company":
        settlement_resolved = salary_paid or promissory_note_issued

    # Constraint check
    if not (company_cancelled and qiwa_cancelled and settlement_resolved):
        if direction == "company_owes_driver" and not settlement_resolved:
            message = "Cannot clear: confirm the driver has been paid, or issue a payment letter with a valid number of days."
        elif direction == "driver_owes_company" and not settlement_resolved:
            message = "Cannot clear: confirm the driver has paid, or issue a promissory note."
        else:
            message = "Cannot clear: Company & Qiwa contracts must be cancelled."
        if request.is_json:
            return jsonify({"success": False, "message": message}), 400
        flash(message, "danger")
        return redirect(url_for("hr.dashboard_hr"))

    # Mark HR cleared
    offboarding.hr_cleared = True
    offboarding.hr_cleared_at = datetime.utcnow()
    offboarding.status = "Completed"

    try:
        from services import contracts, dms_payroll

        if direction == "driver_owes_company" and promissory_note_issued and not salary_paid:
            offboarding.promissory_note_issued = True
            offboarding.promissory_note_at = datetime.utcnow()
            contracts.generate_offboarding_promissory_note(offboarding, uploaded_by_id=current_user.id)

        if direction == "company_owes_driver" and payment_letter_issued and not salary_paid:
            offboarding.payment_letter_issued = True
            offboarding.payment_letter_due_days = due_days_int
            offboarding.payment_letter_at = datetime.utcnow()
            contracts.generate_payment_letter(offboarding, due_days_int, uploaded_by_id=current_user.id)

        contracts.generate_final_settlement(offboarding, uploaded_by_id=current_user.id)

        # Settlement is confirmed complete (paid, either direction) once
        # HR finalizes with an actual payment (not a letter/note promising
        # future payment) - only then close the DMS slip out to match.
        if direction != "even" and salary_paid:
            dms_payroll.mark_salary_slip_settled(offboarding.dms_salary_slip_id)
    except Exception:
        current_app.logger.exception("Settlement document generation failed for offboarding_id=%s", offboarding.id)

    db.session.commit()

    # Send email to Fleet Manager(s)
    try:
        all_users = User.query.all()
        recipients = [u.email for u in all_users if u.email]

        if recipients:
            msg = Message(
                subject=f"Driver Fully Offboarded | اكتمال خروج السائق: {offboarding.driver.name}",
                recipients=recipients
            )

            msg.html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                    <!-- English -->
                    <div style="text-align: left;">
                        <h2 style="color: #713183;">Driver Fully Offboarded</h2>
                        <p>Hello Team,</p>
                        <p>HR has completed the offboarding process for driver <strong>{offboarding.driver.name}</strong>.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Driver Name</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{offboarding.driver.name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Iqama Number</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{offboarding.driver.iqaama_number or 'N/A'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;"><strong>Offboarding Completed At</strong></td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}</td>
                            </tr>
                        </table>
                        <p>The record has been updated and the driver is fully offboarded.</p>
                    </div>

                    <hr style="margin: 30px 0;">

                    <!-- Arabic -->
                    <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                        <h2 style="color: #713183;">اكتمال خروج السائق</h2>
                        <p>مرحبًا بالفريق،</p>
                        <p>لقد قامت إدارة الموارد البشرية بإنهاء عملية مغادرة السائق <strong>{offboarding.driver.name}</strong>.</p>
                        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">اسم السائق</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{offboarding.driver.name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">رقم الإقامة</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{offboarding.driver.iqaama_number or 'N/A'}</td>
                            </tr>
                            <tr>
                                <td style="padding: 8px; border: 1px solid #ddd;">تاريخ اكتمال الخروج</td>
                                <td style="padding: 8px; border: 1px solid #ddd;">{datetime.utcnow().strftime('%Y-%m-%d %H:%M')}</td>
                            </tr>
                        </table>
                        <p>تم تحديث السجل وتم إلغاء تثبيت برنامج التشغيل بالكامل.</p>
                    </div>

                    <hr style="margin: 30px 0;">
                    <p style="text-align:center;">Regards / مع التحية,<br>HR Team / فريق الموارد البشرية</p>

                </div>
            </body>
            </html>
            """

            mail.send(msg)


    except Exception as e:
        print("[EMAIL ERROR]", e)
        if request.is_json:
            return jsonify({"success": False, "message": f"Email send failed: {str(e)}"}), 500
        flash(f"Email send failed: {str(e)}", "danger")
        return redirect(url_for("hr.dashboard_hr"))

    success_message = f"HR clearance completed for {offboarding.driver.name} and notified."
    if request.is_json:
        return jsonify({"success": True, "message": success_message})
    flash(success_message, "success")
    return redirect(url_for("hr.dashboard_hr"))
