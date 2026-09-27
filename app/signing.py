"""HMAC-SHA256 callback signing contract (shared by the sender and the mock receiver).

    canonical = f"{timestamp}." + raw_body_bytes          # timestamp = UTC Unix seconds
    signature = "sha256=" + hex(HMAC_SHA256(secret, canonical))

    X-Callback-Timestamp: 1790503200
    X-Callback-Signature: sha256=5d41402abc4b2a76b9719d911017c592...

The body is the exact bytes sent on the wire (empty for a request without a body).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import NamedTuple

ALGORITHM = "HMAC-SHA256"
SIGNATURE_PREFIX = "sha256="
DEFAULT_TOLERANCE_SECONDS = 300


def now_ts() -> int:
    return int(time.time())


def compute_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    message = f"{timestamp}.".encode("ascii") + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


class Verification(NamedTuple):
    valid: bool
    reason: str | None = None


def verify_signature(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: int | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> Verification:
    if not signature:
        return Verification(False, "missing_signature")
    if not timestamp:
        return Verification(False, "missing_timestamp")
    try:
        ts = int(timestamp)
    except ValueError:
        return Verification(False, "invalid_timestamp")
    if timestamp != str(ts):  # canonical decimal only (no "+", padding, or non-ASCII digits)
        return Verification(False, "invalid_timestamp")
    current = now_ts() if now is None else now
    if abs(current - ts) > tolerance_seconds:
        return Verification(False, "timestamp_out_of_tolerance")
    expected = compute_signature(secret, ts, body)
    # Constant-time comparison; encode both sides so non-ASCII input cannot raise.
    if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8", errors="replace")):
        return Verification(False, "signature_mismatch")
    return Verification(True, None)
