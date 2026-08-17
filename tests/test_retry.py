import os
import unittest
from unittest.mock import patch

from app.config import Settings
from app.retry import RetryPolicy, is_transient_provider_error


class RetryPolicyTests(unittest.TestCase):
    def policy(self, **overrides):
        delays = []
        values = {
            "max_attempts": 4,
            "initial_delay_seconds": 1,
            "max_delay_seconds": 2,
            "multiplier": 2,
            "jitter_ratio": 0,
            "sleeper": delays.append,
            "random_source": lambda: 0.5,
        }
        values.update(overrides)
        return RetryPolicy(**values), delays

    def test_first_attempt_success_does_not_sleep(self):
        policy, delays = self.policy()
        calls = []
        result = policy.execute(
            lambda: calls.append(1) or "ok", retry_if=lambda _error: True,
            provider="test", operation_name="safe_operation",
        )
        self.assertEqual((result, len(calls), delays), ("ok", 1, []))

    def test_transient_failure_then_success_uses_two_attempts(self):
        policy, delays = self.policy()
        outcomes = [TimeoutError(), "ok"]

        def operation():
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        self.assertEqual(policy.execute(
            operation, retry_if=is_transient_provider_error,
            provider="test", operation_name="safe_operation",
        ), "ok")
        self.assertEqual(delays, [1])

    def test_exhaustion_is_bounded_caps_delay_and_preserves_final_exception(self):
        policy, delays = self.policy()
        errors = [TimeoutError(str(index)) for index in range(4)]

        def operation():
            raise errors.pop(0)

        with self.assertRaises(TimeoutError) as caught:
            policy.execute(
                operation, retry_if=is_transient_provider_error,
                provider="test", operation_name="safe_operation",
            )
        self.assertEqual(str(caught.exception), "3")
        self.assertEqual(delays, [1, 2, 2])

    def test_non_retryable_error_stops_immediately(self):
        policy, delays = self.policy()
        calls = []

        def operation():
            calls.append(1)
            raise ValueError("invalid application input")

        with self.assertRaises(ValueError):
            policy.execute(
                operation, retry_if=is_transient_provider_error,
                provider="test", operation_name="safe_operation",
            )
        self.assertEqual((len(calls), delays), (1, []))

    def test_injected_jitter_is_deterministic_and_never_exceeds_maximum(self):
        policy, delays = self.policy(
            initial_delay_seconds=2, max_delay_seconds=2,
            jitter_ratio=0.5, random_source=lambda: 1,
        )
        calls = []

        def operation():
            calls.append(1)
            if len(calls) < 2:
                raise TimeoutError()
            return "ok"

        policy.execute(
            operation, retry_if=is_transient_provider_error,
            provider="test", operation_name="safe_operation",
        )
        self.assertEqual(delays, [2])

    def test_policy_validation_rejects_unbounded_or_invalid_configuration(self):
        for values in (
            {"max_attempts": 0}, {"initial_delay_seconds": -1},
            {"initial_delay_seconds": 2, "max_delay_seconds": 1},
            {"multiplier": 0.5}, {"jitter_ratio": 2},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RetryPolicy(**values)

    def test_retry_and_sqlite_defaults_are_loaded_from_safe_configuration(self):
        environment = {
            "NEEDS_REVIEW_FOLDER_ID": "review",
            "ALLOWED_DRIVE_FOLDERS": "{}",
            "PROVIDER_RETRY_MAX_ATTEMPTS": "4",
            "PROVIDER_RETRY_INITIAL_DELAY_SECONDS": "0.25",
            "PROVIDER_RETRY_MAX_DELAY_SECONDS": "2",
            "PROVIDER_RETRY_MULTIPLIER": "3",
            "PROVIDER_RETRY_JITTER_RATIO": "0",
            "PROVIDER_REQUEST_TIMEOUT_SECONDS": "15",
            "SQLITE_BUSY_TIMEOUT_MS": "321",
        }
        with patch("app.config.load_dotenv"), patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(
            (settings.provider_retry_max_attempts, settings.provider_retry_initial_delay_seconds,
             settings.provider_retry_max_delay_seconds, settings.provider_retry_multiplier,
             settings.provider_retry_jitter_ratio, settings.provider_request_timeout_seconds,
             settings.sqlite_busy_timeout_ms),
            (4, 0.25, 2, 3, 0, 15, 321),
        )


if __name__ == "__main__":
    unittest.main()
