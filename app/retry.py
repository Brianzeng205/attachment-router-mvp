"""Small bounded retry policy for transient provider calls only."""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar


logger = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_INITIAL_DELAY_SECONDS = 0.5
DEFAULT_MAX_DELAY_SECONDS = 5.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_JITTER_RATIO = 0.1


@dataclass(frozen=True)
class RetryPolicy:
    """A deterministic-under-injection, bounded exponential retry policy."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER
    jitter_ratio: float = DEFAULT_JITTER_RATIO
    sleeper: Callable[[float], None] = field(default=time.sleep, repr=False, compare=False)
    random_source: Callable[[], float] = field(default=random.random, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if not all(math.isfinite(value) for value in (
            self.initial_delay_seconds, self.max_delay_seconds, self.multiplier, self.jitter_ratio,
        )):
            raise ValueError("retry numeric settings must be finite")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("initial retry delay cannot exceed maximum delay")
        if self.multiplier < 1:
            raise ValueError("retry multiplier must be at least 1")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def execute(
        self, operation: Callable[[], T], *, retry_if: Callable[[Exception], bool],
        provider: str, operation_name: str,
    ) -> T:
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if attempt >= self.max_attempts or not retry_if(exc):
                    if attempt >= self.max_attempts:
                        logger.error(
                            "event=provider_retry_exhausted provider=%s operation=%s max_attempts=%d "
                            "final_error_class=%s",
                            provider, operation_name, self.max_attempts, type(exc).__name__,
                        )
                    raise
                delay = self._delay_after_failure(attempt)
                logger.warning(
                    "event=provider_retry provider=%s operation=%s attempt=%d max_attempts=%d "
                    "error_class=%s retry_delay_seconds=%.2f",
                    provider, operation_name, attempt, self.max_attempts, type(exc).__name__, delay,
                )
                self.sleeper(delay)
        raise AssertionError("retry loop exhausted without returning or raising")

    def _delay_after_failure(self, failed_attempt: int) -> float:
        base = min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (self.multiplier ** (failed_attempt - 1)),
        )
        jitter = base * self.jitter_ratio * ((2 * self.random_source()) - 1)
        return min(self.max_delay_seconds, max(0.0, base + jitter))


def is_transient_provider_error(error: Exception) -> bool:
    """Classify only transport, throttling, and selected transient server failures."""
    status = _status_code(error)
    if status == 429 or (status is not None and 500 <= status <= 599):
        return True
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    return type(error).__name__ in {
        "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
        "OverloadedError", "ConnectTimeout", "ReadTimeout", "Timeout", "ConnectionError",
        "ConnectionResetError", "RemoteDisconnected", "ServerNotFoundError",
    }


def policy_from_settings(settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.provider_retry_max_attempts,
        initial_delay_seconds=settings.provider_retry_initial_delay_seconds,
        max_delay_seconds=settings.provider_retry_max_delay_seconds,
        multiplier=settings.provider_retry_multiplier,
        jitter_ratio=settings.provider_retry_jitter_ratio,
    )


def _status_code(error: Exception) -> int | None:
    candidates = (
        getattr(error, "status_code", None),
        getattr(getattr(error, "resp", None), "status", None),
        getattr(getattr(error, "response", None), "status_code", None),
    )
    return next((value for value in candidates if type(value) is int), None)
