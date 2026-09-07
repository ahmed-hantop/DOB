import os
import re
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session, send_from_directory, make_response
from extensions import db, mail, limiter
from models import Driver, User, DriverDocument, Branch
from services.file_storage import save_to_shared_storage, link_into_shared_storage
from utils.validation import parse_date
from flask_mail import Message
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from forms.common import PublicRegisterForm
from services.admin_service import DEFAULT_DRIVER_PASSWORD

public_bp = Blueprint("public", __name__)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}
ENGLISH_PATTERN = re.compile(r'^[A-Za-z0-9\s]+$')  # Letters, numbers, spaces

ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.pdf'}


def _upload_root() -> str:
    path = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(path, exist_ok=True)
    return path


def _public_page_response(filename: str):
    page_path = os.path.join(current_app.root_path, filename)
    if not os.path.exists(page_path):
        return None

    resp = make_response(send_from_directory(current_app.root_path, filename))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


def _resolve_next_page(default_endpoint: str) -> str:
    next_page = (request.args.get("next") or "").strip()
    if next_page.startswith("/") and not next_page.startswith("//"):
        return next_page

    if request.referrer:
        return request.referrer

    return url_for(default_endpoint)

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def validate_english(value, field_name):
    """Check if input contains only English letters/numbers."""
    if not value or not ENGLISH_PATTERN.match(value):
        flash(f"❌ {field_name} must contain only English letters and numbers.", "danger")
        return False
    return True


def _dammam_branch_id():
    branch = Branch.query.filter_by(name="Dammam").first()
    return branch.id if branch else None


def _validate_upload(file_storage, label: str) -> bool:
    if not file_storage or not file_storage.filename:
        flash(f"❌ {label} upload is required.", "danger")
        return False

    content_type = (file_storage.mimetype or "").lower()
    if not (content_type.startswith("image/") or content_type == "application/pdf"):
        flash(f"❌ Invalid file content for {label}. Only images or PDF are accepted.", "danger")
        return False

    max_len = current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)
    file_storage.stream.seek(0, os.SEEK_END)
    file_size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if file_size > max_len:
        flash(f"❌ {label} file too large. Maximum 16 MB.", "danger")
        return False

    return True


def _validate_upload(file_storage, label: str) -> bool:
    if not file_storage or not file_storage.filename:
        flash(f"{label} upload is required.", "danger")
        return False

    content_type = (file_storage.mimetype or "").lower()
    if not (content_type.startswith("image/") or content_type == "application/pdf"):
        flash(f"Invalid file content for {label}. Only images or PDF are accepted.", "danger")
        return False

    max_len = current_app.config.get("MAX_CONTENT_LENGTH", 100 * 1024 * 1024)
    file_storage.stream.seek(0, os.SEEK_END)
    file_size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if file_size > max_len:
        max_mb = int(max_len / (1024 * 1024))
        flash(f"{label} file too large. Maximum {max_mb} MB.", "danger")
        return False

    return True


def _save_upload(file_storage, upload_folder: str, safe_name: str) -> str:
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    file_name = secure_filename(f"{safe_name}.{ext}")
    file_storage.save(os.path.join(upload_folder, file_name))
    return file_name


# ------------------------
# Landing / Front page
# ------------------------
@public_bp.route("/")
def index():
    return render_template("index.html")


# ------------------------
# Serve static HTML pages
# ------------------------
@public_bp.route("/<page_name>.html")
def serve_page(page_name):
    lang = session.get("lang", "en")
    filename = f"rtl_{page_name}.html" if lang == "ar" else f"{page_name}.html"
    resp = _public_page_response(filename)
    if resp is None:
        return "Page not found", 404
    return resp


# ------------------------
# Driver registration
# ------------------------
@public_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    form = PublicRegisterForm()

    if form.validate_on_submit():
        name = form.name.data
        iqaama_number = form.iqaama_number.data
        absher_number = form.absher_number.data

        if not validate_english(name, "Name") or not validate_english(iqaama_number, "Iqama Number") or not validate_english(absher_number, "Absher Number"):
            return redirect(url_for("public.register"))

        iqaama_expiry = parse_date(form.iqaama_expiry_date.data)
        saudi_driving_license = form.saudi_driving_license.data == "yes"
        nationality = form.nationality.data
        city = form.city.data
        previous_sponsor_number = form.previous_sponsor_number.data

        # Iqama is required. A licence is required only for drivers who have
        # one, while passport is optional at registration.
        uploads = {
            "iqama": (form.iqama_card_upload.data, "Iqama card"),
        }
        if saudi_driving_license:
            uploads["license"] = (form.driving_license_upload.data, "Driving license")
        if form.passport_upload.data and form.passport_upload.data.filename:
            uploads["passport"] = (form.passport_upload.data, "Passport")
        for file_storage, label in uploads.values():
            if not _validate_upload(file_storage, label):
                return redirect(url_for("public.register"))

        upload_folder = _upload_root()
        safe_name = f"{name.replace(' ', '_').lower()}_{iqaama_number}"
        saved_filenames = {
            doc_type: _save_upload(file_storage, upload_folder, f"{safe_name}_{doc_type}")
            for doc_type, (file_storage, _label) in uploads.items()
        }

        new_driver = Driver(
            name=name,
            password=generate_password_hash(DEFAULT_DRIVER_PASSWORD),
            iqaama_number=iqaama_number,
            iqaama_expiry=iqaama_expiry,
            saudi_driving_license=saudi_driving_license,
            nationality=nationality,
            city=city,
            address=(form.address.data or None),
            absher_number=absher_number,
            previous_sponsor_number=previous_sponsor_number,
            dob=form.dob.data,
            driver_profession=form.driver_profession.data or None,
            email=form.email.data or None,
            license_type=form.license_type.data or None,
            license_expiry=form.license_expiry.data,
            passport_no=form.passport_no.data or None,
            passport_expiry_date=form.passport_expiry_date.data,
            remarks=form.remarks.data or None,
            iqama_card_upload=saved_filenames["iqama"],
            driving_license_upload=saved_filenames.get("license"),
            passport_upload=saved_filenames.get("passport"),
            branch_id=_dammam_branch_id(),
            onboarding_stage="Ops Manager",
            driver_type_id=1,
        )

        db.session.add(new_driver)
        try:
            db.session.flush()
            new_driver.ensure_shared_driver_defaults()
            db.session.commit()

            # Publish all three documents into the shared driver_documents store.
            # The files are already on disk in static/uploads (they had to be
            # saved before the Driver row existed, since the columns reference
            # them), so hard-link those rather than writing the same bytes again
            # under a second name -- see link_into_shared_storage().
            shared_root = current_app.config.get("DRIVER_DOCUMENT_PATH", upload_folder)
            for doc_type, (file_storage, _label) in uploads.items():
                try:
                    saved_name = saved_filenames.get(doc_type)
                    relative_path = link_into_shared_storage(
                        os.path.join(upload_folder, saved_name), new_driver.id, shared_root, saved_name
                    )
                    file_storage.stream.seek(0, 2)
                    doc = DriverDocument(
                        driver_id     = new_driver.id,
                        document_type = doc_type,
                        file_path     = relative_path,
                        original_name = file_storage.filename,
                        file_size     = file_storage.stream.tell(),
                        uploaded_from = "dobs",
                    )
                    db.session.add(doc)
                    db.session.commit()
                except Exception:
                    current_app.logger.exception("Publishing to shared storage failed for %s registration", doc_type)
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.exception("Public registration IntegrityError")
            msg = str(getattr(e, "orig", e))
            msg_lower = msg.lower()
            if (
                "iqaama" in msg_lower
                or "iqama" in msg_lower
                or "iqaama_number" in msg_lower
                or "ux_drivers_iqaama_number" in msg_lower
                or "drivers_iqaama_number" in msg_lower
            ) and ("duplicate" in msg_lower or "unique" in msg_lower or "key" in msg_lower):
                flash("❌ This IQAMA number already exists.", "danger")
            elif ("driver_id" in msg_lower or "ux_drivers_driver_id" in msg_lower) and ("duplicate" in msg_lower or "unique" in msg_lower or "key" in msg_lower):
                flash("❌ Driver ID already exists. Please try again.", "danger")
            else:
                flash(f"⚠️ Unexpected error occurred: {msg}", "danger")
            return redirect(url_for("public.register"))

        try:
            recipients = [u.email for u in User.query.filter(User.role.in_(["OpsManager"])).all() if u.email]
            if recipients:
                msg = Message(
                    subject="New Driver Registration Submitted | تسجيل سائق جديد",
                    recipients=recipients
                )

                msg.html = f"""
                <html>
                <body style="font-family: Arial, sans-serif; color: #333; background-color: #f8f9fa; padding: 20px;">
                    <div style="max-width: 600px; margin: auto; background: white; padding: 25px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">

                        <!-- English LTR -->
                        <div style="text-align: left;">
                            <h2 style="color: #713183;">Driver Onboarding System</h2>
                            <p>Dear Team,</p>
                            <p>A new driver has submitted a registration form. Please review:</p>
                            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                                <tr>
                                    <td style="padding: 8px; border: 1px solid #ddd;"><strong>Driver Name</strong></td>
                                    <td style="padding: 8px; border: 1px solid #ddd;">{name}</td>
                                </tr>
                            </table>
                            <p><a href="https://dobs.dobs.cloud/login">Login to the system</a></p>
                        </div>

                        <hr style="margin: 30px 0;">

                        <!-- Arabic RTL -->
                        <div dir="rtl" lang="ar" style="text-align: right; font-family: Tahoma, sans-serif;">
                            <h2 style="color: #713183;">نظام إدارة السائقين</h2>
                            <p>الفريق المحترم،</p>
                            <p>لقد قام السائق بتقديم نموذج تسجيل جديد. يرجى مراجعة التفاصيل:</p>
                            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                                <tr>
                                    <td style="padding: 8px; border: 1px solid #ddd;">اسم السائق</td>
                                    <td style="padding: 8px; border: 1px solid #ddd;">{name}</td>
                                </tr>
                            </table>
                            <p><a href="https://dobs.dobs.cloud/login">تسجيل الدخول إلى النظام</a></p>
                        </div>

                    </div>
                </body>
                </html>
                """

                mail.send(msg)

        except Exception as e:
            current_app.logger.exception("Public registration email failed")

        flash("✅ Driver data received successfully!", "success")
        return redirect(url_for("public.register"))

    if request.method == "POST":
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")
        current_app.logger.warning("Public registration validation failed", extra={"errors": form.errors})

    return render_template("register.html", form=form)


# ------------------------
# Language switch
# ------------------------
@public_bp.route("/set_language/<lang>")
def set_language(lang):
    if lang in ["en", "ar"]:
        session["lang"] = lang
    next_page = _resolve_next_page("public.index")
    return redirect(next_page)
