"""Non-critical, versioned batch ETA estimation."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EtaProfile:
    version: str
    fixed_seconds: float
    per_item_seconds: float
    concurrency: int
    uncertainty_ratio: float

    def __post_init__(self) -> None:
        if not self.version or self.fixed_seconds < 0 or self.per_item_seconds <= 0:
            raise ValueError("ETA profile durations are invalid")
        if self.concurrency < 1 or not 0 <= self.uncertainty_ratio <= 1:
            raise ValueError("ETA profile concurrency or uncertainty is invalid")


@dataclass(frozen=True, slots=True)
class EtaEstimate:
    low_seconds: int
    high_seconds: int
    profile_version: str
    dynamic: bool


class EtaEstimator:
    """Estimate by waves, then calibrate per-item duration with EWMA alpha 0.3."""

    def __init__(self, profile: EtaProfile, *, alpha: float = 0.3) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be within (0, 1]")
        self.profile = profile
        self._alpha = alpha
        self._ewma_seconds: float | None = None

    def observe(self, *, duration_seconds: float, terminal_count: int) -> float | None:
        if duration_seconds < 0 or terminal_count < 1:
            raise ValueError("ETA observations are invalid")
        if terminal_count < 2:
            return None
        self._ewma_seconds = (
            duration_seconds
            if self._ewma_seconds is None
            else self._alpha * duration_seconds + (1 - self._alpha) * self._ewma_seconds
        )
        return self._ewma_seconds

    def estimate(self, *, item_count: int, completed_count: int = 0) -> EtaEstimate:
        if item_count < 1 or completed_count < 0 or completed_count > item_count:
            raise ValueError("ETA item counts are invalid")
        remaining = item_count - completed_count
        per_item = self._ewma_seconds or self.profile.per_item_seconds
        waves = math.ceil(remaining / self.profile.concurrency) if remaining else 0
        center = self.profile.fixed_seconds + waves * per_item
        spread = center * self.profile.uncertainty_ratio
        return EtaEstimate(
            low_seconds=max(0, math.floor(center - spread)),
            high_seconds=max(0, math.ceil(center + spread)),
            profile_version=self.profile.version,
            dynamic=self._ewma_seconds is not None,
        )

    def restore_ewma(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("ETA EWMA must be non-negative")
        self._ewma_seconds = seconds
