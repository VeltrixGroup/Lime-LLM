"""Password hashing (bcrypt) for the control plane.

bcrypt only considers the first 72 bytes of a password, so we deliberately and
consistently truncate to 72 bytes in both hash and verify — otherwise a long
password could hash here but raise on verify (or vice versa) depending on the
bcrypt build.  Signup schemas also cap the password length.
"""

from __future__ import annotations

import hashlib
import secrets

import bcrypt

_BCRYPT_MAX_BYTES = 72

#: Prefix on edge-agent tokens so they're recognizable in logs/config.
AGENT_TOKEN_PREFIX = "sga_"


def hash_password(password: str) -> str:
    """Return a bcrypt hash (utf-8 str) for ``password``."""
    digest = bcrypt.hashpw(
        password.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()
    )
    return digest.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Check ``password`` against a stored bcrypt hash; never raises."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def generate_agent_token() -> str:
    """A fresh, high-entropy edge-agent token (shown to the owner once)."""
    return AGENT_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 of an agent token for storage/lookup.

    Agent tokens are 256-bit random values, so a fast hash is sufficient
    (unlike user passwords, which need bcrypt) and lets us look a key up by
    hash on every request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
