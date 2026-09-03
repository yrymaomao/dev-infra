"""HMAC-bound Last-Event-ID cursor for resumable batch streams."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class CursorInvalid(ValueError):
    pass


class CursorExpired(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedCursor:
    sequences: dict[str, int]
    issued_at: datetime


class CursorSigner:
    def __init__(
        self,
        key: bytes,
        *,
        ttl: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(key) < 32 or ttl <= timedelta(0):
            raise ValueError("cursor signing configuration is invalid")
        self._key = key
        self._ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(
        self,
        *,
        batch_id: str,
        tenant_id: str,
        sequences: dict[str, int],
    ) -> str:
        if not batch_id or not tenant_id or any(value < 0 for value in sequences.values()):
            raise ValueError("cursor claims are invalid")
        now = self._utc_now()
        payload = json.dumps(
            {
                "v": 1,
                "b": batch_id,
                "t": tenant_id,
                "s": dict(sorted(sequences.items())),
                "iat": int(now.timestamp()),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{self._encode(payload)}.{self._encode(signature)}"

    def verify(self, token: str, *, batch_id: str, tenant_id: str) -> DecodedCursor:
        if not token or len(token) > 32768:
            raise CursorInvalid("cursor is invalid")
        try:
            payload_part, signature_part = token.split(".", 1)
            payload = self._decode(payload_part)
            supplied = self._decode(signature_part)
        except (ValueError, UnicodeError):
            raise CursorInvalid("cursor is invalid") from None
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            raise CursorInvalid("cursor is invalid")
        try:
            claims = json.loads(payload)
            issued_at = datetime.fromtimestamp(int(claims["iat"]), tz=UTC)
            sequences = {str(key): int(value) for key, value in claims["s"].items()}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OverflowError):
            raise CursorInvalid("cursor is invalid") from None
        if (
            claims.get("v") != 1
            or claims.get("b") != batch_id
            or claims.get("t") != tenant_id
            or any(value < 0 for value in sequences.values())
            or issued_at > self._utc_now() + timedelta(seconds=5)
        ):
            raise CursorInvalid("cursor is invalid")
        if self._utc_now() - issued_at > self._ttl:
            raise CursorExpired("cursor is expired")
        return DecodedCursor(sequences=sequences, issued_at=issued_at)

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("cursor clock must be timezone-aware")
        return now.astimezone(UTC)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
