import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}


def is_allowed_file(filename: str, allowed_ext=ALLOWED_EXTENSIONS) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in allowed_ext


def save_upload(file_storage, dest_dir: str, filename: str) -> str:
    """Save an uploaded file to dest_dir ensuring the directory exists."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    safe_name = secure_filename(filename)
    target = Path(dest_dir) / safe_name
    file_storage.save(target)
    # Optional AV scan using external command (e.g., clamdscan)
    if os.getenv("AV_SCAN_ENABLED", "false").lower() == "true":
        cmd = os.getenv("AV_SCAN_COMMAND")
        if not cmd:
            raise ValueError("AV scan enabled but AV_SCAN_COMMAND is not set")
        proc = subprocess.run(f"{cmd} {target}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            target.unlink(missing_ok=True)
            raise ValueError("Upload failed AV scan")
    return safe_name


def safe_ext_filename(base: str, field: str, original_name: str) -> str:
    ext = Path(original_name).suffix.lower()
    return f"{base}_{field}{ext}"


def validate_upload(file_storage, max_bytes: int, allowed_ext=ALLOWED_EXTENSIONS, allowed_mime_prefixes=("image/", "application/pdf")):
    """Validate extension, MIME type, and size for an uploaded file."""
    if not file_storage or not getattr(file_storage, "filename", None):
        raise ValueError("File is required.")

    filename = file_storage.filename
    if not is_allowed_file(filename, allowed_ext):
        raise ValueError("Invalid file type. Only JPG, PNG, and PDF are allowed.")

    content_type = (getattr(file_storage, "mimetype", "") or "").lower()
    if not any(content_type.startswith(prefix) for prefix in allowed_mime_prefixes):
        raise ValueError("Invalid file content. Only images or PDF are accepted.")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > max_bytes:
        raise ValueError(f"File too large. Maximum {max_bytes // (1024 * 1024)} MB.")


def link_into_shared_storage(legacy_path: str, driver_id: int, shared_root: str, filename: str | None = None) -> str:
    """Publish an ALREADY-SAVED legacy upload into the shared driver_documents
    store without writing a second copy of the same bytes.

    Every driver document used to be written twice: once into static/uploads
    under a descriptive name, and again into the shared folder under a
    timestamped one. That wasted disk (~200 MB and climbing) and, worse, meant
    the two copies had different filenames, so anything comparing names saw one
    document as two -- measured 2026-09-01, 219 of 219 cross-system duplicates
    had mismatched names and 190 of 241 drivers showed at least one document
    twice on the DMS Shared Documents screen.

    A hard link fixes both at once: one set of bytes on disk with two names
    pointing at the same inode, and the shared copy keeps the SAME filename as
    the legacy one, so filename-based matching now agrees with content-based
    matching. Both paths live under /srv/apps (same filesystem), which is what
    makes the link possible; if that ever stops being true, or the link is
    refused for any other reason, we fall back to a real copy so the shared
    store is never left missing a document.

    Returns:  {driver_id}/{filename}  (relative path for driver_documents.file_path)
    """
    source = Path(legacy_path)
    if not source.is_file():
        raise FileNotFoundError(f"legacy upload not found: {legacy_path}")

    final_name = secure_filename(filename or source.name)
    folder = Path(shared_root) / str(driver_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / final_name

    if target.exists():
        # Same inode already published -- nothing to do. A different file under
        # the same name is replaced, matching the old overwrite behaviour.
        try:
            if target.samefile(source):
                return f"{driver_id}/{final_name}"
        except OSError:
            pass
        target.unlink(missing_ok=True)

    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)

    return f"{driver_id}/{final_name}"


def save_to_shared_storage(file_storage, driver_id: int, document_type: str, shared_root: str, filename: str | None = None) -> str:
    """Save a file to the shared driver_documents storage and return the relative path.

    Raw uploads (no meaningful name to give the file) save as
    {shared_root}/{driver_id}/{timestamp}_{type}.{ext}. System-generated
    documents (contracts, promissory notes, ...) pass an explicit `filename`
    so the file on disk reads the same human-readable name every caller
    uses, e.g. via services/contracts.py's _document_filename().
    Returns:  {driver_id}/{filename}  (relative path stored in driver_documents.file_path)
    """
    ext = Path(file_storage.filename).suffix.lower()
    if filename:
        final_name = secure_filename(filename)
        if not final_name.lower().endswith(ext):
            final_name = f"{final_name}{ext}"
    else:
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        final_name = secure_filename(f"{timestamp}_{document_type}{ext}")
    folder = Path(shared_root) / str(driver_id)
    folder.mkdir(parents=True, exist_ok=True)
    file_storage.stream.seek(0)
    file_storage.save(folder / final_name)
    return f"{driver_id}/{final_name}"
