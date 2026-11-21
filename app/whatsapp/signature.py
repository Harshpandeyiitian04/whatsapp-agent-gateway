"""Meta webhook signature: X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app_secret, raw_body)."""

import hashlib
import hmac

PREFIX = "sha256="


def sign(secret: str, raw_body: bytes) -> str:
    return PREFIX + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify(secret: str, raw_body: bytes, header: str | None) -> bool:
    """Constant-time comparison; any missing or malformed header is a failure."""
    if not header or not header.startswith(PREFIX):
        return False
    return hmac.compare_digest(sign(secret, raw_body), header)
