"""Password hashing and JWT issue/verify.

bcrypt is used directly rather than through passlib: passlib 1.7.4 reads
``bcrypt.__about__`` which bcrypt 4.x removed, producing a noisy traceback on
every hash. The direct API is also a smaller surface for something this
security-sensitive.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Final

import bcrypt
import jwt
from jwt import InvalidTokenError

from app.config import Settings, get_settings

logger: Final = logging.getLogger(__name__)

# bcrypt silently truncates at 72 bytes. Rejecting longer input is safer than
# accepting a password whose tail is ignored, which would make two different
# passwords interchangeable.
_MAX_PASSWORD_BYTES: Final = 72

ACCESS_TOKEN_TYPE: Final = "access"
REFRESH_TOKEN_TYPE: Final = "refresh"


def hash_password(password: str) -> str:
    """Hash a plaintext password with a per-password salt."""
    encoded = password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {_MAX_PASSWORD_BYTES} bytes; bcrypt would "
            "silently truncate anything longer"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=12)).decode("utf-8")


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """A real bcrypt hash used to equalise login timing for unknown accounts.

    Verifying against a *valid* hash costs the same as a genuine check. A
    malformed placeholder would fail fast and leave the timing difference that
    reveals whether an email is registered. Computed once, on first use.
    """
    return hash_password("trinetra-timing-equalisation-dummy")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time password check. Never raises on malformed input."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:_MAX_PASSWORD_BYTES],
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # A corrupt or non-bcrypt hash must read as "wrong password", not as a
        # 500 that tells an attacker the account exists and is misconfigured.
        logger.warning("password verification failed: malformed stored hash")
        return False


def generate_api_key() -> tuple[str, str]:
    """Return (plaintext, hash) for a new service API key.

    The plaintext is shown to the operator once and never stored. Keys are
    hashed with SHA-256 rather than bcrypt: they are high-entropy random
    strings, so the slow-hash protection bcrypt buys for human passwords is
    wasted latency on every service request.
    """
    plaintext = f"tri_{secrets.token_urlsafe(32)}"
    return plaintext, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    settings: Settings,
    claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "iss": "trinetra",
    }
    if claims:
        payload.update(claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(
    user_id: str,
    role: str,
    department_id: str | None,
    settings: Settings | None = None,
) -> str:
    """Short-lived token carrying the authorisation facts.

    Role and department are embedded so authorisation needs no database round
    trip, which is what keeps the check cheap enough to run on every request.
    The cost is that a role change takes effect at the next token refresh.
    """
    settings = settings or get_settings()
    return _create_token(
        user_id,
        ACCESS_TOKEN_TYPE,
        timedelta(minutes=settings.access_token_ttl_minutes),
        settings,
        {"role": role, "department_id": department_id},
    )


def create_refresh_token(user_id: str, settings: Settings | None = None) -> str:
    """Long-lived token that can only mint access tokens.

    Deliberately carries no role or department: a refresh token presented as an
    access token must not authorise anything, and re-reading the user row on
    refresh is what lets a deactivated account lose access.
    """
    settings = settings or get_settings()
    return _create_token(
        user_id,
        REFRESH_TOKEN_TYPE,
        timedelta(days=settings.refresh_token_ttl_days),
        settings,
    )


def decode_token(token: str, expected_type: str, settings: Settings | None = None) -> dict[str, Any]:
    """Verify signature, expiry and token type.

    Raises:
        InvalidTokenError: signature, expiry or type mismatch.
    """
    settings = settings or get_settings()
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer="trinetra",
        options={"require": ["exp", "sub", "type"]},
    )
    if payload.get("type") != expected_type:
        raise InvalidTokenError(
            f"expected a {expected_type} token, got {payload.get('type')!r}"
        )
    return payload
