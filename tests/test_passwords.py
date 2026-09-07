import bcrypt
from werkzeug.security import generate_password_hash

from utils.passwords import verify_password_hash


def test_verifies_werkzeug_password_hash():
    stored_hash = generate_password_hash("Correct Horse Battery Staple!")

    assert verify_password_hash(stored_hash, "Correct Horse Battery Staple!")
    assert not verify_password_hash(stored_hash, "wrong")


def test_verifies_laravel_bcrypt_password_hash():
    stored_hash = bcrypt.hashpw(
        b"Correct Horse Battery Staple!",
        bcrypt.gensalt(),
    ).decode("utf-8")
    laravel_hash = "$2y$" + stored_hash[4:]

    assert verify_password_hash(laravel_hash, "Correct Horse Battery Staple!")
    assert not verify_password_hash(laravel_hash, "wrong")


def test_malformed_hash_is_rejected_without_exception():
    assert not verify_password_hash("$2y$malformed", "anything")
    assert not verify_password_hash("not-a-supported-hash", "anything")
