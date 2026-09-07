import os
from datetime import datetime
from typing import Dict

from flask import current_app
from flask_mail import Message

from services.file_storage import is_allowed_file, save_upload, safe_ext_filename, validate_upload, save_to_shared_storage, link_into_shared_storage
from services import onboarding_workflow
from models import QIWA_CONTRACT_STATUSES, TRANSFER_STATUSES, DriverTypeSettings

# Maps Driver column names used in HR approval -> shared document_type
_HR_FIELD_TO_DOC_TYPE = {
    "company_contract_file": "contract",
    "promissory_note_file":  "other",
    "qiwa_contract_file":    "contract",
    "other_document_files":  "other",
}


def _insert_driver_document(db, driver, document_type: str, file_storage, relative_path: str, uploaded_by_id=None):
    from models import DriverDocument
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    doc = DriverDocument(
        driver_id     = driver.id,
        document_type = document_type,
        file_path     = relative_path,
        original_name = file_storage.filename,
        file_size     = size,
        uploaded_from = "dobs",
        uploaded_by   = uploaded_by_id,
    )
    db.session.add(doc)


def process_hr_approval(driver, files: Dict[str, object], form_data: Dict[str, object], upload_folder: str, max_bytes: int, db=None, uploaded_by_id=None):
    """Handle HR approval: save files to legacy location and dual-write to shared storage."""
    base_name = (driver.name or "driver").replace(" ", "_")
    iqama = driver.iqaama_number or "unknown"
    prefix = f"{base_name}_{iqama}"

    shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH", upload_folder)

    for db_field in files:
        file_list = files.getlist(db_field) if hasattr(files, "getlist") else [files[db_field]]
        for file_storage in file_list:
            if not file_storage or not getattr(file_storage, "filename", None):
                continue
            original = file_storage.filename
            validate_upload(file_storage, max_bytes)
            filename = safe_ext_filename(prefix, db_field, original)
            saved_name = save_upload(file_storage, upload_folder, filename)
            if hasattr(driver, db_field):
                setattr(driver, db_field, filename)

            if db is not None and driver.id:
                doc_type = _HR_FIELD_TO_DOC_TYPE.get(db_field, "other")
                try:
                    # Hard-link the file we just saved rather than writing the
                    # same bytes a second time under a different name -- see
                    # link_into_shared_storage() for why that mattered.
                    relative_path = link_into_shared_storage(
                        os.path.join(upload_folder, saved_name), driver.id, shared_root, saved_name
                    )
                    _insert_driver_document(db, driver, doc_type, file_storage, relative_path, uploaded_by_id)
                except Exception:
                    current_app.logger.exception("Publishing to shared storage failed for field %s", db_field)

    # Qiwa contracts are a company-sponsorship concept - only Sponsor-type
    # drivers can have one, per driver_type's requires_qiwa_contract flag
    # (admin-editable, not hardcoded). Freelancer/Manpower submissions are
    # silently forced False rather than erroring, since the UI already
    # hides the field for those types - this is defense in depth.
    type_settings = DriverTypeSettings.query.get(driver.driver_type_id)
    qiwa_allowed = bool(type_settings and type_settings.requires_qiwa_contract)

    driver.qiwa_contract_created = qiwa_allowed and bool(form_data.get("qiwa_contract_created"))
    driver.company_contract_created = bool(form_data.get("company_contract_created"))

    qiwa_status = form_data.get("qiwa_contract_status", "Pending") or "Pending"
    transfer_status = form_data.get("sponsorship_transfer_status", "Pending") or "Pending"
    if not qiwa_allowed:
        qiwa_status = "Pending"
        transfer_status = "Not Required"

    if qiwa_status not in QIWA_CONTRACT_STATUSES:
        raise ValueError("Invalid qiwa_contract_status")
    if transfer_status not in TRANSFER_STATUSES:
        raise ValueError("Invalid sponsorship_transfer_status")

    driver.qiwa_contract_status = qiwa_status
    driver.sponsorship_transfer_status = transfer_status

    # Driver address (optional -- company accommodation is assigned later in
    # DMS) and bank details (one bank account + one STC Pay, both required at
    # this stage) -> written into the shared drivers / driver_bank_accounts
    # tables that DMS reads.
    address_val = (form_data.get("address") or "").strip()
    if hasattr(driver, "address"):
        driver.address = address_val or None

    if db is not None and driver.id:
        from models import DriverBankAccount

        bank_name = (form_data.get("bank_name") or "").strip()
        account_number = (form_data.get("account_number") or "").strip()
        stc_pay_number = (form_data.get("stc_pay_number") or "").strip()

        if bank_name or account_number:
            bank_row = DriverBankAccount.query.filter_by(
                driver_id=driver.id, account_type=DriverBankAccount.TYPE_BANK
            ).filter(DriverBankAccount.deleted_at.is_(None)).first()
            if not bank_row:
                bank_row = DriverBankAccount(driver_id=driver.id, account_type=DriverBankAccount.TYPE_BANK)
                db.session.add(bank_row)
            bank_row.bank_name = bank_name or None
            bank_row.account_number = account_number or None
            bank_row.stc_pay_number = None
            bank_row.is_active = True

        if stc_pay_number:
            stc_row = DriverBankAccount.query.filter_by(
                driver_id=driver.id, account_type=DriverBankAccount.TYPE_STC_PAY
            ).filter(DriverBankAccount.deleted_at.is_(None)).first()
            if not stc_row:
                stc_row = DriverBankAccount(driver_id=driver.id, account_type=DriverBankAccount.TYPE_STC_PAY)
                db.session.add(stc_row)
            stc_row.stc_pay_number = stc_pay_number or None
            stc_row.bank_name = None
            stc_row.account_number = None
            stc_row.is_active = True

    driver.hr_approved_at = datetime.utcnow()
    onboarding_workflow.advance(driver, from_stage="HR")

    # Generic promissory note - not business-specific, so it can be
    # generated now even though platform/business assignment hasn't
    # happened yet (that's Ops Supervisor's job, still ahead in the
    # sequence). The business-specific Driver Contract is generated later,
    # at HR Final, once the business assignment is known.
    if db is not None and driver.id:
        try:
            from services import contracts
            contracts.generate_promissory_note(driver, uploaded_by_id=uploaded_by_id)
        except Exception:
            current_app.logger.exception("Promissory note generation failed for driver_id=%s", driver.id)


def save_transfer_proof(driver, file_storage, upload_folder: str, status: str, max_bytes: int, db=None, uploaded_by_id=None):
    if status != "Transferred":
        raise ValueError("Transfer status must be 'Transferred' when uploading proof.")
    validate_upload(file_storage, max_bytes)

    base_name = (driver.name or "driver").replace(" ", "_")
    iqama = driver.iqaama_number or "unknown"
    filename = safe_ext_filename(base_name, "transfer_proof", file_storage.filename)
    saved_name = save_upload(file_storage, upload_folder, filename)

    driver.sponsorship_transfer_status = status
    driver.sponsorship_transfer_proof = filename
    driver.sponsorship_transfer_completed_at = datetime.utcnow()

    if db is not None and driver.id:
        shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH", upload_folder)
        try:
            relative_path = link_into_shared_storage(
                os.path.join(upload_folder, saved_name), driver.id, shared_root, saved_name
            )
            _insert_driver_document(db, driver, "other", file_storage, relative_path, uploaded_by_id)
        except Exception:
            current_app.logger.exception("Publishing to shared storage failed for transfer_proof")


def send_rejection_email(mail, recipients, driver_name, driver_iqaama, nationality, reason, rejected_by):
    if not recipients:
        return
    msg = Message(
        subject=f"Driver Rejected by HR | تم رفض السائق: {driver_name}",
        recipients=recipients,
    )
    msg.html = f"""
    <html><body style='font-family: Arial, sans-serif;'>
    <div style='max-width:600px;margin:auto;padding:20px;background:#fff;'>
      <h2 style='color:#713183;'>Driver Rejection Notification</h2>
      <p>Driver <strong>{driver_name}</strong> (iqaama: <strong>{driver_iqaama or 'N/A'}</strong>, Nationality: <strong>{nationality}</strong>) was rejected by HR.</p>
      <p><strong>Reason:</strong><br>{reason}</p>
      <p>Rejected by: {rejected_by}</p>
      <hr>
      <h3 dir='rtl' style='text-align:right;color:#713183;'>إشعار رفض السائق</h3>
      <p dir='rtl' style='text-align:right;'>تم رفض السائق <strong>{driver_name}</strong> (رقم الإقامة: <strong>{driver_iqaama or 'N/A'}</strong>) من قبل الموارد البشرية.</p>
      <p dir='rtl' style='text-align:right;'><strong>سبب الرفض:</strong><br>{reason}</p>
    </div></body></html>
    """
    mail.send(msg)
