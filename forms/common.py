import json

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import BooleanField, DateField, DecimalField, IntegerField, PasswordField, SelectField, StringField, TextAreaField
from wtforms.validators import Optional, DataRequired, Email, Length, EqualTo, ValidationError

from models import ONBOARDING_STAGES


# Keep in sync with app.py's _NAV_CATALOG keys - the set of dashboard pages
# a role can be pointed at. A role with no dashboard assigned falls back to
# a generic landing page rather than a dead end after login.
DASHBOARD_ENDPOINT_CHOICES = [
    ("", "— No dashboard assigned yet —"),
    ("admin.dashboard", "SuperAdmin Dashboard"),
    ("hr.dashboard_hr", "HR Dashboard"),
    ("ops_manager.dashboard_ops", "Ops Manager Dashboard"),
    ("ops_supervisor.dashboard_ops_supervisor", "Ops Supervisor Dashboard"),
    ("ops_coordinator.dashboard_ops_coordinator", "Ops Coordinator Dashboard"),
    ("fleet.dashboard_fleet", "Fleet Dashboard"),
    ("finance.dashboard_finance", "Finance Dashboard"),
]


class RoleForm(FlaskForm):
    name = StringField("Role Name", validators=[DataRequired(), Length(max=50)])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    dashboard_endpoint = SelectField("Dashboard", validators=[Optional()], choices=DASHBOARD_ENDPOINT_CHOICES)


class CSRFOnlyForm(FlaskForm):
    """Form used to enforce CSRF on action-only POSTs."""
    pass


class ReportsFilterForm(FlaskForm):
    report_type = SelectField(
        "Report Type",
        choices=[
            ("all", "All Drivers"),
            ("onboarding", "Onboarded"),
            ("pending_onboarding", "Pending Onboarding"),
            ("offboarding", "Completed Offboarding"),
            ("pending_offboarding", "Pending Offboarding"),
            ("rejected", "Rejected"),
        ],
        validators=[Optional()],
        default="all",
    )
    start_date = DateField("Start Date", validators=[Optional()])
    end_date = DateField("End Date", validators=[Optional()])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    transfer_status = SelectField(
        "Transfer Status",
        choices=[("", "All"), ("transferred", "Transferred"), ("not_transferred", "Not Transferred")],
        validators=[Optional()],
        default="",
    )


class AddUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=100)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6)])
    # Choices populated per-request from Role.query in the route (roles are
    # admin-editable at runtime via the Roles & Permissions screen, so a
    # static choice list here would go stale).
    role = SelectField("Role", validators=[DataRequired()], choices=[])
    name = StringField("Name", validators=[Optional(), Length(max=100)])
    designation = StringField("Designation", validators=[Optional(), Length(max=100)])
    branch_city = StringField("Branch City", validators=[Optional(), Length(max=100)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])


class EditUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=100)])
    role = SelectField("Role", validators=[DataRequired()], choices=[])
    name = StringField("Name", validators=[Optional(), Length(max=100)])
    designation = StringField("Designation", validators=[Optional(), Length(max=100)])
    branch_city = StringField("Branch City", validators=[Optional(), Length(max=100)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current Password", validators=[DataRequired()])
    new_password = PasswordField("New Password", validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("new_password", message="Passwords must match")],
    )


class AddDriverForm(FlaskForm):
    name = StringField("Full Name", validators=[DataRequired(), Length(max=100)])
    iqaama_number = StringField("Iqama Number", validators=[DataRequired(), Length(max=20)])
    iqaama_expiry = DateField("Iqama Expiry", validators=[Optional()], format="%Y-%m-%d")
    nationality = StringField("Nationality", validators=[Optional(), Length(max=100)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    absher_number = StringField("Absher Number", validators=[Optional(), Length(max=15)])
    previous_sponsor_number = StringField("Previous Sponsor Number", validators=[Optional(), Length(max=50)])
    # Fields added to mirror DMS's driver create form - see the
    # 20260827_driver_dms_parity_fields migration for what was deliberately
    # left out (sponsor_company_id, bank_accounts, language).
    dob = DateField("Date of Birth", validators=[Optional()], format="%Y-%m-%d")
    driver_profession = StringField("Profession", validators=[Optional(), Length(max=255)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    license_type = SelectField(
        "License Type",
        choices=[("", "- Select -"), ("Private", "Private"), ("Commercial", "Commercial"), ("Heavy", "Heavy"), ("Motorcycle", "Motorcycle")],
        validators=[Optional()],
        default="",
    )
    license_expiry = DateField("License Expiry", validators=[Optional()], format="%Y-%m-%d")
    passport_no = StringField("Passport Number", validators=[Optional(), Length(max=50)])
    passport_expiry_date = DateField("Passport Expiry", validators=[Optional()], format="%Y-%m-%d")
    remarks = TextAreaField("Remarks", validators=[Optional()])
    saudi_driving_license = SelectField(
        "Saudi Driving License",
        choices=[("false", "No"), ("true", "Yes")],
        validators=[Optional()],
        default="false",
    )
    issued_mobile_number = StringField("Issued Mobile", validators=[Optional(), Length(max=50)])
    issued_device_id = StringField("Issued Device ID", validators=[Optional(), Length(max=100)])
    mobile_issued = SelectField(
        "Mobile Issued",
        choices=[("false", "No"), ("true", "Yes")],
        validators=[Optional()],
        default="false",
    )
    car_details = StringField("Car Details", validators=[Optional(), Length(max=200)])
    assignment_date = DateField("Assignment Date", validators=[Optional()], format="%Y-%m-%d")
    tamm_authorized = SelectField(
        "TAMM Authorized",
        choices=[("false", "No"), ("true", "Yes")],
        validators=[Optional()],
        default="false",
    )
    transfer_fee_paid = SelectField(
        "Transfer Fee Paid",
        choices=[("false", "No"), ("true", "Yes")],
        validators=[Optional()],
        default="false",
    )
    transfer_fee_amount = StringField("Transfer Fee Amount", validators=[Optional(), Length(max=50)])
    transfer_fee_paid_at = StringField("Transfer Fee Paid At", validators=[Optional(), Length(max=50)])
    qiwa_contract_status = StringField("Qiwa Status", validators=[Optional(), Length(max=20)])
    onboarding_stage = StringField("Onboarding Stage", validators=[Optional(), Length(max=50)])


class PublicRegisterForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=100)])
    iqaama_number = StringField("Iqama Number", validators=[DataRequired(), Length(max=20)])
    absher_number = StringField("Absher Number", validators=[DataRequired(), Length(max=20)])
    iqaama_expiry_date = DateField("Iqama Expiry", validators=[Optional()], format="%Y-%m-%d")
    nationality = StringField("Nationality", validators=[Optional(), Length(max=100)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    address = TextAreaField("Address", validators=[Optional(), Length(max=1000)])
    previous_sponsor_number = StringField("Previous Sponsor Number", validators=[Optional(), Length(max=50)])
    saudi_driving_license = SelectField(
        "Saudi Driving License",
        choices=[("no", "No"), ("yes", "Yes")],
        default="no",
    )
    # Fields added to mirror DMS's driver create form (dms.speedlogi.sa/dms/drivers/create) -
    # kept Optional here (unlike DMS, which requires dob/email) since this is
    # the public self-registration entry point and the applicant hasn't been
    # screened yet; Ops Manager/HR can fill in anything missing later.
    dob = DateField("Date of Birth", validators=[Optional()], format="%Y-%m-%d")
    driver_profession = StringField("Profession", validators=[Optional(), Length(max=255)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=120)])
    license_type = SelectField(
        "License Type",
        choices=[("", "- Select -"), ("Private", "Private"), ("Commercial", "Commercial"), ("Heavy", "Heavy"), ("Motorcycle", "Motorcycle")],
        validators=[Optional()],
        default="",
    )
    license_expiry = DateField("License Expiry", validators=[Optional()], format="%Y-%m-%d")
    passport_no = StringField("Passport Number", validators=[Optional(), Length(max=50)])
    passport_expiry_date = DateField("Passport Expiry", validators=[Optional()], format="%Y-%m-%d")
    remarks = TextAreaField("Additional Notes", validators=[Optional()])
    iqama_card_upload = FileField(
        "Iqama Card",
        validators=[FileRequired(message="Iqama card upload is required."), FileAllowed(["png", "jpg", "jpeg", "pdf"], "Images or PDF only")],
    )
    driving_license_upload = FileField(
        "Driving License",
        validators=[Optional(), FileAllowed(["png", "jpg", "jpeg", "pdf"], "Images or PDF only")],
    )
    passport_upload = FileField(
        "Passport",
        validators=[Optional(), FileAllowed(["png", "jpg", "jpeg", "pdf"], "Images or PDF only")],
    )


class OpsManagerApproveForm(CSRFOnlyForm):
    ops_note = StringField("Ops Note", validators=[Optional(), Length(max=500)])
    driver_type_id = IntegerField("Driver Type", validators=[DataRequired()])
    will_provide_vehicle = SelectField(
        "Company Vehicle",
        choices=[("false", "No — driver arranges their own"), ("true", "Yes — company will provide a vehicle")],
        validators=[Optional()],
        default="false",
    )
    # Which platform (Business) this driver will work under. Required for
    # freelancer driver types since HR generates one contract per platform
    # for them (DriverTypeSettings.contract_mode == "per_business") -
    # enforced in blueprints/ops_manager/routes.py, not here, since the
    # requirement depends on the driver_type_id chosen in the same submit.
    platform_business_id = IntegerField("Platform", validators=[Optional()])


class OpsManagerRejectForm(CSRFOnlyForm):
    reject_reason = StringField("Reject Reason", validators=[Optional(), Length(max=500)])


class OpsManagerOffboardingForm(CSRFOnlyForm):
    reason = StringField("Offboarding Reason", validators=[Optional(), Length(max=500)])


class OpsCoordinatorOffboardingForm(CSRFOnlyForm):
    reason = StringField("Offboarding Reason", validators=[DataRequired(), Length(max=500)])


class OpsSupervisorApproveForm(FlaskForm):
    # Optional, not required: a driver Ops Manager didn't assign any
    # platform to (e.g. no platform work involved) has nothing to pick here.
    # blueprints/ops_supervisor/routes.py enforces "required if assigned".
    platform_ids_csv = StringField("Platform IDs", validators=[Optional()])
    mobile_issued = BooleanField("Mobile Issued")


class FleetAssignForm(FlaskForm):
    vehicle_id = IntegerField("Vehicle", validators=[DataRequired()])
    vehicle_plate = StringField("Vehicle Plate", validators=[Optional(), Length(max=50)])
    vehicle_details = StringField("Vehicle Details", validators=[Optional(), Length(max=200)])
    assignment_date = DateField("Assignment Date", validators=[DataRequired()], format="%Y-%m-%d")
    tamm_authorized = BooleanField("TAMM Authorized")


class FleetOffboardingForm(FlaskForm):
    fleet_damage_report = StringField("Damage Report", validators=[Optional(), Length(max=500)])
    fleet_damage_cost = StringField("Damage Cost", validators=[Optional(), Length(max=50)])
    car_returned = BooleanField("Car Returned")
    tamm_revoked = BooleanField("TAMM Revoked")
    finalize = BooleanField("Finalize")
    finance_cleared = BooleanField("Finance Cleared")


class FinanceApproveForm(FlaskForm):
    transfer_fee_paid = BooleanField("Transfer Fee Paid")
    transfer_fee_amount = StringField("Transfer Fee Amount", validators=[Optional(), Length(max=50)])
    transfer_fee_paid_at = StringField("Transfer Fee Paid At", validators=[Optional(), Length(max=50)])


class FinanceOffboardingClearForm(FlaskForm):
    finance_adjustments = StringField("Finance Adjustments", validators=[Optional(), Length(max=50)])
    finance_note = StringField("Finance Note", validators=[Optional(), Length(max=500)])


# -------------------------
# Admin config: onboarding workflow builder + contract templates
# -------------------------
class OnboardingStageTemplateForm(FlaskForm):
    driver_type_id = IntegerField("Driver Type ID", validators=[DataRequired()])
    sequence_order = IntegerField("Order", validators=[DataRequired()])
    stage_name = SelectField(
        "Stage",
        choices=[(s, s) for s in ONBOARDING_STAGES if s != "Rejected"],
        validators=[DataRequired()],
    )
    skip_condition_field = StringField("Skip Condition Field", validators=[Optional(), Length(max=100)])
    skip_condition_value = StringField("Skip Condition Value", validators=[Optional(), Length(max=50)])


class DriverTypeSettingsForm(FlaskForm):
    driver_type_id = IntegerField("Driver Type ID", validators=[DataRequired()])
    contract_mode = SelectField(
        "Contract Mode",
        choices=[("single", "Single fixed contract"), ("per_business", "One per assigned business")],
        validators=[DataRequired()],
    )
    requires_qiwa_contract = BooleanField("Uses Qiwa Contracts (Sponsor-only concept)")


class CompanyPresetForm(FlaskForm):
    """Editable version of what used to be fixed CONTRACT_* env vars - the
    identity/signatory defaults pre-filled into new contract and promissory
    note templates."""
    first_party_name = StringField("First Party Name", validators=[Optional(), Length(max=150)])
    first_party_name_ar = StringField("First Party Name (Arabic)", validators=[Optional(), Length(max=150)])
    first_party_label = StringField("First Party Label", validators=[Optional(), Length(max=100)])
    first_party_label_ar = StringField("First Party Label (Arabic)", validators=[Optional(), Length(max=100)])
    second_party_label = StringField("Second Party Label", validators=[Optional(), Length(max=100)])
    second_party_label_ar = StringField("Second Party Label (Arabic)", validators=[Optional(), Length(max=100)])
    company_signatory_name = StringField("Company Signatory Name", validators=[Optional(), Length(max=150)])
    company_signatory_title = StringField("Company Signatory Title", validators=[Optional(), Length(max=150)])


class ContractTemplateForm(FlaskForm):
    name = StringField("Template Name", validators=[DataRequired(), Length(max=150)])
    business_id = IntegerField("Business ID (blank = generic)", validators=[Optional()])
    driver_type_id = IntegerField("Driver Type ID (blank = any)", validators=[Optional()])
    first_party_name = StringField("First Party Name", validators=[Optional(), Length(max=150)])
    first_party_name_ar = StringField("First Party Name (Arabic)", validators=[Optional(), Length(max=150)])
    first_party_label = StringField("First Party Label", validators=[Optional(), Length(max=100)])
    first_party_label_ar = StringField("First Party Label (Arabic)", validators=[Optional(), Length(max=100)])
    second_party_label = StringField("Second Party Label", validators=[Optional(), Length(max=100)])
    second_party_label_ar = StringField("Second Party Label (Arabic)", validators=[Optional(), Length(max=100)])
    intro_content = TextAreaField("Intro / Opening Text", validators=[Optional()])
    intro_content_ar = TextAreaField("Intro / Opening Text (Arabic)", validators=[Optional()])
    body_content = TextAreaField("Contract Body", validators=[DataRequired()])
    body_content_ar = TextAreaField("Contract Body (Arabic)", validators=[Optional()])
    eligibility_content = TextAreaField("Eligibility Terms", validators=[Optional()])
    eligibility_content_ar = TextAreaField("Eligibility Terms (Arabic)", validators=[Optional()])
    general_terms_content = TextAreaField("General Terms", validators=[Optional()])
    general_terms_content_ar = TextAreaField("General Terms (Arabic)", validators=[Optional()])
    target_orders = IntegerField("Target Orders", validators=[Optional()])
    target_salary = DecimalField("Target Salary", validators=[Optional()])
    bonus_per_extra_order = DecimalField("Bonus Per Extra Order", validators=[Optional()])
    deduction_per_missing_order = DecimalField("Deduction Per Missing Order", validators=[Optional()])
    calculation_tiers_json = TextAreaField("Calculation Tiers JSON", validators=[Optional()])
    company_signatory_name = StringField("Company Signatory Name", validators=[Optional(), Length(max=150)])
    company_signatory_title = StringField("Company Signatory Title", validators=[Optional(), Length(max=150)])
    signature_notes = TextAreaField("Signature Notes", validators=[Optional()])
    signature_notes_ar = TextAreaField("Signature Notes (Arabic)", validators=[Optional()])
    is_active = BooleanField("Active", default=True)

    def validate_calculation_tiers_json(self, field):
        if not field.data:
            return

        try:
            payload = json.loads(field.data)
        except Exception as exc:
            raise ValidationError(f"Invalid JSON: {exc}") from exc

        if not isinstance(payload, list):
            raise ValidationError("Calculation tiers JSON must be a JSON array.")

        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                raise ValidationError(f"Tier #{index} must be a JSON object.")


class PromissoryNoteTemplateForm(FlaskForm):
    """Same document-template scenario as ContractTemplateForm, minus the
    fields that don't apply: promissory notes aren't per-business or
    per-driver-type (see services/contracts.py generate_promissory_note),
    and have no commission package/tiers."""
    name = StringField("Template Name", validators=[DataRequired(), Length(max=150)])
    first_party_name = StringField("First Party Name", validators=[Optional(), Length(max=150)])
    first_party_name_ar = StringField("First Party Name (Arabic)", validators=[Optional(), Length(max=150)])
    first_party_label = StringField("First Party Label", validators=[Optional(), Length(max=100)])
    first_party_label_ar = StringField("First Party Label (Arabic)", validators=[Optional(), Length(max=100)])
    second_party_label = StringField("Second Party Label", validators=[Optional(), Length(max=100)])
    second_party_label_ar = StringField("Second Party Label (Arabic)", validators=[Optional(), Length(max=100)])
    intro_content = TextAreaField("Intro / Opening Text", validators=[Optional()])
    intro_content_ar = TextAreaField("Intro / Opening Text (Arabic)", validators=[Optional()])
    body_content = TextAreaField("Statement", validators=[DataRequired()])
    body_content_ar = TextAreaField("Statement (Arabic)", validators=[Optional()])
    eligibility_content = TextAreaField("Eligibility Terms", validators=[Optional()])
    eligibility_content_ar = TextAreaField("Eligibility Terms (Arabic)", validators=[Optional()])
    general_terms_content = TextAreaField("General Terms", validators=[Optional()])
    general_terms_content_ar = TextAreaField("General Terms (Arabic)", validators=[Optional()])
    company_signatory_name = StringField("Company Signatory Name", validators=[Optional(), Length(max=150)])
    company_signatory_title = StringField("Company Signatory Title", validators=[Optional(), Length(max=150)])
    signature_notes = TextAreaField("Signature Notes", validators=[Optional()])
    signature_notes_ar = TextAreaField("Signature Notes (Arabic)", validators=[Optional()])
    is_active = BooleanField("Active", default=True)
