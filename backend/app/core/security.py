"""Password hashing and session tokens.

Both use only the standard library:
  * scrypt (RFC 7914, memory-hard, OWASP-approved) for passwords
  * SHA-256 for session tokens, which are high-entropy random values, not secrets

There is no shared secret between services and no JWT to decode, so FastAPI can
verify a session with a single indexed lookup.
"""

import base64
import hashlib
import hmac
import secrets

# scrypt cost parameters. n=2**15 with r=8 needs ~32 MiB and ~100 ms per hash.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_SCHEME = "scrypt"
_SESSION_TOKEN_BYTES = 32

#: Minimum length for a newly *set* password. Sign-in deliberately does not apply this:
#: the hash is the authority on what an account may use, so refusing an old weak password
#: at the door would lock out an existing user rather than improve anything. Registration
#: is the only place a password is chosen, so it is the only place the floor applies.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    """Return a self-describing hash: scrypt$n$r$p$salt$digest."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"{_SCHEME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification. Never raises on malformed input."""
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != _SCHEME:
            return False
        candidate = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_unb64(digest)),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


def new_session_token() -> str:
    """Opaque, high-entropy cookie value."""
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Only the hash is stored, so a database dump cannot be replayed as a cookie."""
    return hashlib.sha256(token.encode()).hexdigest()
