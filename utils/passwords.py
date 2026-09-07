"""Password-hash compatibility for native DOBS and shared HR/DMS accounts."""

import bcrypt
from werkzeug.security import check_password_hash


_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def verify_password_hash(stored_hash: str, candidate_password: str) -> bool:
    """Verify Werkzeug or Laravel/PHP bcrypt hashes without raising on input."""
    if not stored_hash or candidate_password is None:
        return False

    try:
        if stored_hash.startswith(_BCRYPT_PREFIXES):
            return bcrypt.checkpw(
                candidate_password.encode("utf-8"),
                stored_hash.encode("utf-8"),
            )

        return check_password_hash(stored_hash, candidate_password)
    except (TypeError, ValueError):
        return False
