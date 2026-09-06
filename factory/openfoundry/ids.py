from __future__ import annotations

import re
import secrets
import threading
import time
import uuid

from openfoundry.errors import ValidationError

_DIGEST = re.compile(r"^([a-z][a-z0-9+._-]*):([0-9a-f]+)$")
_lock = threading.Lock()
_last = 0


def uuid7() -> uuid.UUID:
    global _last
    with _lock:
        now = int(time.time_ns() // 1_000_000)
        random_bits = secrets.randbits(74)
        candidate = (now << 74) | random_bits
        if candidate <= _last:
            candidate = _last + 1
        _last = candidate
        timestamp, rand = candidate >> 74, candidate & ((1 << 74) - 1)
    integer = (timestamp << 80) | (7 << 76)
    integer |= ((rand >> 62) & 0xFFF) << 64
    integer |= 0b10 << 62
    integer |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=integer)


def validate_uuid7(value: str | uuid.UUID) -> uuid.UUID:
    try:
        parsed = value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValidationError("invalid UUID") from exc
    if parsed.version != 7 or parsed.variant != uuid.RFC_4122:
        raise ValidationError("identifier must be an RFC UUIDv7")
    return parsed


def parse_digest(value: str) -> tuple[str, str]:
    match = _DIGEST.fullmatch(value)
    if not match:
        raise ValidationError("invalid digest; expected algorithm:lowercase-hex")
    algorithm, digest = match.groups()
    if algorithm == "sha256" and len(digest) != 64:
        raise ValidationError("a SHA-256 digest must contain 64 hexadecimal characters")
    return algorithm, digest


generate_uuid7 = uuid7
