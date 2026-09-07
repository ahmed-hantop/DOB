from flask import Flask, render_template, request, redirect, url_for, flash, session , current_app, jsonify, send_file
from flask_login import login_required, current_user, login_user
from models import (
    Business, DriverBusinessIDS, Offboarding, db, Driver, User, BusinessID, BusinessDriver,
    DriverType, OnboardingStageTemplate, DriverTypeSettings, ContractTemplate, CompanyPreset, Branch, DriverDocument, ONBOARDING_STAGES,
)
from extensions import db, mail, limiter
from flask_mail import Message
from datetime import datetime
from io import BytesIO
import os
from utils.email_utils import send_password_change_email
from utils.passwords import verify_password_hash
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename
from services.file_storage import save_upload, validate_upload
from services.admin_service import (
    update_driver_from_form,
    delete_driver_and_offboarding,
    create_user_from_form,
    update_user_from_form,
    delete_user as delete_user_service,
    change_user_password,
)
from forms.common import (
    CSRFOnlyForm, AddUserForm, AddDriverForm, ChangePasswordForm, EditUserForm,
    OnboardingStageTemplateForm, DriverTypeSettingsForm, ContractTemplateForm, PromissoryNoteTemplateForm,
    CompanyPresetForm, RoleForm,
    DASHBOARD_ENDPOINT_CHOICES,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_
from utils.auth import require_roles_or_owner
from utils.cache import ttl_cache
from models import Role, Permission, RolePermission, UserRole, UserPermission
from services.rbac import (
    grant_role, revoke_role, set_permission_override, clear_permission_override,
    role_permission_codes, set_role_permissions, user_can_access_dashboard,
    require_permission, user_has_permission, user_has_role,
)
from services import impersonation
from services.impersonation import ImpersonationError
from services.contracts import (
    render_contract_template_preview,
    generated_contract_links,
    generated_promissory_link,
    signed_promissory_link,
)
from blueprints.auth.routes import _post_login_redirect
from . import admin_bp

UPLOAD_FOLDER = "static/uploads"

DRIVER_DOCUMENT_UPLOADS = {
    "iqama_card_upload": ("Iqama Card", "iqama"),
    "driving_license_upload": ("Driving License", "license"),
    "passport_upload": ("Passport", "passport"),
    "company_contract_file": ("Company Contract", "contract"),
    "promissory_note_file": ("Promissory Note", "contract"),
    "qiwa_contract_file": ("Qiwa Contract", "contract"),
    "transfer_fee_receipt": ("Transfer Fee Receipt", "other"),
    "sponsorship_transfer_proof": ("Sponsorship Transfer Proof", "other"),
    "tamm_authorization_ss": ("TAMM Authorization", "other"),
}


def _store_dashboard_document_uploads(driver):
    """Persist dashboard uploads without clearing existing documents on edit."""
    max_bytes = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    upload_dir = current_app.config.get("UPLOAD_FOLDER", UPLOAD_FOLDER)

    for field, (label, document_type) in DRIVER_DOCUMENT_UPLOADS.items():
        upload = request.files.get(field)
        if not upload or not upload.filename:
            continue

        validate_upload(upload, max_bytes)
        filename = f"{driver.id}_{field}_{datetime.utcnow():%Y%m%d%H%M%S}_{secure_filename(upload.filename)}"
        setattr(driver, field, save_upload(upload, upload_dir, filename))



@ttl_cache(ttl_seconds=120, maxsize=1)
def _cached_businesses():
    return Business.query.order_by(Business.name).all()

# ------------------------- 
# Language Helper
# -------------------------
def set_lang():
    """
    Determine current language:
    - URL parameter 'lang' (priority)
    - session (fallback)
    Defaults to 'en'
    Returns: lang_code, is_rtl
    """
    lang = request.args.get("lang") or session.get("lang") or "en"
    session["lang"] = lang
    rtl = True if lang == "ar" else False
    return lang, rtl


def _pagination_params():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 50))
    except ValueError:
        per_page = 50
    per_page = max(1, min(per_page, 200))
    return page, per_page

# -------------------------
# Date helpers
# -------------------------
def safe_date(value):
    if not value:
        return None
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return value

def safe_datetime(value):
    if not value:
        return None
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def normalize_onboarding_stage(value):
    if value in (None, ""):
        return value
    text = str(value).strip()
    key = text.lower().replace(" ", "")
    if key in {"fleet", "fleetmanager"}:
        return "Fleet Manager"
    return text


def _fill_contract_template_from_form(template, form):
    template.name = form.name.data
    template.business_id = form.business_id.data or None
    template.driver_type_id = form.driver_type_id.data or None
    template.document_kind = "driver_contract"
    template.first_party_name = form.first_party_name.data or None
    template.first_party_name_ar = form.first_party_name_ar.data or None
    template.first_party_label = form.first_party_label.data or None
    template.first_party_label_ar = form.first_party_label_ar.data or None
    template.second_party_label = form.second_party_label.data or None
    template.second_party_label_ar = form.second_party_label_ar.data or None
    template.intro_content = form.intro_content.data or None
    template.intro_content_ar = form.intro_content_ar.data or None
    template.body_content = form.body_content.data
    template.body_content_ar = form.body_content_ar.data or None
    template.eligibility_content = form.eligibility_content.data or None
    template.eligibility_content_ar = form.eligibility_content_ar.data or None
    template.general_terms_content = form.general_terms_content.data or None
    template.general_terms_content_ar = form.general_terms_content_ar.data or None
    template.target_orders = form.target_orders.data or None
    template.target_salary = form.target_salary.data or None
    template.bonus_per_extra_order = form.bonus_per_extra_order.data or None
    template.deduction_per_missing_order = form.deduction_per_missing_order.data or None
    template.calculation_tiers_json = form.calculation_tiers_json.data or None
    template.company_signatory_name = form.company_signatory_name.data or None
    template.company_signatory_title = form.company_signatory_title.data or None
    template.signature_notes = form.signature_notes.data or None
    template.signature_notes_ar = form.signature_notes_ar.data or None
    template.is_active = form.is_active.data
    return template


def _get_company_preset() -> CompanyPreset:
    """Singleton row (id=1), created on first access. Superseded the fixed
    CONTRACT_* env vars as the source for template defaults - a blank field
    here still falls back to that env-configured value (see the two
    _*_template_defaults() functions below), so upgrading doesn't lose
    whatever was already configured via environment."""
    preset = CompanyPreset.query.get(1)
    if not preset:
        preset = CompanyPreset(id=1)
        db.session.add(preset)
        db.session.commit()
    return preset


def _company_preset_field(preset: CompanyPreset, field: str, config_key: str, fallback: str) -> str:
    return getattr(preset, field, None) or current_app.config.get(config_key) or fallback


def _contract_template_defaults():
    preset = _get_company_preset()
    return {
        "name": "Freelancer Driver Contract - Ninja",
        "first_party_name": _company_preset_field(preset, "first_party_name", "CONTRACT_FIRST_PARTY_NAME", "Speed Logi Company"),
        "first_party_name_ar": _company_preset_field(preset, "first_party_name_ar", "CONTRACT_FIRST_PARTY_NAME_AR", "شركة سبيد لوجي"),
        "first_party_label": _company_preset_field(preset, "first_party_label", "CONTRACT_FIRST_PARTY_LABEL", "First Party"),
        "first_party_label_ar": _company_preset_field(preset, "first_party_label_ar", "CONTRACT_FIRST_PARTY_LABEL_AR", "الطرف الأول"),
        "second_party_label": _company_preset_field(preset, "second_party_label", "CONTRACT_SECOND_PARTY_LABEL", "Second Party / The Courier"),
        "second_party_label_ar": _company_preset_field(preset, "second_party_label_ar", "CONTRACT_SECOND_PARTY_LABEL_AR", "الطرف الثاني / السائق"),
        "intro_content": (
            "Whereas the Parties have entered an Employment Contract, they hereby agree that this "
            "Addendum shall form an integral part of the Employment Contract and shall be governed by "
            "the following terms and conditions."
        ),
        "intro_content_ar": (
            "حيث إن الطرفين قد أبرما عقد عمل، فقد اتفقا بموجب هذا الملحق على أن يكون جزءًا لا يتجزأ من عقد "
            "العمل، وأن تسري عليه الشروط والأحكام التالية."
        ),
        "body_content": (
            "This Addendum operates on the monthly package system. The Courier's monthly target shall be "
            "{target_orders} completed delivery orders during the Company's approved evaluation period. "
            "The Courier shall be entitled to a gross monthly salary of SAR {target_salary}, provided "
            "that the monthly target and all performance indicators set forth in this Addendum are achieved."
        ),
        "body_content_ar": (
            "يعمل هذا الملحق وفق نظام الباقة الشهرية. ويكون المستهدف الشهري للمندوب هو {target_orders} طلبًا "
            "مكتملًا خلال فترة التقييم المعتمدة من الشركة. ويستحق المندوب راتبًا شهريًا إجماليًا قدره "
            "{target_salary} ريال سعودي، شريطة تحقيق المستهدف الشهري وجميع مؤشرات الأداء المنصوص عليها في هذا "
            "الملحق."
        ),
        "eligibility_content": (
            "To be eligible for the monthly salary and any additional incentives, the Courier must achieve "
            "the required completed orders, maintain a delivery speed rate of no less than 85%, maintain "
            "an order acceptance rate of no less than 95%, commit to the approved work schedule with a "
            "minimum shift duration of 11 hours per shift, comply with all Company policies and operational "
            "procedures, maintain a professional appearance, provide excellent customer service, and protect "
            "the Company's reputation. If any performance indicator is not achieved, the Company reserves "
            "the right to recalculate the Courier's compensation in accordance with this Addendum."
        ),
        "eligibility_content_ar": (
            "لاستحقاق الراتب الشهري وأي حوافز إضافية، يجب على المندوب تحقيق العدد المطلوب من الطلبات المكتملة، "
            "والمحافظة على سرعة توصيل لا تقل عن 85%، ونسبة قبول طلبات لا تقل عن 95%، والالتزام بجدول العمل "
            "المعتمد بحد أدنى 11 ساعة لكل وردية، والالتزام بسياسات الشركة وإجراءاتها التشغيلية، والمحافظة على "
            "المظهر المهني، وتقديم خدمة عملاء ممتازة، وحماية سمعة الشركة. وفي حال عدم تحقيق أي مؤشر من مؤشرات "
            "الأداء، يحق للشركة إعادة احتساب مستحقات المندوب وفقًا لهذا الملحق."
        ),
        "general_terms_content": (
            "This Addendum forms an integral part of the Employment Contract executed between the Parties "
            "and shall be read together with the Employment Contract unless otherwise stated herein. The "
            "Courier acknowledges that he has carefully read, understood, and accepted all the terms and "
            "conditions of this Addendum, including the salary calculation method, incentives, and deductions, "
            "without any reservation. The Courier shall comply with all operational instructions issued by "
            "the Company, its clients, or the approved operating platform, provided that such instructions "
            "do not conflict with the applicable laws and regulations. The Company reserves the right to amend "
            "the package target, incentive scheme, deduction mechanism, or performance indicators whenever "
            "business requirements so require, provided that the Courier is notified before such amendments "
            "become effective. In the event of any conflict between this Addendum and the Employment Contract, "
            "the provisions of this Addendum shall prevail with respect to the package system, performance "
            "indicators, incentives, and deductions. This Addendum shall be governed by the applicable laws "
            "and regulations of the Kingdom of Saudi Arabia."
        ),
        "general_terms_content_ar": (
            "يُعد هذا الملحق جزءًا لا يتجزأ من عقد العمل المبرم بين الطرفين، ويُقرأ مع عقد العمل ما لم يُنص "
            "على خلاف ذلك في هذا الملحق. ويقر المندوب بأنه قرأ وفهم ووافق على جميع شروط وأحكام هذا الملحق، بما "
            "في ذلك آلية احتساب الراتب والحوافز والخصومات، دون أي تحفظ. ويلتزم المندوب بجميع التعليمات "
            "التشغيلية الصادرة من الشركة أو عملائها أو المنصة التشغيلية المعتمدة، ما دامت لا تتعارض مع الأنظمة "
            "واللوائح المعمول بها. ويحق للشركة تعديل المستهدف أو آلية الحوافز أو آلية الخصومات أو مؤشرات "
            "الأداء متى اقتضت متطلبات العمل ذلك، على أن يتم إشعار المندوب قبل سريان تلك التعديلات. وفي حال "
            "تعارض هذا الملحق مع عقد العمل، تكون أحكام هذا الملحق هي السارية فيما يتعلق بنظام الباقة ومؤشرات "
            "الأداء والحوافز والخصومات. ويخضع هذا الملحق لأنظمة ولوائح المملكة العربية السعودية."
        ),
        "target_orders": 460,
        "target_salary": 5500,
        "bonus_per_extra_order": 10,
        "deduction_per_missing_order": 22,
        "calculation_tiers_json": (
            '[{"label":"Level One","min_orders":460,"max_orders":null,"calculation_mode":"bonus_per_extra_order","per_order_rate":10},'
            '{"label":"Level Two","min_orders":414,"max_orders":459,"calculation_mode":"deduction_per_missing_order","per_order_rate":22},'
            '{"label":"Level Three","min_orders":368,"max_orders":413,"calculation_mode":"deduction_per_missing_order","per_order_rate":22.5},'
            '{"label":"Level Four","min_orders":322,"max_orders":367,"calculation_mode":"deduction_per_missing_order","per_order_rate":23},'
            '{"label":"Level Five","min_orders":0,"max_orders":321,"calculation_mode":"performance_review","per_order_rate":0}]'
        ),
        "company_signatory_name": _company_preset_field(preset, "company_signatory_name", "CONTRACT_COMPANY_SIGNATORY_NAME", "Authorized Signatory"),
        "company_signatory_title": _company_preset_field(preset, "company_signatory_title", "CONTRACT_COMPANY_SIGNATORY_TITLE", "Operations Director"),
        "signature_notes": (
            "This Addendum has been executed in two original copies, with each Party retaining one copy "
            "for implementation."
        ),
        "signature_notes_ar": "حرر هذا الملحق من نسختين أصليتين، يحتفظ كل طرف بنسخة للعمل بموجبها.",
    }


def _fill_promissory_note_template_from_form(template, form):
    template.name = form.name.data
    template.business_id = None
    template.driver_type_id = None
    template.document_kind = "promissory_note"
    template.first_party_name = form.first_party_name.data or None
    template.first_party_name_ar = form.first_party_name_ar.data or None
    template.first_party_label = form.first_party_label.data or None
    template.first_party_label_ar = form.first_party_label_ar.data or None
    template.second_party_label = form.second_party_label.data or None
    template.second_party_label_ar = form.second_party_label_ar.data or None
    template.intro_content = form.intro_content.data or None
    template.intro_content_ar = form.intro_content_ar.data or None
    template.body_content = form.body_content.data
    template.body_content_ar = form.body_content_ar.data or None
    template.eligibility_content = form.eligibility_content.data or None
    template.eligibility_content_ar = form.eligibility_content_ar.data or None
    template.general_terms_content = form.general_terms_content.data or None
    template.general_terms_content_ar = form.general_terms_content_ar.data or None
    template.company_signatory_name = form.company_signatory_name.data or None
    template.company_signatory_title = form.company_signatory_title.data or None
    template.signature_notes = form.signature_notes.data or None
    template.signature_notes_ar = form.signature_notes_ar.data or None
    template.is_active = form.is_active.data
    return template


def _promissory_note_template_defaults():
    preset = _get_company_preset()
    return {
        "name": "Driver Promissory Note",
        "first_party_name": _company_preset_field(preset, "first_party_name", "CONTRACT_FIRST_PARTY_NAME", "Speed Logi Company"),
        "first_party_name_ar": _company_preset_field(preset, "first_party_name_ar", "CONTRACT_FIRST_PARTY_NAME_AR", "شركة سبيد لوجي"),
        "first_party_label": _company_preset_field(preset, "first_party_label", "CONTRACT_FIRST_PARTY_LABEL", "First Party"),
        "first_party_label_ar": _company_preset_field(preset, "first_party_label_ar", "CONTRACT_FIRST_PARTY_LABEL_AR", "الطرف الأول"),
        "second_party_label": _company_preset_field(preset, "second_party_label", "CONTRACT_SECOND_PARTY_LABEL", "Second Party / The Courier"),
        "second_party_label_ar": _company_preset_field(preset, "second_party_label_ar", "CONTRACT_SECOND_PARTY_LABEL_AR", "الطرف الثاني / السائق"),
        "intro_content": "",
        "intro_content_ar": "",
        "body_content": (
            "This is to confirm that {driver_name} (Driver ID: {driver_id}) has received and acknowledged "
            "the terms of this promissory note as part of driver onboarding."
        ),
        "body_content_ar": (
            "هذا إقرار بأن {driver_name} (رقم السائق: {driver_id}) قد استلم وأقر بشروط سند لأمر هذا "
            "كجزء من إجراءات انضمام السائق."
        ),
        "eligibility_content": "",
        "eligibility_content_ar": "",
        "general_terms_content": "",
        "general_terms_content_ar": "",
        "company_signatory_name": _company_preset_field(preset, "company_signatory_name", "CONTRACT_COMPANY_SIGNATORY_NAME", "Authorized Signatory"),
        "company_signatory_title": _company_preset_field(preset, "company_signatory_title", "CONTRACT_COMPANY_SIGNATORY_TITLE", "Operations Director"),
        "signature_notes": "",
        "signature_notes_ar": "",
    }

# -------------------------
# SuperAdmin Dashboard
# -------------------------
@admin_bp.route("/", methods=["GET"])
@login_required
def dashboard():
    if not user_can_access_dashboard(current_user, "admin.dashboard"):
        flash("Access denied. SuperAdmin role required.", "danger")
        return redirect(url_for("auth.login"))

    # -------------------------
    # Drivers and Offboarding
    # -------------------------
    total_users = User.query.filter(User.id != current_user.id).count()
    page, per_page = _pagination_params()
    search_q = (request.args.get("q") or "").strip()

    # Paginate pending onboarding drivers only, so pages refill correctly
    # after a driver moves to Completed and leaves the pending pool.
    # Rejected drivers get their own tab/query below rather than showing up
    # here mixed in with drivers still actively moving through the pipeline.
    filtered_drivers_query = Driver.query.filter(Driver.onboarding_stage.notin_(["Completed", "Rejected"]))
    if search_q:
        pattern = f"%{search_q}%"
        filtered_drivers_query = filtered_drivers_query.filter(
            or_(
                Driver.name.ilike(pattern),
                Driver.iqaama_number.ilike(pattern),
                Driver.driver_id.ilike(pattern),
            )
        )

    paged_total_drivers = filtered_drivers_query.count()
    total_pages = max(1, (paged_total_drivers + per_page - 1) // per_page)
    page = min(page, total_pages)

    drivers_query = filtered_drivers_query.order_by(Driver.id.desc())
    drivers_total = Driver.query.count()
    drivers_completed_total = Driver.query.filter(Driver.onboarding_stage == "Completed").count()
    drivers_rejected_total = Driver.query.filter(Driver.onboarding_stage == "Rejected").count()
    drivers_pending_total = max(0, drivers_total - drivers_completed_total - drivers_rejected_total)
    drivers = drivers_query.offset((page - 1) * per_page).limit(per_page).all()

    status_pairs = Offboarding.query.with_entities(Offboarding.driver_id, Offboarding.status).all()
    offboarded_ids = {driver_id for driver_id, status in status_pairs if status == "Completed"}
    in_offboarding_ids = {driver_id for driver_id, status in status_pairs if status != "Completed"}

    pending_q = (
        Offboarding.query.filter(Offboarding.status != "Completed")
        .order_by(Offboarding.updated_at.desc())
    )
    completed_q = (
        Offboarding.query.filter(Offboarding.status == "Completed")
        .order_by(Offboarding.updated_at.desc())
    )
    pending_total = pending_q.count()
    completed_total = completed_q.count()
    pending_offboarding_records = pending_q.offset((page - 1) * per_page).limit(per_page).all()
    completed_offboarding_records = completed_q.offset((page - 1) * per_page).limit(per_page).all()

    # -------------------------
    # Helper: serialize drivers
    # -------------------------
    def serialize_driver(d, offboarding_record=None):
        normalized_stage = normalize_onboarding_stage(d.onboarding_stage)
        data = {
            "id": d.id,
            "name": d.name,
            "branch_id": d.branch_id,
            "iqaama_number": d.iqaama_number,
            "iqaama_expiry": d.iqaama_expiry.isoformat() if d.iqaama_expiry else None,
            "nationality": d.nationality,
            "absher_number": d.absher_number,
            "previous_sponsor_number": d.previous_sponsor_number,
            "dob": d.dob.isoformat() if d.dob else None,
            "driver_profession": d.driver_profession,
            "email": d.email,
            "license_type": d.license_type,
            "license_expiry": d.license_expiry.isoformat() if d.license_expiry else None,
            "passport_no": d.passport_no,
            "passport_expiry_date": d.passport_expiry_date.isoformat() if d.passport_expiry_date else None,
            "remarks": d.remarks,
            "iqama_card_upload": d.iqama_card_upload,
            "iqama_card_upload_url": url_for("static", filename=f"uploads/{d.iqama_card_upload}") if d.iqama_card_upload else "",
            "driving_license_upload": d.driving_license_upload,
            "driving_license_upload_url": url_for("static", filename=f"uploads/{d.driving_license_upload}") if d.driving_license_upload else "",
            "passport_upload": d.passport_upload,
            "passport_upload_url": url_for("static", filename=f"uploads/{d.passport_upload}") if d.passport_upload else "",
            "saudi_driving_license": bool(d.saudi_driving_license),
            "city": d.city,
            "car_details": d.car_details,
            "assignment_date": d.assignment_date.isoformat() if d.assignment_date else None,
            "issued_mobile_number": d.issued_mobile_number,
            "issued_device_id": d.issued_device_id,
            "mobile_issued": bool(d.mobile_issued),
            "qiwa_contract_created": bool(d.qiwa_contract_created),
            "company_contract_created": bool(d.company_contract_created),
            "qiwa_contract_status": d.qiwa_contract_status,
            "ops_manager_approved": bool(d.ops_manager_approved),
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
            "onboarding_stage": normalized_stage,
            "rejected_by_stage": d.rejected_by_stage,
            "rejected_at": d.rejected_at.isoformat() if d.rejected_at else None,
            "reject_reason": d.reject_reason,
            "company_contract_file": d.company_contract_file,
            "promissory_note_file": d.promissory_note_file,
            "qiwa_contract_file": d.qiwa_contract_file,
            "company_contract_file_url": url_for("static", filename=f"uploads/{d.company_contract_file}") if d.company_contract_file else "",
            "promissory_note_file_url": url_for("static", filename=f"uploads/{d.promissory_note_file}") if d.promissory_note_file else "",
            "qiwa_contract_file_url": url_for("static", filename=f"uploads/{d.qiwa_contract_file}") if d.qiwa_contract_file else "",
            "transfer_fee_receipt_url": url_for("static", filename=f"uploads/{d.transfer_fee_receipt}") if d.transfer_fee_receipt else "",
            "sponsorship_transfer_proof_url": url_for("static", filename=f"uploads/{d.sponsorship_transfer_proof}") if d.sponsorship_transfer_proof else "",
            "tamm_authorization_ss_url": url_for("static", filename=f"uploads/{d.tamm_authorization_ss}") if d.tamm_authorization_ss else "",
            "offboarding_stage": d.offboarding_stage or (offboarding_record.status if offboarding_record else None),
            "fully_onboarded": normalized_stage == "Completed",
            "in_offboarding": d.id in in_offboarding_ids,
            "offboard_requested_by":d.offboard_requested_by if offboarding_record else None,
            "offboard_reason": d.offboard_reason if offboarding_record else None,
            "offboard_requested_at": safe_datetime(offboarding_record.requested_at) if offboarding_record else None,
            "documents": [
                {
                    "label": doc.notes or doc.document_type.replace("_", " ").title(),
                    "url": url_for("driver.preview_document", doc_id=doc.id),
                }
                for doc in d.documents.filter(DriverDocument.deleted_at.is_(None)).order_by(DriverDocument.created_at.desc()).all()
            ],
        }

        # Include offboarding record(s) as a list
        records = []
        if offboarding_record:
            records.append({
                "id": offboarding_record.id,
                "driver_id": offboarding_record.driver_id,
                "requested_by_id": offboarding_record.requested_by_id,
                "offboarding_requested_at": offboarding_record.requested_at.isoformat() if offboarding_record.requested_at else None,
                "status": offboarding_record.status,
                "ops_supervisor_cleared": offboarding_record.ops_supervisor_cleared,
                "ops_supervisor_cleared_at": offboarding_record.ops_supervisor_cleared_at.isoformat() if offboarding_record.ops_supervisor_cleared_at else None,
                "ops_supervisor_note": offboarding_record.ops_supervisor_note or "",
                "fleet_cleared": offboarding_record.fleet_cleared,
                "fleet_cleared_at": offboarding_record.fleet_cleared_at.isoformat() if offboarding_record.fleet_cleared_at else None,
                "fleet_damage_report": offboarding_record.fleet_damage_report or "",
                "fleet_damage_cost": offboarding_record.fleet_damage_cost or 0.0,
                "finance_cleared": offboarding_record.finance_cleared,
                "finance_cleared_at": offboarding_record.finance_cleared_at.isoformat() if offboarding_record.finance_cleared_at else None,
                "finance_note": offboarding_record.finance_note or "",
                "hr_cleared": offboarding_record.hr_cleared,
                "hr_cleared_at": offboarding_record.hr_cleared_at.isoformat() if offboarding_record.hr_cleared_at else None,
                "hr_note": offboarding_record.hr_note or "",
                "tamm_revoked": offboarding_record.tamm_revoked,
                "tamm_revoked_at": offboarding_record.tamm_revoked_at.isoformat() if offboarding_record.tamm_revoked_at else None,
                "company_contract_cancelled": offboarding_record.company_contract_cancelled,
                "qiwa_contract_cancelled": offboarding_record.qiwa_contract_cancelled,
                "salary_paid": offboarding_record.salary_paid,
                "updated_at": offboarding_record.updated_at.isoformat() if offboarding_record.updated_at else None,
            })
        data["records"] = records

        # Contract-pack info only matters once a driver is fully onboarded -
        # skip the extra queries for every other tab's rows.
        if normalized_stage == "Completed":
            data["generated_contracts"] = generated_contract_links(d)
            data["generated_promissory_note"] = generated_promissory_link(d)
            data["signed_promissory_note"] = signed_promissory_link(d)
            data["generate_contract_pack_url"] = url_for("hr.generate_contract_pack_completed", driver_id=d.id)
            data["upload_signed_promissory_url"] = url_for("hr.upload_signed_promissory_note", driver_id=d.id)

        return data


    # -------------------------
    # Serialize drivers
    # -------------------------
    driver_dicts = [serialize_driver(d) for d in drivers]
    # Skip any offboarding records without an attached driver to avoid AttributeError
    pending_offboarding_driver_dicts = [
        serialize_driver(o.driver, offboarding_record=o)
        for o in pending_offboarding_records
        if o.driver is not None
    ]
    completed_offboarding_driver_dicts = [
        serialize_driver(o.driver, offboarding_record=o)
        for o in completed_offboarding_records
        if o.driver is not None
    ]
    # Build the fully-onboarded list from all drivers (not just the current page)
    completed_drivers = Driver.query.filter(Driver.onboarding_stage == "Completed").order_by(Driver.id.desc()).all()
    fully_onboarded_only = [
        serialize_driver(d)
        for d in completed_drivers
        if d.id not in in_offboarding_ids
    ]

    rejected_drivers_records = (
        Driver.query.filter(Driver.onboarding_stage == "Rejected")
        .order_by(Driver.rejected_at.desc(), Driver.id.desc())
        .all()
    )
    rejected_drivers_only = [serialize_driver(d) for d in rejected_drivers_records]

    # -------------------------
    # Businesses and available IDs
    # -------------------------
    businesses = _cached_businesses()
    assigned_ids_subq = (
        db.session.query(DriverBusinessIDS.business_id_id)
        .filter(DriverBusinessIDS.transferred_at.is_(None))
        .subquery()
    )
    all_businesses = []
    for b in businesses:
        available_ids = (
            db.session.query(BusinessID)
            .filter(
                BusinessID.business_id == b.id,
                BusinessID.is_active == True,
                ~BusinessID.id.in_(assigned_ids_subq.select())
            ).all()
        )
        all_businesses.append({
            "id": b.id,
            "name": b.name,
            "available_ids": [{"id": bid.id, "value": bid.value} for bid in available_ids]
        })

    # -------------------------
    # Render template
    # -------------------------
    dashboard_branches = Branch.query.filter(Branch.deleted_at.is_(None)).order_by(Branch.name).all()
    onboarding_filter_stages = tuple(
        stage for stage in ONBOARDING_STAGES if stage not in {"Completed", "Rejected"}
    )
    offboarding_filter_stages = (
        ("OpsSupervisor", "Ops Supervisor"),
        ("Fleet", "Fleet"),
        ("Finance", "Finance"),
        ("HR", "HR"),
    )

    return render_template(
        "dashboard.html",
        drivers=driver_dicts,
        fully_onboarded_drivers=fully_onboarded_only,
        rejected_drivers=rejected_drivers_only,
        pending_offboarding_drivers=pending_offboarding_driver_dicts,
        completed_offboarding_drivers=completed_offboarding_driver_dicts,
        total_users=total_users,
        total_drivers=drivers_total,
        paged_total_drivers=paged_total_drivers,
        total_pending_onboarded=drivers_pending_total,
        total_completed_onboarded=drivers_completed_total,
        total_rejected=drivers_rejected_total,
        total_pending_offboarded=pending_total,
        total_completed_offboarded=completed_total,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        q=search_q,
        all_businesses=all_businesses,
        branches=dashboard_branches,
        branches_json=[{"id": b.id, "name": b.name} for b in dashboard_branches],
        onboarding_stages=ONBOARDING_STAGES,
        onboarding_filter_stages=onboarding_filter_stages,
        offboarding_filter_stages=offboarding_filter_stages,
        can_manage_completed_contracts=(
            user_has_permission(current_user, "onboarding.completed.manage_contracts")
            or user_has_role(current_user, "Admin", "SuperAdmin")
        ),
    )


# -------------------------
# Users (standalone page)
# -------------------------
@admin_bp.route("/users", methods=["GET"])
@login_required
def users():
    if not user_has_role(current_user, "SuperAdmin"):
        flash("Access denied. SuperAdmin role required.", "danger")
        return redirect(url_for("auth.login"))

    all_users = User.query.filter(User.id != current_user.id).all()
    users_dicts = [
        {
            "id": u.id,
            "username": u.username,
            "name": u.name,
            "designation": u.designation,
            "branch_city": u.branch_city,
            "email": u.email,
            "role": u.role,
            "locked_until": u.locked_until.isoformat() if u.locked_until else None,
        }
        for u in all_users
    ]

    return render_template(
        "admin_users.html",
        users=users_dicts,
        total_users=len(users_dicts),
        all_roles=Role.query.order_by(Role.name).all(),
        can_impersonate=user_has_permission(current_user, "users.impersonate"),
    )


# -------------------------
# Get Driver JSON
# -------------------------
@admin_bp.route("/driver/<int:driver_id>/json")
@login_required
def driver_json(driver_id):
    if not user_has_role(current_user, "Admin", "SuperAdmin"):
        return {"error": "Access denied"}, 403

    driver = Driver.query.get_or_404(driver_id)

    assigned = []
    active_ids = (
        DriverBusinessIDS.query
        .filter_by(driver_id=driver.id, transferred_at=None)
        .all()
    )
    if active_ids:
        business_id_ids = [link.business_id_id for link in active_ids]
        business_ids = BusinessID.query.filter(BusinessID.id.in_(business_id_ids)).all()
        for bid in business_ids:
            assigned.append({
                "business_id": bid.business_id,
                "platform_id": bid.id,
                "platform_value": bid.value,
            })

    driver_data = {
        "id": driver.id,
        "name": driver.name,
        "iqaama_number": driver.iqaama_number,
        "iqaama_expiry": driver.iqaama_expiry.isoformat() if driver.iqaama_expiry else None,
        "nationality": driver.nationality,
        "absher_number": driver.absher_number,
        "previous_sponsor_number": driver.previous_sponsor_number,
        "iqama_card_upload": driver.iqama_card_upload,
        "saudi_driving_license": driver.saudi_driving_license,
        "assigned_businesses": assigned,
        "issued_mobile_number": driver.issued_mobile_number,
        "issued_device_id": driver.issued_device_id,
        "mobile_issued": driver.mobile_issued,
        "car_details": driver.car_details,
        "assignment_date": driver.assignment_date.isoformat() if driver.assignment_date else None,
        "tamm_authorized": driver.tamm_authorized,
        "transfer_fee_paid": driver.transfer_fee_paid,
        "transfer_fee_amount": driver.transfer_fee_amount,
        "transfer_fee_paid_at": driver.transfer_fee_paid_at.isoformat() if driver.transfer_fee_paid_at else None,
        "qiwa_contract_status": driver.qiwa_contract_status,
        "onboarding_stage": normalize_onboarding_stage(driver.onboarding_stage),
        # file URLs
        "iqama_card_upload_url": url_for("static", filename=f"uploads/{driver.iqama_card_upload}") if driver.iqama_card_upload else "",
        "tamm_authorization_ss_url": getattr(driver, "tamm_authorization_ss_url", ""),
        "transfer_fee_receipt_url": getattr(driver, "transfer_fee_receipt_url", ""),
        "sponsorship_transfer_proof_url": getattr(driver, "sponsorship_transfer_proof_url", ""),
        "company_contract_file_url": getattr(driver, "company_contract_file_url", ""),
        "promissory_note_file_url": getattr(driver, "promissory_note_file_url", ""),
        "qiwa_contract_file_url": getattr(driver, "qiwa_contract_file_url", "")
    }

    return driver_data


# -------------------------
# Add Driver
# -------------------------
@admin_bp.route("/driver/add", methods=["POST"])
@login_required
def add_driver():
    if not user_has_role(current_user, "Admin", "SuperAdmin"):
        flash("Access denied", "danger")
        return redirect(url_for("auth.login"))

    form = AddDriverForm()
    if not form.validate_on_submit():
        # Surface field-level errors to the UI
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        current_app.logger.warning("[ADMIN] Add driver validation failed", extra={"errors": form.errors})
        return redirect(url_for("admin.dashboard"))

    transfer_paid_at_raw = form.transfer_fee_paid_at.data
    transfer_paid_at = None
    if transfer_paid_at_raw:
        try:
            transfer_paid_at = datetime.fromisoformat(transfer_paid_at_raw)
        except ValueError:
            transfer_paid_at = None
    driver = Driver(
        name=form.name.data,
        iqaama_number=form.iqaama_number.data,
        iqaama_expiry=form.iqaama_expiry.data,
        saudi_driving_license=form.saudi_driving_license.data == "true",
        nationality=form.nationality.data,
        city=form.city.data,
        absher_number=form.absher_number.data,
        previous_sponsor_number=form.previous_sponsor_number.data,
        issued_mobile_number=form.issued_mobile_number.data,
        issued_device_id=form.issued_device_id.data,
        mobile_issued=form.mobile_issued.data == "true",
        car_details=form.car_details.data,
        assignment_date=form.assignment_date.data,
        tamm_authorized=form.tamm_authorized.data == "true",
        transfer_fee_paid=form.transfer_fee_paid.data == "true",
        transfer_fee_amount=float(form.transfer_fee_amount.data) if form.transfer_fee_amount.data else None,
        transfer_fee_paid_at=transfer_paid_at,
        qiwa_contract_status=form.qiwa_contract_status.data or "Pending",
        onboarding_stage=form.onboarding_stage.data or "Ops Manager",
    )

    db.session.add(driver)
    try:
        db.session.flush()
    except IntegrityError as e:
        db.session.rollback()
        msg = str(getattr(e, "orig", e))
        flash(f"Failed to create driver: {msg}", "danger")
        current_app.logger.exception("[ADMIN] IntegrityError creating driver")
        return redirect(url_for("admin.dashboard"))

    business_ids = request.form.getlist("business_id[]")
    platform_ids = request.form.getlist("platform_id[]")
    try:
        update_driver_from_form(driver, request.form, business_ids, platform_ids)
        _store_dashboard_document_uploads(driver)
        db.session.commit()
        flash(f"✅ Driver {driver.name} created successfully.", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("[ADMIN] Error creating driver")
        flash(f"❌ Error creating driver: {str(e)}", "danger")

    return redirect(url_for("admin.dashboard"))

# -------------------------
# Update Existing Driver
# -------------------------
@admin_bp.route("/driver/<int:driver_id>/update", methods=["POST"])
@login_required
def update_driver(driver_id):
    if not user_has_role(current_user, "Admin", "SuperAdmin"):
        flash("Access denied", "danger")
        return redirect(url_for("auth.login"))

    form = AddDriverForm()
    if not form.validate_on_submit():
        flash("Please correct the driver form.", "danger")
        return redirect(url_for("admin.dashboard"))

    driver = Driver.query.get_or_404(driver_id)
    try:
        business_ids = request.form.getlist("business_id[]")
        platform_ids = request.form.getlist("platform_id[]")
        update_driver_from_form(driver, request.form, business_ids, platform_ids)
        _store_dashboard_document_uploads(driver)
        db.session.commit()
        flash(f"✅ Driver {driver.name} updated successfully.", "success")
        return redirect(url_for("admin.dashboard"))
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Error updating driver")
        flash(f"❌ Error updating driver: {str(e)}", "danger")
        return redirect(url_for("admin.dashboard"))


# -------------------------
# Reactivate Rejected Driver
# -------------------------
@admin_bp.route("/driver/<int:driver_id>/reactivate", methods=["POST"])
@login_required
def reactivate_driver(driver_id):
    if not user_has_role(current_user, "SuperAdmin"):
        flash("Access Denied", "danger")
        return redirect(url_for("admin.dashboard"))

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.dashboard"))

    driver = Driver.query.get_or_404(driver_id)
    if driver.onboarding_stage != "Rejected":
        flash(f"{driver.name} is not currently rejected.", "warning")
        return redirect(url_for("admin.dashboard"))

    # Full restart through the pipeline, same as a brand-new driver - clear
    # every stage-approval timestamp so later stages can't be skipped based
    # on leftover state from the rejected run.
    driver.onboarding_stage = "Ops Manager"
    driver.rejected_by_stage = None
    driver.rejected_at = None
    driver.reject_reason = None
    driver.ops_manager_approved = False
    driver.ops_manager_approved_at = None
    driver.ops_supervisor_approved_at = None
    driver.fleet_manager_approved_at = None
    driver.finance_approved_at = None
    driver.hr_approved_at = None

    try:
        db.session.commit()
        flash(f"{driver.name} has been reactivated and sent back to Ops Manager.", "success")
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to reactivate driver id=%s", driver_id)
        flash(f"Failed to reactivate driver: {e}", "danger")

    return redirect(url_for("admin.dashboard"))


# -------------------------
# Delete Driver
# -------------------------
@admin_bp.route("/dashboard/driver/<int:driver_id>/delete", methods=["POST"])
@login_required
def delete_driver(driver_id):
    from app import db

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        if is_ajax:
            return jsonify({"ok": False, "message": "Invalid request."}), 400
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.dashboard"))

    try:
        delete_driver_and_offboarding(driver_id)
        if is_ajax:
            return jsonify({"ok": True, "message": "Driver deleted successfully."}), 200
        flash("Driver and related offboarding records deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        if is_ajax:
            return jsonify({"ok": False, "message": f"Error deleting driver: {str(e)}"}), 500
        flash(f"Error deleting driver: {str(e)}", "danger")

    return redirect(url_for("admin.dashboard"))


# -------------------------
# Delete Offboarding Record (keeps the driver - just cancels/removes the
# offboarding in progress, e.g. one started by mistake or stuck)
# -------------------------
@admin_bp.route("/offboarding/<int:offboarding_id>/delete", methods=["POST"])
@login_required
def delete_offboarding_record(offboarding_id):
    if not user_has_role(current_user, "Admin", "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.dashboard"))

    record = Offboarding.query.get_or_404(offboarding_id)
    driver = record.driver
    driver_name = driver.name if driver else "driver"

    # Clear the legacy Driver-level flags too, so nothing lingers pointing
    # at an offboarding that no longer exists (same cleanup
    # ops_manager.reject_offboarding does when it discards a request).
    if driver:
        driver.offboard_request = False
        driver.offboard_requested_by = None
        driver.offboard_reason = None
        driver.offboard_requested_at = None

    try:
        db.session.delete(record)
        db.session.commit()
        flash(f"Offboarding record deleted for {driver_name}.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting offboarding record: {e}", "danger")

    return redirect(url_for("admin.dashboard"))


# -------------------------
# Add/Edit/Delete Users (unchanged)
# -------------------------
@admin_bp.route("/add_user", methods=["POST"])
@login_required
def add_user():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Access Denied", 403

    form = AddUserForm()
    form.role.choices = [(r.name, r.name) for r in Role.query.order_by(Role.name).all()]
    if not form.validate_on_submit():
        flash("Please correct the user form.", "danger")
        return redirect(url_for("admin.users"))

    username = form.username.data
    raw_password = form.password.data
    role = form.role.data
    name = form.name.data
    designation = form.designation.data
    branch_city = form.branch_city.data
    email = form.email.data

    try:
        new_user = create_user_from_form(
            username=username,
            raw_password=raw_password,
            role=role,
            name=name,
            designation=designation,
            branch_city=branch_city,
            email=email,
        )
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to create user: {e}", "danger")
        return redirect(url_for("admin.users"))

    # Send email notification
    sender = current_app.config.get("MAIL_DEFAULT_SENDER")
    email_sent = False

    try:
        msg = Message(
            "Your Account Has Been Created | تم إنشاء حسابك",
            recipients=[email],
            sender=sender,
        )
        msg.html = f"""
        <html dir="ltr" lang="en">
        <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
            <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">
                <div style="text-align: center; border-bottom: 3px solid #004aad; padding-bottom: 10px;">
                    <h2 style="color: #004aad;">iLab Information Technology</h2>
                    <p style="font-size: 14px; color: #777;">Account Notification</p>
                </div>

                <div style="margin-top: 20px;">
                    <p>Dear <strong>{name}</strong>,</p>
                    <p>We are pleased to inform you that your account has been successfully created.</p>
                    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd;"><strong>Username</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;">{username}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd;"><strong>Password</strong></td>
                            <td style="padding: 8px; border: 1px solid #ddd;">{raw_password}</td>
                        </tr>
                    </table>
                    <p>You can now log in to your account using the provided credentials on https://dobs.dobs.cloud/login.</p>

                    <p style="margin-top: 25px;">Best regards,<br><strong>iLab IT Support Team</strong></p>
                </div>

                <hr style="margin: 30px 0;">
                <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                    <p><strong>عزيزي {name}</strong>،</p>
                    <p>نود إعلامك بأنه تم إنشاء حسابك بنجاح.</p>
                    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd;">اسم المستخدم</td>
                            <td style="padding: 8px; border: 1px solid #ddd;">{username}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px; border: 1px solid #ddd;">كلمة المرور</td>
                            <td style="padding: 8px; border: 1px solid #ddd;">{raw_password}</td>
                        </tr>
                    </table>
                    <p>https://dobs.dobs.cloud/login يمكنك الآن تسجيل الدخول باستخدام بيانات الدخول أعلاه.</p>

                    <p style="margin-top: 25px;">مع أطيب التحيات،<br><strong>فريق دعم آي لاب لتقنية المعلومات</strong></p>
                </div>
            </div>
        </body>
        </html>
        """
        mail.send(msg)
        email_sent = True
    except Exception as e:
        current_app.logger.exception(
            "Welcome email failed for new user id=%s email=%s: %s",
            new_user.id,
            email,
            e,
        )
        flash(f"User created, but the welcome email failed: {e}", "warning")

    if email_sent:
        current_app.logger.info(
            "Welcome email accepted by mail server for new user id=%s email=%s",
            new_user.id,
            email,
        )
        flash("User created successfully and welcome email was accepted by the mail server.", "success")
    return redirect(url_for("admin.users"))

# -------------------------
# Edit User
# -------------------------
@admin_bp.route("/edit_user/<int:user_id>", methods=["POST"])
@login_required
@require_roles_or_owner("SuperAdmin", owner_loader=lambda user_id: User.query.get(user_id), owner_attr="id")
def edit_user(user_id):
    form = EditUserForm()
    form.role.choices = [(r.name, r.name) for r in Role.query.order_by(Role.name).all()]
    if not form.validate_on_submit():
        flash("Invalid or incomplete user data.", "danger")
        return redirect(url_for("admin.users"))

    user = User.query.get_or_404(user_id)
    try:
        if user_has_role(current_user, "SuperAdmin"):
            update_user_from_form(
                user,
                username=form.username.data or user.username,
                name=form.name.data or user.name,
                designation=form.designation.data or user.designation,
                branch_city=form.branch_city.data or user.branch_city,
                email=form.email.data or user.email,
                role=form.role.data or user.role,
            )
            flash(f"User {user.username} updated successfully.", "success")
        else:
            # Allow self-service updates for non-privileged users without role/username changes
            user.name = form.name.data or user.name
            user.designation = form.designation.data or user.designation
            user.branch_city = form.branch_city.data or user.branch_city
            user.email = form.email.data or user.email
            db.session.commit()
            flash("Profile updated successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to update user: {e}", "danger")
    return redirect(url_for("admin.users"))

# -------------------------
# Unlock / reset another user's access (SuperAdmin only)
# -------------------------
@admin_bp.route("/users/<int:user_id>/unlock", methods=["POST"])
@login_required
def unlock_user(user_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Access Denied", 403
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.users"))
    user = User.query.get_or_404(user_id)
    user.failed_logins = 0
    user.locked_until = None
    db.session.commit()
    flash(f"Login lock cleared for {user.username}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def reset_user_password(user_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Access Denied", 403
    form = CSRFOnlyForm()
    password = (request.form.get("new_password") or "").strip()
    if not form.validate_on_submit() or len(password) < 6:
        flash("Enter a password of at least 6 characters.", "danger")
        return redirect(url_for("admin.users"))
    user = User.query.get_or_404(user_id)
    change_user_password(user, password)
    user.failed_logins = 0
    user.locked_until = None
    db.session.commit()
    flash(f"Password reset and login lock cleared for {user.username}.", "success")
    return redirect(url_for("admin.users"))


# -------------------------
# Delete User
# -------------------------
@admin_bp.route("/delete_user/<int:user_id>", methods=["POST"])
@login_required
def delete_user(user_id):
    if not user_has_role(current_user, "SuperAdmin"):
        flash("Access Denied", "danger")
        return redirect(url_for("admin.users"))

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.users"))

    user = User.query.get_or_404(user_id)
    try:
        delete_user_service(user, acting_user_id=current_user.id)
        flash(f"User {user.username} deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to delete user: {e}", "danger")
    return redirect(url_for("admin.users"))

# -------------------------
# Change Password (for SuperAdmin)
# -------------------------
@admin_bp.route("/change_password", methods=["POST"])
@limiter.limit("5 per minute")
@login_required
def change_password():
    """Allow SuperAdmin to change their password securely and send email notification."""
    if not user_has_role(current_user, "SuperAdmin"):
        flash("Access Denied", "danger")
        return redirect(url_for("admin.dashboard"))

    form = ChangePasswordForm()
    if not form.validate_on_submit():
        flash("Please correct the password form.", "danger")
        return redirect(url_for("admin.dashboard"))

    current_password = form.current_password.data
    new_password = form.new_password.data

    if not verify_password_hash(current_user.password, current_password):
        flash("Current password is incorrect.", "danger")
        return redirect(url_for("admin.dashboard"))

    try:
        # Update password in database
        change_user_password(current_user, new_password)

        # Send email notification using helper
        if send_password_change_email(current_user, new_password):
            flash("Password updated and email notification sent.", "success")
        else:
            flash("Password updated, but email could not be sent.", "warning")

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"[ADMIN] Failed to change password: {e}")
        flash("Could not update password right now. Try again later.", "danger")

    return redirect(url_for("admin.dashboard"))


# -------------------------
# Onboarding Workflow Builder
#
# Admin-editable data driving services/onboarding_workflow.py - adding a
# driver type's stage sequence and related workflow behavior happens here,
# not in code.
# -------------------------
@admin_bp.route("/workflow-config", methods=["GET"])
@login_required
def workflow_config():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    driver_types = DriverType.query.filter(DriverType.deleted_at.is_(None)).order_by(DriverType.name).all()
    stage_templates = OnboardingStageTemplate.query.order_by(
        OnboardingStageTemplate.driver_type_id, OnboardingStageTemplate.sequence_order
    ).all()
    type_settings = {row.driver_type_id: row for row in DriverTypeSettings.query.all()}

    stages_by_type = {}
    for row in stage_templates:
        stages_by_type.setdefault(row.driver_type_id, []).append(row)

    return render_template(
        "admin_workflow_config.html",
        driver_types=driver_types,
        stages_by_type=stages_by_type,
        type_settings=type_settings,
        stage_form=OnboardingStageTemplateForm(),
        settings_form=DriverTypeSettingsForm(),
    )


@admin_bp.route("/contract-templates", methods=["GET"])
@login_required
def contract_templates():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    edit_template_id = request.args.get("edit_template_id", type=int)
    action = (request.args.get("action") or "").strip().lower()
    show_contract_form = action == "new" or bool(edit_template_id)
    editing_template = (
        ContractTemplate.query.filter(
            ContractTemplate.id == edit_template_id,
            or_(ContractTemplate.document_kind.is_(None), ContractTemplate.document_kind == "driver_contract"),
        ).first()
        if edit_template_id else None
    )
    form_seed = editing_template or ContractTemplate(**_contract_template_defaults())
    templates = (
        ContractTemplate.query.filter(
            or_(ContractTemplate.document_kind.is_(None), ContractTemplate.document_kind == "driver_contract")
        )
        .order_by(ContractTemplate.updated_at.desc(), ContractTemplate.name.asc())
        .all()
    )

    return render_template(
        "admin_contract_templates.html",
        contract_templates=templates,
        businesses=_cached_businesses(),
        driver_types=DriverType.query.filter(DriverType.deleted_at.is_(None)).order_by(DriverType.name).all(),
        contract_form=ContractTemplateForm(obj=form_seed),
        editing_template=editing_template,
        show_contract_form=show_contract_form,
        contract_defaults=_contract_template_defaults(),
        company_preset=_get_company_preset(),
        company_preset_form=CompanyPresetForm(obj=_get_company_preset()),
    )


@admin_bp.route("/workflow-config/stage-template/add", methods=["POST"])
@login_required
def add_stage_template():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = OnboardingStageTemplateForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("admin.workflow_config"))

    row = OnboardingStageTemplate(
        driver_type_id=form.driver_type_id.data,
        sequence_order=form.sequence_order.data,
        stage_name=form.stage_name.data,
        skip_condition_field=form.skip_condition_field.data or None,
        skip_condition_value=form.skip_condition_value.data or None,
    )
    db.session.add(row)
    try:
        db.session.commit()
        flash("Stage added to the onboarding sequence.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("That driver type already has a stage at this order position.", "danger")
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("[ADMIN] Failed to add stage template")
        flash(f"Failed to add stage: {e}", "danger")

    return redirect(url_for("admin.workflow_config"))


@admin_bp.route("/workflow-config/stage-template/<int:row_id>/delete", methods=["POST"])
@login_required
def delete_stage_template(row_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.workflow_config"))

    row = OnboardingStageTemplate.query.get_or_404(row_id)
    db.session.delete(row)
    db.session.commit()
    flash("Stage removed from the onboarding sequence.", "success")
    return redirect(url_for("admin.workflow_config"))


@admin_bp.route("/workflow-config/driver-type-settings/save", methods=["POST"])
@login_required
def save_driver_type_settings():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = DriverTypeSettingsForm()
    if not form.validate_on_submit():
        flash("Please choose a driver type and contract mode.", "danger")
        return redirect(url_for("admin.workflow_config"))

    settings = DriverTypeSettings.query.get(form.driver_type_id.data)
    if settings:
        settings.contract_mode = form.contract_mode.data
        settings.requires_qiwa_contract = form.requires_qiwa_contract.data
    else:
        settings = DriverTypeSettings(
            driver_type_id=form.driver_type_id.data,
            contract_mode=form.contract_mode.data,
            requires_qiwa_contract=form.requires_qiwa_contract.data,
        )
        db.session.add(settings)

    db.session.commit()
    flash("Driver type settings saved.", "success")
    return redirect(url_for("admin.workflow_config"))


@admin_bp.route("/workflow-config/contract-template/add", methods=["POST"])
@login_required
def add_contract_template():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = ContractTemplateForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("admin.contract_templates"))

    template = _fill_contract_template_from_form(ContractTemplate(), form)
    db.session.add(template)
    db.session.commit()
    flash(f"Contract template '{template.name}' created.", "success")
    return redirect(url_for("admin.contract_templates"))


@admin_bp.route("/workflow-config/contract-template/<int:template_id>/update", methods=["POST"])
@login_required
def update_contract_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = ContractTemplateForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("admin.contract_templates", edit_template_id=template_id))

    template = ContractTemplate.query.get_or_404(template_id)
    _fill_contract_template_from_form(template, form)
    db.session.commit()
    flash(f"Contract template '{template.name}' updated.", "success")
    return redirect(url_for("admin.contract_templates"))


@admin_bp.route("/workflow-config/contract-template/<int:template_id>/preview", methods=["GET"])
@login_required
def preview_contract_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    template = ContractTemplate.query.get_or_404(template_id)
    pdf_bytes = render_contract_template_preview(template)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{template.name.replace(' ', '_')}_preview.pdf",
    )


@admin_bp.route("/workflow-config/contract-template/<int:template_id>/duplicate", methods=["POST"])
@login_required
def duplicate_contract_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.contract_templates"))

    source = ContractTemplate.query.get_or_404(template_id)

    skip_columns = {"id", "created_at", "updated_at"}
    copy_data = {
        column.name: getattr(source, column.name)
        for column in ContractTemplate.__table__.columns
        if column.name not in skip_columns
    }
    copy_data["name"] = f"{source.name} (Copy)"
    # Duplicates always start inactive so they can't be picked up by
    # _resolve_template() and start generating contracts before someone has
    # reviewed/edited the copy.
    copy_data["is_active"] = False

    duplicate = ContractTemplate(**copy_data)
    db.session.add(duplicate)
    db.session.commit()
    flash(f"Duplicated '{source.name}' as '{duplicate.name}' (inactive).", "success")
    return redirect(url_for("admin.contract_templates", edit_template_id=duplicate.id))


@admin_bp.route("/workflow-config/contract-template/<int:template_id>/toggle-active", methods=["POST"])
@login_required
def toggle_contract_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.contract_templates"))

    template = ContractTemplate.query.get_or_404(template_id)
    template.is_active = not template.is_active
    db.session.commit()
    flash(f"Contract template '{template.name}' is now {'active' if template.is_active else 'inactive'}.", "success")
    return redirect(url_for("admin.contract_templates"))


@admin_bp.route("/workflow-config/contract-template/<int:template_id>/delete", methods=["POST"])
@login_required
def delete_contract_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.contract_templates"))

    template = ContractTemplate.query.get_or_404(template_id)
    db.session.delete(template)
    db.session.commit()
    flash("Contract template deleted.", "success")
    return redirect(url_for("admin.contract_templates"))


# -------------------------
# Promissory Note Templates
#
# Same ContractTemplate table/scenario as driver contracts above, scoped by
# document_kind="promissory_note" - one template for every driver regardless
# of driver type or platform (see services/contracts.py generate_promissory_note),
# so business_id/driver_type_id are always forced to None here.
# -------------------------
@admin_bp.route("/promissory-note-templates", methods=["GET"])
@login_required
def promissory_note_templates():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    edit_template_id = request.args.get("edit_template_id", type=int)
    action = (request.args.get("action") or "").strip().lower()
    show_form = action == "new" or bool(edit_template_id)
    editing_template = (
        ContractTemplate.query.filter_by(id=edit_template_id, document_kind="promissory_note").first()
        if edit_template_id else None
    )
    form_seed = editing_template or ContractTemplate(**_promissory_note_template_defaults())
    templates = (
        ContractTemplate.query.filter_by(document_kind="promissory_note")
        .order_by(ContractTemplate.updated_at.desc(), ContractTemplate.name.asc())
        .all()
    )

    return render_template(
        "admin_promissory_note_templates.html",
        promissory_note_templates=templates,
        promissory_note_form=PromissoryNoteTemplateForm(obj=form_seed),
        editing_template=editing_template,
        show_form=show_form,
        template_defaults=_promissory_note_template_defaults(),
        company_preset=_get_company_preset(),
        company_preset_form=CompanyPresetForm(obj=_get_company_preset()),
    )


@admin_bp.route("/workflow-config/promissory-note-template/add", methods=["POST"])
@login_required
def add_promissory_note_template():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = PromissoryNoteTemplateForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("admin.promissory_note_templates"))

    template = _fill_promissory_note_template_from_form(ContractTemplate(), form)
    db.session.add(template)
    db.session.commit()
    flash(f"Promissory note template '{template.name}' created.", "success")
    return redirect(url_for("admin.promissory_note_templates"))


@admin_bp.route("/workflow-config/promissory-note-template/<int:template_id>/update", methods=["POST"])
@login_required
def update_promissory_note_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = PromissoryNoteTemplateForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("admin.promissory_note_templates", edit_template_id=template_id))

    template = ContractTemplate.query.filter_by(id=template_id, document_kind="promissory_note").first_or_404()
    _fill_promissory_note_template_from_form(template, form)
    db.session.commit()
    flash(f"Promissory note template '{template.name}' updated.", "success")
    return redirect(url_for("admin.promissory_note_templates"))


@admin_bp.route("/workflow-config/promissory-note-template/<int:template_id>/preview", methods=["GET"])
@login_required
def preview_promissory_note_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    template = ContractTemplate.query.filter_by(id=template_id, document_kind="promissory_note").first_or_404()
    pdf_bytes = render_contract_template_preview(template)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{template.name.replace(' ', '_')}_preview.pdf",
    )


@admin_bp.route("/workflow-config/promissory-note-template/<int:template_id>/toggle-active", methods=["POST"])
@login_required
def toggle_promissory_note_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.promissory_note_templates"))

    template = ContractTemplate.query.filter_by(id=template_id, document_kind="promissory_note").first_or_404()
    template.is_active = not template.is_active
    db.session.commit()
    flash(f"Promissory note template '{template.name}' is now {'active' if template.is_active else 'inactive'}.", "success")
    return redirect(url_for("admin.promissory_note_templates"))


@admin_bp.route("/workflow-config/promissory-note-template/<int:template_id>/delete", methods=["POST"])
@login_required
def delete_promissory_note_template(template_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.promissory_note_templates"))

    template = ContractTemplate.query.filter_by(id=template_id, document_kind="promissory_note").first_or_404()
    db.session.delete(template)
    db.session.commit()
    flash("Promissory note template deleted.", "success")
    return redirect(url_for("admin.promissory_note_templates"))


@admin_bp.route("/workflow-config/company-preset/save", methods=["POST"])
@login_required
def save_company_preset():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CompanyPresetForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
    else:
        preset = _get_company_preset()
        preset.first_party_name = form.first_party_name.data or None
        preset.first_party_name_ar = form.first_party_name_ar.data or None
        preset.first_party_label = form.first_party_label.data or None
        preset.first_party_label_ar = form.first_party_label_ar.data or None
        preset.second_party_label = form.second_party_label.data or None
        preset.second_party_label_ar = form.second_party_label_ar.data or None
        preset.company_signatory_name = form.company_signatory_name.data or None
        preset.company_signatory_title = form.company_signatory_title.data or None
        db.session.commit()
        flash("Company preset updated. New templates will start from these defaults.", "success")

    redirect_to = request.form.get("redirect_to")
    if redirect_to == "promissory":
        return redirect(url_for("admin.promissory_note_templates"))
    return redirect(url_for("admin.contract_templates"))


# -------------------------
# Per-user access: multi-role grants + standalone permission overrides
#
# This is what makes "give this Ops Manager extra Fleet access" or "let
# this specific employee request offboarding regardless of role" an
# admin-dashboard action instead of a code change.
# -------------------------
@admin_bp.route("/user/<int:user_id>/access", methods=["GET"])
@login_required
def user_access(user_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    user = User.query.get_or_404(user_id)
    all_roles = Role.query.order_by(Role.name).all()
    all_permissions = Permission.query.order_by(Permission.code).all()
    granted_role_ids = {row.role_id for row in UserRole.query.filter_by(user_id=user.id).all()}
    overrides = {row.permission_id: row.granted for row in UserPermission.query.filter_by(user_id=user.id).all()}

    return render_template(
        "admin_user_access.html",
        user=user,
        all_roles=all_roles,
        all_permissions=all_permissions,
        granted_role_ids=granted_role_ids,
        overrides=overrides,
        can_impersonate=user_has_permission(current_user, "users.impersonate"),
    )


@admin_bp.route("/user/<int:user_id>/access/roles", methods=["POST"])
@login_required
def save_user_roles(user_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.user_access", user_id=user_id))

    user = User.query.get_or_404(user_id)
    selected_role_ids = {int(v) for v in request.form.getlist("role_ids")}
    all_roles = Role.query.all()

    for role in all_roles:
        if role.id in selected_role_ids:
            grant_role(user, role.name)
        else:
            revoke_role(user, role.name)

    db.session.commit()
    flash(f"Roles updated for {user.username}.", "success")
    return redirect(url_for("admin.user_access", user_id=user_id))


@admin_bp.route("/user/<int:user_id>/access/permissions", methods=["POST"])
@login_required
def save_user_permissions(user_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.user_access", user_id=user_id))

    user = User.query.get_or_404(user_id)
    # Radio group per permission: "grant" | "revoke" | "inherit" (no override)
    for perm in Permission.query.all():
        choice = request.form.get(f"perm_{perm.id}", "inherit")
        if choice == "grant":
            set_permission_override(user, perm.code, True)
        elif choice == "revoke":
            set_permission_override(user, perm.code, False)
        else:
            clear_permission_override(user, perm.code)

    db.session.commit()
    flash(f"Permission overrides updated for {user.username}.", "success")
    return redirect(url_for("admin.user_access", user_id=user_id))


@admin_bp.route("/user/<int:user_id>/impersonate", methods=["POST"])
@login_required
@require_permission("users.impersonate")
def impersonate_user(user_id):
    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.users"))

    target = User.query.get_or_404(user_id)
    admin_username = current_user.username
    try:
        impersonation.start(current_user, target, session, ip_address=request.remote_addr)
    except ImpersonationError as e:
        flash(str(e), "danger")
        return redirect(url_for("admin.users"))

    login_user(target)
    current_app.logger.info(
        "impersonation_start",
        extra={"admin": admin_username, "target": target.username},
    )
    flash(f"Now viewing DOBS as {target.name or target.username}.", "warning")
    return _post_login_redirect(target)


# ------------------------
# Role management (create roles, define their default permissions)
# ------------------------

# Seeded by migrations/versions/20260707_rbac_workflow_config.py - these
# names are still matched by raw-string role checks elsewhere in the app
# (dobs_user.role, base.html's nav, ROLE_DASHBOARD_ENDPOINTS), so renaming
# or deleting them here would silently desync those. Protected, not just by
# convention - enforced in delete_role below.
LEGACY_ROLE_NAMES = {
    "SuperAdmin", "HR", "HRManager", "OpsManager", "OpsSupervisor",
    "OpsCoordinator", "FleetManager", "Finance", "FinanceManager", "Admin",
}


@admin_bp.route("/roles", methods=["GET"])
@login_required
def roles():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    all_roles = Role.query.order_by(Role.name).all()
    user_counts = {
        row[0]: row[1]
        for row in db.session.query(UserRole.role_id, db.func.count(UserRole.user_id))
        .group_by(UserRole.role_id).all()
    }
    return render_template(
        "admin_roles.html",
        roles=all_roles,
        legacy_role_names=LEGACY_ROLE_NAMES,
        user_counts=user_counts,
        role_form=RoleForm(),
        dashboard_choices=DASHBOARD_ENDPOINT_CHOICES,
    )


@admin_bp.route("/roles/add", methods=["POST"])
@login_required
def add_role():
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = RoleForm()
    if not form.validate_on_submit():
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        return redirect(url_for("admin.roles"))

    if Role.query.filter_by(name=form.name.data).first():
        flash("A role with that name already exists.", "danger")
        return redirect(url_for("admin.roles"))

    db.session.add(Role(
        name=form.name.data.strip(),
        description=form.description.data or None,
        dashboard_endpoint=form.dashboard_endpoint.data or None,
    ))
    db.session.commit()
    flash(f"Role '{form.name.data}' created.", "success")
    return redirect(url_for("admin.roles"))


@admin_bp.route("/roles/<int:role_id>/set-dashboard", methods=["POST"])
@login_required
def set_role_dashboard(role_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.roles"))

    role = Role.query.get_or_404(role_id)
    if role.name in LEGACY_ROLE_NAMES:
        flash(f"'{role.name}' is a built-in role - its dashboard cannot be changed here.", "danger")
        return redirect(url_for("admin.roles"))

    valid_endpoints = {code for code, _label in DASHBOARD_ENDPOINT_CHOICES}
    endpoint = request.form.get("dashboard_endpoint", "")
    if endpoint not in valid_endpoints:
        flash("Unknown dashboard.", "danger")
        return redirect(url_for("admin.roles"))

    role.dashboard_endpoint = endpoint or None
    db.session.commit()
    flash(f"Dashboard updated for role '{role.name}'.", "success")
    return redirect(url_for("admin.roles"))


@admin_bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@login_required
def delete_role(role_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.roles"))

    role = Role.query.get_or_404(role_id)
    if role.name in LEGACY_ROLE_NAMES:
        flash(f"'{role.name}' is a built-in role and cannot be deleted.", "danger")
        return redirect(url_for("admin.roles"))

    in_use = (
        UserRole.query.filter_by(role_id=role.id).first()
        or User.query.filter_by(role=role.name).first()
    )
    if in_use:
        flash(f"'{role.name}' is still assigned to at least one user and cannot be deleted.", "danger")
        return redirect(url_for("admin.roles"))

    RolePermission.query.filter_by(role_id=role.id).delete()
    db.session.delete(role)
    db.session.commit()
    flash(f"Role '{role.name}' deleted.", "success")
    return redirect(url_for("admin.roles"))


@admin_bp.route("/roles/<int:role_id>/permissions", methods=["GET"])
@login_required
def role_permissions(role_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    role = Role.query.get_or_404(role_id)
    all_permissions = Permission.query.order_by(Permission.code).all()
    granted_codes = role_permission_codes(role)

    grouped = {}
    for perm in all_permissions:
        category = perm.code.split(".")[0]
        grouped.setdefault(category, []).append(perm)

    return render_template(
        "admin_role_permissions.html",
        role=role,
        grouped_permissions=grouped,
        granted_codes=granted_codes,
    )


@admin_bp.route("/roles/<int:role_id>/permissions", methods=["POST"])
@login_required
def save_role_permissions(role_id):
    if not user_has_role(current_user, "SuperAdmin"):
        return "Forbidden", 403

    form = CSRFOnlyForm()
    if not form.validate_on_submit():
        flash("Invalid request.", "danger")
        return redirect(url_for("admin.role_permissions", role_id=role_id))

    role = Role.query.get_or_404(role_id)
    selected_codes = set(request.form.getlist("permission_codes"))
    set_role_permissions(role, selected_codes)
    db.session.commit()
    flash(f"Permissions updated for role '{role.name}'.", "success")
    return redirect(url_for("admin.role_permissions", role_id=role_id))
