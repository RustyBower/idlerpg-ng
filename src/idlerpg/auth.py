"""Password hashing.

The original bot stored crypt(3) hashes, which are unsalted DES truncating the
password to eight characters. Not worth carrying forward: this uses PBKDF2 from
the standard library, so there is no dependency to add.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ITERATIONS = 240_000
ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations
    ).hex()
    return f"{ALGORITHM}${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification. Returns False on anything malformed."""
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
    except (ValueError, AttributeError):
        return False
    if algorithm != ALGORITHM:
        return False
    try:
        rounds = int(iterations)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode(), salt.encode(), rounds
    ).hex()
    return hmac.compare_digest(candidate, digest)
