# models.py
from extensions import db
from flask_login import UserMixin
from datetime import datetime
import secrets
import sqlalchemy as sa

# Driver ID generation settings
DRIVER_ID_PREFIX = "D"
DRIVER_ID_START = 2000

# Allowed status enums (kept as simple string sets to ease validation and migrations)
ONBOARDING_STAGES = (
    "Ops Manager",
    "HR",
    "HR Final",
    "Ops Supervisor",
    "Fleet Manager",
    "Finance",
    "Completed",
    "Rejected",
)

QIWA_CONTRACT_STATUSES = (
    "Pending",
    "Created",
    "Approved",
    "Rejected",
)

TRANSFER_STATUSES = (
    "Pending",
    "Transferred",
    "Completed",
    "Not Required",
)

OFFBOARDING_STATUSES = (
    "Requested",
    "OpsSupervisor",
    "Fleet",
    "Finance",
    "HR",
    "Completed",
    "pending_tamm",
)

# =========================
# External reference models
#
# `branches` and `driver_types` are owned/migrated by the DMS (Laravel) side
# of the shared database. DOBS never creates, alters, or writes to these
# tables — it only reads them, so only the columns DOBS actually needs are
# mapped here.
# =========================
class Branch(db.Model):
    __tablename__ = "branches"

    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<Branch {self.name}>"


class DriverType(db.Model):
    __tablename__ = "driver_types"

    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    is_freelancer = db.Column(db.Boolean, default=False)
    fields = db.Column(db.Text, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<DriverType {self.name}>"


# =========================
# User Model
# =========================
class User(db.Model, UserMixin):
    __tablename__ = "dobs_user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)  # Hash stored
    role = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    designation = db.Column(db.String(100), nullable=True)
    branch_city = db.Column(db.String(100), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    # Relationship to Offboarding requests
    offboarding_requests = db.relationship("Offboarding", back_populates="requested_by")

    # Relationship to Drivers approved by HR
    hr_approved_drivers = db.relationship("Driver", back_populates="hr_approved_by_user", foreign_keys="Driver.hr_approved_by")

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class ResetToken(db.Model):
    __tablename__ = "password_resets"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(128), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(48))
    user_id = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")

    @property
    def is_used(self):
        return self.used_at is not None

    def mark_used(self):
        self.used_at = datetime.utcnow()

    def __repr__(self):
        return f"<ResetToken user={self.user_id} used={self.is_used}>"


# =========================
# Driver Model
# =========================
class Driver(db.Model):
    __tablename__ = "drivers"
    __table_args__ = (
        db.Index("ix_drivers_onboarding_stage", "onboarding_stage"),
        db.Index("ux_drivers_driver_id", "driver_id", unique=True),
        db.Index("ux_drivers_iqaama_number", "iqaama_number", unique=True),
        db.CheckConstraint(
            "onboarding_stage IN ('Ops Manager','HR','HR Final','Ops Supervisor','Fleet Manager','Finance','Completed','Rejected')",
            name="ck_drivers_onboarding_stage",
        ),
        db.CheckConstraint(
            "qiwa_contract_status IN ('Pending','Created','Approved','Rejected')",
            name="ck_drivers_qiwa_status",
        ),
        db.CheckConstraint(
            "sponsorship_transfer_status IN ('Pending','Transferred','Completed','Not Required')",
            name="ck_drivers_transfer_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    driver_id = db.Column(db.String, unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)  # Hashed password
    name = db.Column(db.String(100), nullable=False)
    iqaama_number = db.Column(db.String(20), unique=True, nullable=False)
    iqaama_expiry = db.Column(db.Date, nullable=True)
    saudi_driving_license = db.Column(db.Boolean, default=False)
    nationality = db.Column(db.String(100), nullable=True)
    absher_number = db.Column(db.String(15), nullable=True)
    previous_sponsor_number = db.Column(db.String(50), nullable=True)
    # Fields to mirror DMS's driver create form (dms.speedlogi.sa/dms/drivers/create).
    # DOBS and DMS share the same physical `drivers` table (see
    # _sync_shared_driver_fields below) - these columns already exist there,
    # added by DMS's own migrations (create_drivers_table; most recently
    # 2026_08_23_150000_add_license_type_and_profession_to_drivers_table.php).
    # DOBS's model just never declared them. No DB-level CHECK constraint on
    # license_type deliberately: DMS only enforces that enum in its own
    # Laravel validation layer, not at the DB level - adding one here could
    # break DMS's own writes to this shared table if it ever inserts a value
    # outside what DOBS assumes.
    # sponsor_company_id (linked SponsorCompany entity) and bank_accounts
    # (repeatable bank/STC Pay sub-form) are NOT included here - DOBS has no
    # SponsorCompany/DriverBankAccount tables at all, so mirroring those two
    # would mean introducing new models/relationships, not just new columns.
    dob = db.Column(db.Date, nullable=True)
    driver_profession = db.Column(db.String(255), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    license_type = db.Column(db.String(20), nullable=True)
    license_expiry = db.Column(db.Date, nullable=True)
    passport_no = db.Column(db.String(50), nullable=True)
    passport_expiry_date = db.Column(db.Date, nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    iqama_card_upload = db.Column(db.String(200), nullable=True)
    driving_license_upload = db.Column(db.String(200), nullable=True)
    passport_upload = db.Column(db.String(200), nullable=True)
    platform = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    # Shared drivers table -- physical column added by DMS migration
    # 2026_09_07_210000_add_driver_address_and_driver_inventory.php.
    # Optional at registration/HR (company accommodation is assigned later
    # in DMS). national_address is DMS-only (not surfaced in DOBS).
    address = db.Column(db.Text, nullable=True)
    car_details = db.Column(db.String(200), nullable=True)
    assignment_date = db.Column(db.Date, nullable=True)
    onboarding_stage = db.Column(db.String(50), default="Ops Manager", nullable=False)
    qiwa_contract_created = db.Column(db.Boolean, default=False)
    company_contract_created = db.Column(db.Boolean, default=False)
    qiwa_transfer_approved = db.Column(db.Boolean, default=False)
    sponsorship_transfer_done = db.Column(db.Boolean, default=False)
    qiwa_contract_status = db.Column(db.String(20), default="Pending")
    sponsorship_transfer_status = db.Column(db.String(20), default="Pending")
    ops_manager_approved_at = db.Column(db.DateTime, nullable=True)
    hr_approved_at = db.Column(db.DateTime, nullable=True)
    ops_supervisor_approved_at = db.Column(db.DateTime, nullable=True)
    fleet_manager_approved_at = db.Column(db.DateTime, nullable=True)
    finance_approved_at = db.Column(db.DateTime, nullable=True)
    ops_manager_approved = db.Column(db.Boolean, default=False)
    # Rejection is only ever allowed at Ops Manager or HR's first pass -
    # once a driver has moved past HR, platform IDs/contracts are already
    # in motion and unwinding them goes through offboarding instead, not a
    # simple reject. rejected_by_stage records which of the two rejected
    # them, for audit/reporting.
    rejected_by_stage = db.Column(db.String(50), nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    reject_reason = db.Column(db.String(500), nullable=True)
    # Set by Ops Manager alongside driver_type_id - determines whether the
    # Fleet Manager stage is included for Freelancer/Manpower drivers (see
    # dobs_onboarding_stage_template's skip_condition_field).
    will_provide_vehicle = db.Column(db.Boolean, nullable=True)
    hr_approved_by = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), nullable=True)
    hr_approved_by_user = db.relationship("User", back_populates="hr_approved_drivers", foreign_keys=[hr_approved_by])
    platform_id = db.Column(db.String(50), nullable=True)
    mobile_issued = db.Column(db.Boolean, default=False)
    tamm_authorized = db.Column(db.Boolean, default=False)
    transfer_fee_paid = db.Column(db.Boolean, default=False)
    transfer_fee_amount = db.Column(db.Numeric(12, 2), nullable=True)
    transfer_fee_paid_at = db.Column(db.DateTime, nullable=True)
    transfer_fee_receipt = db.Column(db.String(200), nullable=True)
    issued_mobile_number = db.Column(db.String(20), nullable=True)
    issued_device_id = db.Column(db.String(100), nullable=True)
    tamm_authorization_ss = db.Column(db.String(200), nullable=True)
    sponsorship_transfer_proof = db.Column(db.String(200), nullable=True)
    driver_type_id = db.Column(db.Integer, db.ForeignKey("driver_types.id"), nullable=False, default=1)
    branch_id = db.Column(db.BigInteger, db.ForeignKey("branches.id"), nullable=True)

    driver_type = db.relationship("DriverType")
    branch = db.relationship("Branch")

    # HR Files
    company_contract_file = db.Column(db.String(200), nullable=True)
    promissory_note_file = db.Column(db.String(200), nullable=True)
    qiwa_contract_file = db.Column(db.String(200), nullable=True)

    # Offboarding / Transfer
    sponsorship_transfer_completed_at = db.Column(db.DateTime, nullable=True)
    offboarding_stage = db.Column(db.String(50), nullable=True)
    offboarding_reason = db.Column(db.Text, nullable=True)
    offboarding_requested_at = db.Column(db.DateTime, nullable=True)

    # Ops coordinator
    offboard_request = db.Column(db.Boolean, default=False)
    offboard_requested_by = db.Column(db.String(100), nullable=True)
    offboard_reason = db.Column(db.String(255), nullable=True)
    offboard_requested_at = db.Column(db.DateTime, nullable=True)

    # Relationship: Driver has many DriverBusiness assignments
    business_links = db.relationship(
        "BusinessDriver",
        back_populates="driver",
        cascade="all, delete-orphan"
    )


    # Relationship: Offboarding records
    offboarding_records = db.relationship(
        "Offboarding",
        back_populates="driver",
        cascade="all, delete-orphan"
    )

    # =========================
    # Helper methods for approval stages
    #
    # Actual stage transitions live in services/onboarding_workflow.py
    # (config-driven per driver_type). This method only records the Fleet
    # Manager approval timestamp - the caller advances the stage itself via
    # onboarding_workflow.advance().
    # =========================
    def mark_fleet_manager_approved(self):
        self.fleet_manager_approved_at = datetime.utcnow()

    def ensure_shared_driver_defaults(self, created_at: datetime | None = None):
        self._sync_shared_driver_fields(created_at=created_at or datetime.utcnow())

    def mark_completed_onboarding(self, completed_at: datetime | None = None):
        completed_at = completed_at or datetime.utcnow()
        self.finance_approved_at = self.finance_approved_at or completed_at
        self.sponsorship_transfer_completed_at = self.sponsorship_transfer_completed_at or completed_at
        self.onboarding_stage = "Completed"
        self._sync_shared_driver_fields(
            created_at=completed_at,
            onboarding_completed_at=completed_at,
            status="Active",
        )

    def _sync_shared_driver_fields(
        self,
        *,
        created_at: datetime | None = None,
        onboarding_completed_at: datetime | None = None,
        status: str | None = None,
    ):
        if not self.id:
            return

        bind = db.session.get_bind() or db.engine
        columns = {column["name"] for column in sa.inspect(bind).get_columns(self.__tablename__)}

        assignments = []
        params = {"driver_id": self.id}

        if created_at is not None and "created_at" in columns:
            assignments.append("created_at = COALESCE(created_at, :created_at)")
            params["created_at"] = created_at

        if onboarding_completed_at is not None and "onboarding_completed_at" in columns:
            assignments.append("onboarding_completed_at = COALESCE(onboarding_completed_at, :onboarding_completed_at)")
            params["onboarding_completed_at"] = onboarding_completed_at

        if status is not None and "status" in columns:
            assignments.append("status = CASE WHEN status IS NULL OR TRIM(status) = '' OR LOWER(status) = 'inactive' THEN :status ELSE status END")
            params["status"] = status

        if assignments:
            db.session.execute(
                sa.text(f"UPDATE drivers SET {', '.join(assignments)} WHERE id = :driver_id"),
                params,
            )

    def hr_files_complete(self):
        """Check if required HR files are uploaded and Qiwa contract created"""
        return bool(
            self.company_contract_file and
            self.promissory_note_file and
            self.qiwa_contract_created
        )

    def __repr__(self):
        return f"<Driver {self.name} - Stage: {self.onboarding_stage}>"


# =========================
# Driver Document Model
# =========================
class DriverDocument(db.Model):
    __tablename__ = "driver_documents"

    id            = db.Column(db.Integer, primary_key=True)
    driver_id     = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False, index=True)
    # Plain string, NOT a closed Python-side Enum -- this table is written by
    # two independent systems with two different vocabularies (DOBS writes
    # iqama/passport/visa/license/medical/contract/mobile/qiwa_contract/other;
    # DMS/Laravel additionally writes company_contract/promissory_note, and its
    # own document_type column is a free-text varchar for exactly this reason).
    # A closed db.Enum() here only validates on READ, not write, so it can
    # never actually stop a bad value from being stored -- it can only crash
    # every dashboard load that touches a driver holding one. That happened
    # for real: 20260831_add_qiwa_contract_document_type.py already widened
    # this list once for the same symptom (LookupError on qiwa_contract), and
    # it broke again 2026-09-03 the moment DMS wrote company_contract/
    # promissory_note. The real MySQL column is already varchar(100) --
    # confirmed via SHOW COLUMNS -- so this just matches the model to the
    # schema that already exists, instead of re-litigating the list forever.
    document_type = db.Column(db.String(100), nullable=False)
    file_path     = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=True)
    file_size     = db.Column(db.Integer, nullable=True)
    uploaded_from = db.Column(db.Enum("dms", "dobs", "hr"), nullable=False, default="dobs")
    uploaded_by   = db.Column(db.Integer, nullable=True)   # DOBS User.id
    notes         = db.Column(db.Text, nullable=True)
    expires_at    = db.Column(db.Date, nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    deleted_at    = db.Column(db.DateTime, nullable=True)  # soft delete

    driver = db.relationship("Driver", backref=db.backref("documents", lazy="dynamic"))

    def __repr__(self):
        return f"<DriverDocument driver={self.driver_id} type={self.document_type}>"


# =========================
# Driver ID generation hook
# =========================
def _next_driver_id(connection) -> str:
    """Generate the next driver ID using a numeric sequence with prefix.

    Falls back to DRIVER_ID_START when no records exist. Uses a MAX() query on
    the numeric portion of driver_id to avoid collisions if rows were inserted
    manually.
    """

    driver_table = Driver.__table__
    num_expr = sa.cast(sa.func.substr(driver_table.c.driver_id, 2), sa.Integer)
    stmt = sa.select(sa.func.max(num_expr)).where(driver_table.c.driver_id.isnot(None))

    # Try to lock the rowset where supported to reduce race risk in MySQL
    try:
        stmt = stmt.with_for_update()
    except Exception:
        pass

    max_num = connection.execute(stmt).scalar_one_or_none() or 0
    base = max(DRIVER_ID_START - 1, max_num)
    return f"{DRIVER_ID_PREFIX}{base + 1}"


@sa.event.listens_for(Driver, "before_insert")
def _set_driver_id(mapper, connection, target):  # pylint: disable=unused-argument
    """Assign a sequential driver_id if one was not provided."""

    if getattr(target, "driver_id", None):
        return

    target.driver_id = _next_driver_id(connection)


# =========================
# Offboarding Model
# =========================
class Offboarding(db.Model):
    __tablename__ = "offboarding"
    __table_args__ = (
        db.Index("ix_offboarding_status", "status"),
        db.CheckConstraint(
            "status IN ('Requested','OpsSupervisor','Fleet','Finance','HR','Completed','pending_tamm')",
            name="ck_offboarding_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), nullable=False)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    request_reason = db.Column(db.Text)
    status = db.Column(db.String(30), default="Requested")
    ops_manager_approved_at = db.Column(db.DateTime)

    ops_supervisor_cleared = db.Column(db.Boolean, default=False)
    ops_supervisor_cleared_at = db.Column(db.DateTime)
    ops_supervisor_note = db.Column(db.Text)
    # Penalties/fines recorded when Ops Supervisor reclaims the platform ID,
    # distinct from Fleet's damage cost and Finance's adjustments.
    ops_supervisor_penalty_amount = db.Column(db.Numeric(12, 2))
    ops_supervisor_penalty_note = db.Column(db.Text)

    fleet_cleared = db.Column(db.Boolean, default=False)
    fleet_cleared_at = db.Column(db.DateTime)
    fleet_damage_report = db.Column(db.Text)
    fleet_damage_cost = db.Column(db.Numeric(12, 2))

    finance_cleared = db.Column(db.Boolean, default=False)
    finance_cleared_at = db.Column(db.DateTime)
    finance_invoice_file = db.Column(db.String(200))
    finance_adjustments = db.Column(db.Numeric(12, 2))
    finance_note = db.Column(db.Text)

    # DMS's own running payroll balance (driver_salary_slips.closing_balance,
    # positive = company owes driver), snapshotted when Finance clears, plus
    # the computed net settlement combining it with Ops Supervisor's penalty
    # and Fleet's damage cost. dms_salary_slip_id records which DMS slip the
    # snapshot came from, so HR-finalize knows exactly what to mark settled
    # back in DMS.
    dms_salary_balance = db.Column(db.Numeric(12, 2))
    dms_salary_slip_id = db.Column(db.Integer)
    net_settlement_amount = db.Column(db.Numeric(12, 2))
    settlement_direction = db.Column(db.String(20))  # company_owes_driver | driver_owes_company | even

    hr_cleared = db.Column(db.Boolean, default=False)
    hr_cleared_at = db.Column(db.DateTime)
    hr_note = db.Column(db.Text)

    tamm_revoked = db.Column(db.Boolean, default=False)
    tamm_revoked_at = db.Column(db.DateTime)

    company_contract_cancelled = db.Column(db.Boolean, default=False)
    qiwa_contract_cancelled = db.Column(db.Boolean, default=False)
    # Settlement confirmed paid, in either direction (company->driver or
    # driver->company) - same field the HR finalize form has always used,
    # meaning broadened rather than renamed to avoid unnecessary schema churn.
    salary_paid = db.Column(db.Boolean, default=False)
    # Alternate resolution when payment hasn't happened yet: driver owes
    # company but can't pay now -> promissory note; company owes driver but
    # hasn't paid yet -> payment letter committing to pay within N days.
    promissory_note_issued = db.Column(db.Boolean, default=False)
    promissory_note_at = db.Column(db.DateTime)
    payment_letter_issued = db.Column(db.Boolean, default=False)
    payment_letter_due_days = db.Column(db.Integer)
    payment_letter_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    driver = db.relationship("Driver", back_populates="offboarding_records")
    requested_by = db.relationship("User", back_populates="offboarding_requests")

# =========================
# Business Models
# =========================

# =========================
# Business Models
# =========================
class Business(db.Model):
    __tablename__ = "businesses"

    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(255), nullable=False) 
    image = db.Column(db.Text, nullable=True)
    branch_id = db.Column(db.BigInteger, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True)

    # Relationship to BusinessIDs
    ids = db.relationship("BusinessID", back_populates="business", cascade="all, delete-orphan")
    drivers = db.relationship("BusinessDriver", back_populates="business", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Business {self.name}>"


class BusinessID(db.Model):
    __tablename__ = "business_ids"

    id = db.Column(db.BigInteger, primary_key=True)
    business_id = db.Column(db.BigInteger, db.ForeignKey("businesses.id"), nullable=False)
    value = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True)

    # Relationship back to Business
    business = db.relationship("Business", back_populates="ids")

    def __repr__(self):
        return f"<BusinessID {self.value} - Active: {self.is_active}>"


class BusinessDriver(db.Model):
    __tablename__ = "business_driver"

    business_id = db.Column(db.BigInteger, db.ForeignKey("businesses.id"), primary_key=True)
    driver_id = db.Column(db.BigInteger, db.ForeignKey("drivers.id"), primary_key=True)

    # Relationships
    driver = db.relationship("Driver", back_populates="business_links")
    business = db.relationship("Business", back_populates="drivers")

    def __repr__(self):
        return f"<BusinessDriver Driver={self.driver_id}, Business={self.business_id}>"


class DriverBusinessIDS(db.Model):
    __tablename__ = "driver_business_ids"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)

    # MUST MATCH drivers.id which is Integer
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False)

    # business_ids.id is BigInteger -> correct
    business_id_id = db.Column(db.BigInteger, db.ForeignKey("business_ids.id"), nullable=False)

    previous_driver_id = db.Column(db.BigInteger, nullable=True)

    assigned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    transferred_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

    # Relationships
    driver = db.relationship("Driver", backref="driver_business_history")


# =========================
# Fleet / Vehicle Assignment Models
# =========================
class Vehicle(db.Model):
    __tablename__ = "vehicles"

    id = db.Column(db.BigInteger, primary_key=True)
    registration_number = db.Column(db.String(255), nullable=False)
    make = db.Column(db.String(255), nullable=False)
    model = db.Column(db.String(255), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(50), nullable=False, default="available")
    branch_id = db.Column(db.Integer, nullable=True)
    current_location = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assignments = db.relationship("AssignDriver", back_populates="vehicle", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Vehicle {self.registration_number} ({self.status})>"


class AssignDriver(db.Model):
    __tablename__ = "assign_drivers"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = db.Column(db.BigInteger, db.ForeignKey("vehicles.id"), nullable=False)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False)
    assign_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(50), nullable=False, default="active")
    tam_authorization = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicle = db.relationship("Vehicle", back_populates="assignments")
    driver = db.relationship("Driver", backref="active_vehicle_assignment")

    def __repr__(self):
        return f"<AssignDriver vehicle={self.vehicle_id} driver={self.driver_id} status={self.status}>"


class AssignDriverReport(db.Model):
    __tablename__ = "assign_driver_reports"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    vehicle_id = db.Column(db.BigInteger, db.ForeignKey("vehicles.id"), nullable=False)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False)
    add_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    unassign_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), nullable=False, default="assign")

    vehicle = db.relationship("Vehicle")
    driver = db.relationship("Driver")

    def __repr__(self):
        return f"<AssignDriverReport vehicle={self.vehicle_id} driver={self.driver_id} status={self.status}>"


# =========================
# RBAC Models
#
# Additive alongside the legacy `User.role` string column. Multi-role support
# comes from UserRole; a permission is granted to a user if EITHER one of
# their roles grants it (RolePermission) OR they have an explicit
# UserPermission override with granted=True (and not revoked via
# granted=False, which takes precedence over role-derived grants).
# =========================
class Role(db.Model):
    __tablename__ = "dobs_role"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    dashboard_endpoint = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Role {self.name}>"


class Permission(db.Model):
    __tablename__ = "dobs_permission"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)

    def __repr__(self):
        return f"<Permission {self.code}>"


class RolePermission(db.Model):
    __tablename__ = "dobs_role_permission"

    role_id = db.Column(db.Integer, db.ForeignKey("dobs_role.id"), primary_key=True)
    permission_id = db.Column(db.Integer, db.ForeignKey("dobs_permission.id"), primary_key=True)

    role = db.relationship("Role", backref="role_permissions")
    permission = db.relationship("Permission")


class UserRole(db.Model):
    __tablename__ = "dobs_user_role"

    user_id = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), primary_key=True)
    role_id = db.Column(db.Integer, db.ForeignKey("dobs_role.id"), primary_key=True)

    user = db.relationship("User", backref="user_roles")
    role = db.relationship("Role")


class UserPermission(db.Model):
    __tablename__ = "dobs_user_permission"

    user_id = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), primary_key=True)
    permission_id = db.Column(db.Integer, db.ForeignKey("dobs_permission.id"), primary_key=True)
    granted = db.Column(db.Boolean, default=True, nullable=False)

    user = db.relationship("User", backref="user_permissions")
    permission = db.relationship("Permission")


class ImpersonationLog(db.Model):
    """Audit trail for SuperAdmin 'login as user'. One row per
    start/stop cycle - ended_at stays NULL while the impersonation is live."""
    __tablename__ = "dobs_impersonation_log"

    id = db.Column(db.Integer, primary_key=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("dobs_user.id"), nullable=False, index=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ended_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)

    admin_user = db.relationship("User", foreign_keys=[admin_user_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])

    def __repr__(self):
        return f"<ImpersonationLog admin={self.admin_user_id} target={self.target_user_id} ended={self.ended_at is not None}>"


# =========================
# Onboarding workflow / contract configuration
#
# Admin-editable data replacing what would otherwise be hardcoded per-type
# Python logic. New driver types, new stage sequences, and new
# platform/business contracts are rows here, not code changes.
# =========================
class OnboardingStageTemplate(db.Model):
    __tablename__ = "dobs_onboarding_stage_template"
    __table_args__ = (
        db.UniqueConstraint("driver_type_id", "sequence_order", name="ux_stage_template_type_order"),
    )

    id = db.Column(db.Integer, primary_key=True)
    driver_type_id = db.Column(db.Integer, db.ForeignKey("driver_types.id"), nullable=False)
    sequence_order = db.Column(db.Integer, nullable=False)
    stage_name = db.Column(db.String(50), nullable=False)  # must be one of ONBOARDING_STAGES
    skip_condition_field = db.Column(db.String(100), nullable=True)  # e.g. "will_provide_vehicle"
    skip_condition_value = db.Column(db.String(50), nullable=True)  # stage is skipped when driver's field == this

    driver_type = db.relationship("DriverType")

    def __repr__(self):
        return f"<OnboardingStageTemplate type={self.driver_type_id} #{self.sequence_order} {self.stage_name}>"


class CompanyPreset(db.Model):
    """Singleton row (id is always 1) holding the company-side identity/
    signatory defaults pre-filled into new contract and promissory note
    templates - editable by SuperAdmin under Workflow & Contracts instead of
    only via CONTRACT_* environment variables. Blank fields fall back to
    those env-configured defaults (see blueprints/admin/routes.py
    _get_company_preset)."""
    __tablename__ = "dobs_company_preset"

    id = db.Column(db.Integer, primary_key=True)
    first_party_name = db.Column(db.String(150), nullable=True)
    first_party_name_ar = db.Column(db.String(150), nullable=True)
    first_party_label = db.Column(db.String(100), nullable=True)
    first_party_label_ar = db.Column(db.String(100), nullable=True)
    second_party_label = db.Column(db.String(100), nullable=True)
    second_party_label_ar = db.Column(db.String(100), nullable=True)
    company_signatory_name = db.Column(db.String(150), nullable=True)
    company_signatory_title = db.Column(db.String(150), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<CompanyPreset {self.first_party_name}>"


class DriverTypeSettings(db.Model):
    __tablename__ = "dobs_driver_type_settings"

    driver_type_id = db.Column(db.Integer, db.ForeignKey("driver_types.id"), primary_key=True)
    contract_mode = db.Column(db.String(20), nullable=False, default="single")  # "single" | "per_business"
    # Qiwa (Ministry of HR platform) contracts are a company-sponsorship
    # concept - only ever relevant for Sponsor-type drivers, never
    # Freelancer/Manpower. Enforced in services/hr_service.py.
    requires_qiwa_contract = db.Column(db.Boolean, nullable=False, default=False)

    driver_type = db.relationship("DriverType")

    def __repr__(self):
        return f"<DriverTypeSettings type={self.driver_type_id} mode={self.contract_mode}>"


class ContractTemplate(db.Model):
    __tablename__ = "dobs_contract_template"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.BigInteger, db.ForeignKey("businesses.id"), nullable=True)  # null = generic
    driver_type_id = db.Column(db.Integer, db.ForeignKey("driver_types.id"), nullable=True)  # null = any type
    name = db.Column(db.String(150), nullable=False)
    body_content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    version = db.Column(db.String(20), default="1.0")
    document_kind = db.Column(db.String(40), nullable=True, default="driver_contract")
    language_mode = db.Column(db.String(20), nullable=True, default="bilingual")
    layout_type = db.Column(db.String(40), nullable=True, default="structured_contract")
    first_party_name = db.Column(db.String(150), nullable=True)
    first_party_name_ar = db.Column(db.String(150), nullable=True)
    first_party_label = db.Column(db.String(100), nullable=True)
    first_party_label_ar = db.Column(db.String(100), nullable=True)
    second_party_label = db.Column(db.String(100), nullable=True)
    second_party_label_ar = db.Column(db.String(100), nullable=True)
    intro_content = db.Column(db.Text, nullable=True)
    intro_content_ar = db.Column(db.Text, nullable=True)
    eligibility_content = db.Column(db.Text, nullable=True)
    eligibility_content_ar = db.Column(db.Text, nullable=True)
    general_terms_content = db.Column(db.Text, nullable=True)
    general_terms_content_ar = db.Column(db.Text, nullable=True)
    target_orders = db.Column(db.Integer, nullable=True)
    target_salary = db.Column(db.Numeric(12, 2), nullable=True)
    bonus_per_extra_order = db.Column(db.Numeric(12, 2), nullable=True)
    deduction_per_missing_order = db.Column(db.Numeric(12, 2), nullable=True)
    calculation_tiers_json = db.Column(db.Text, nullable=True)
    company_signatory_name = db.Column(db.String(150), nullable=True)
    company_signatory_title = db.Column(db.String(150), nullable=True)
    # body_content is nullable=False (required English body); its Arabic
    # counterpart is optional so existing rows (and the one hardcoded
    # default driver-contract sentence, translated in
    # services/contracts.py _translate_contract_sections) keep working
    # without every template needing to backfill it immediately.
    body_content_ar = db.Column(db.Text, nullable=True)
    signature_notes = db.Column(db.Text, nullable=True)
    signature_notes_ar = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    business = db.relationship("Business")
    driver_type = db.relationship("DriverType")

    def __repr__(self):
        return f"<ContractTemplate {self.name} business={self.business_id} type={self.driver_type_id}>"


class SharedGeneratedDriverContract(db.Model):
    __tablename__ = "shared_generated_driver_contracts"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    reference_number = db.Column(db.String(40), unique=True, nullable=False)
    driver_id = db.Column(db.BigInteger, db.ForeignKey("drivers.id"), nullable=False)
    template_id = db.Column(db.BigInteger, db.ForeignKey("dobs_contract_template.id"), nullable=True)
    driver_document_id = db.Column(db.BigInteger, nullable=True)
    business_id = db.Column(db.BigInteger, db.ForeignKey("businesses.id"), nullable=True)
    generated_by = db.Column(db.BigInteger, nullable=True)
    driver_type_id = db.Column(db.Integer, db.ForeignKey("driver_types.id"), nullable=True)
    generated_from_system = db.Column(db.String(20), nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    file_path = db.Column(db.String(500), nullable=False)
    original_name = db.Column(db.String(255), nullable=True)
    storage_disk = db.Column(db.String(100), nullable=False, default="shared_driver_documents")
    signed_status = db.Column(db.String(30), nullable=False, default="pending")
    signed_at = db.Column(db.DateTime, nullable=True)
    signed_copy_path = db.Column(db.String(500), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    driver = db.relationship("Driver")
    business = db.relationship("Business")
    template = db.relationship("ContractTemplate")
    driver_type = db.relationship("DriverType")

    def __repr__(self):
        return f"<SharedGeneratedDriverContract ref={self.reference_number} driver={self.driver_id}>"


# ---------------------------------------------------------------------------
# Shared with DMS (dms.speedlogi.sa). Physical tables owned by DMS's
# migrations -- these models just let DOBS read/write them.
#   driver_bank_accounts      : created 2026_08_24 / 2026_08_25 (DMS)
#   inventory_items           : created 2026_09_07_210000 (DMS)
#   driver_inventory_issuances: created 2026_09_07_210000 (DMS)
# ---------------------------------------------------------------------------

class DriverBankAccount(db.Model):
    __tablename__ = "driver_bank_accounts"

    TYPE_BANK = "bank"
    TYPE_STC_PAY = "stc_pay"

    id = db.Column(db.BigInteger, primary_key=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False, index=True)
    account_type = db.Column(db.String(20), nullable=False, default=TYPE_BANK)
    bank_name = db.Column(db.String(255), nullable=True)
    account_number = db.Column(db.String(100), nullable=True)   # IBAN / account number
    stc_pay_number = db.Column(db.String(30), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    driver = db.relationship("Driver")

    def __repr__(self):
        return f"<DriverBankAccount driver={self.driver_id} type={self.account_type}>"


class InventoryItem(db.Model):
    __tablename__ = "inventory_items"

    TYPE_SHIRT = "shirt"
    TYPE_BAG = "bag"
    TYPE_DRIVER_CARD = "driver_card"

    id = db.Column(db.BigInteger, primary_key=True)
    item_type = db.Column(db.String(20), nullable=False)
    business_id = db.Column(db.BigInteger, db.ForeignKey("businesses.id"), nullable=True)
    stock_quantity = db.Column(db.Integer, default=0, nullable=False)
    low_stock_threshold = db.Column(db.Integer, nullable=True)
    restock_on_return = db.Column(db.Boolean, default=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    business = db.relationship("Business")

    def __repr__(self):
        return f"<InventoryItem {self.item_type} business={self.business_id} stock={self.stock_quantity}>"


class DriverInventoryIssuance(db.Model):
    __tablename__ = "driver_inventory_issuances"

    SOURCE_DMS = "dms"
    SOURCE_DOBS_HR_FINAL = "dobs_hr_final"

    id = db.Column(db.BigInteger, primary_key=True)
    handover_id = db.Column(db.BigInteger, db.ForeignKey("driver_inventory_handovers.id"), nullable=True, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=True, index=True)
    inventory_item_id = db.Column(db.BigInteger, db.ForeignKey("inventory_items.id"), nullable=False)
    quantity = db.Column(db.Integer, default=1, nullable=False)
    # issued_by holds a dobs_user.id here (a users.id when written by DMS) --
    # not FK-constrained on the table, since the column is shared by both
    # apps' distinct user tables. issued_by_name is the display name captured
    # at write time so DMS can still show who issued a DOBS row.
    issued_by = db.Column(db.Integer, nullable=True)
    issued_by_name = db.Column(db.String(150), nullable=True)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    source = db.Column(db.String(20), default=SOURCE_DMS, nullable=False)
    returned_quantity = db.Column(db.Integer, default=0, nullable=False)
    returned_at = db.Column(db.DateTime, nullable=True)
    returned_by = db.Column(db.Integer, nullable=True)
    returned_by_name = db.Column(db.String(150), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    item = db.relationship("InventoryItem")
    handover = db.relationship("DriverInventoryHandover", back_populates="issuances")

    def __repr__(self):
        return f"<DriverInventoryIssuance driver={self.driver_id} item={self.inventory_item_id} qty={self.quantity}>"


class DriverInventoryHandover(db.Model):
    """One driver-kit handover event (Shirt + Bag + Driver Card together).
    Groups driver_inventory_issuances rows so they can be printed as one
    signed acknowledgement. Physical table owned by DMS migration
    2026_09_07_230000; this model just lets DOBS write to it at HR Final.
    """
    __tablename__ = "driver_inventory_handovers"

    SOURCE_DMS = "dms"
    SOURCE_DOBS_HR_FINAL = "dobs_hr_final"

    id = db.Column(db.BigInteger, primary_key=True)
    handover_number = db.Column(db.String(30), nullable=True, unique=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False)
    # dual-app: a users.id from DMS, a dobs_user.id from DOBS -- not FK'd.
    issued_by = db.Column(db.Integer, nullable=True)
    issued_by_name = db.Column(db.String(150), nullable=True)
    issued_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    source = db.Column(db.String(20), default=SOURCE_DMS, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    pdf_token = db.Column(db.String(64), nullable=True, unique=True)
    signed_at = db.Column(db.DateTime, nullable=True)
    signed_recorded_by = db.Column(db.Integer, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    driver = db.relationship("Driver")
    issuances = db.relationship("DriverInventoryIssuance", back_populates="handover")

    def __repr__(self):
        return f"<DriverInventoryHandover {self.handover_number} driver={self.driver_id}>"
