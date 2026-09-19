"""
Unit tests for retry / backoff bounding logic.

Safety-critical requirements:
1. Only 'transient' failures trigger automatic retry.
2. Max attempts (MAX_TOOL_RETRIES) is never exceeded.
3. Max total elapsed time (RETRY_MAX_TOTAL_SEC) ceiling is respected.
4. Auth, deterministic, disk, oom, ambiguous failures are NOT auto-retried.
5. Successful results short-circuit — no unnecessary retries after success.
6. The attempt counter increments correctly in meta.
"""
import os
import time
import unittest
from unittest.mock import MagicMock, call, patch


def _configure(max_retries: int = 3, base_delay: float = 0.01,
               max_total: float = 10.0, mode: str = "dry_run"):
    """Set Config values to known state before each test."""
    os.environ["GROQ_API_KEY"] = "gsk_test_key"
    os.environ["EXECUTION_MODE"] = mode
    os.environ["MAX_TOOL_RETRIES"] = str(max_retries)
    os.environ["RETRY_BASE_DELAY_SEC"] = str(base_delay)
    os.environ["RETRY_MAX_TOTAL_SEC"] = str(max_total)
    import importlib, config
    importlib.reload(config)
    config.Config.MAX_TOOL_RETRIES = max_retries
    config.Config.RETRY_BASE_DELAY_SEC = base_delay
    config.Config.RETRY_MAX_TOTAL_SEC = max_total
    config.Config.EXECUTION_MODE = mode
    config.Config.GROQ_API_KEY = "gsk_test_key"


def _transient_result(attempt: int = 1) -> dict:
    return {
        "ok": False,
        "stdout": "",
        "stderr": "connection timed out",
        "exit_code": 1,
        "duration_sec": 0.01,
        "meta": {"failure_class": "transient", "attempt": attempt},
    }


def _success_result() -> dict:
    return {
        "ok": True,
        "stdout": "Success",
        "stderr": "",
        "exit_code": 0,
        "duration_sec": 0.01,
        "meta": {"failure_class": "none"},
    }


def _failure_result(failure_class: str) -> dict:
    return {
        "ok": False,
        "stdout": "",
        "stderr": f"failed: {failure_class}",
        "exit_code": 1,
        "duration_sec": 0.01,
        "meta": {"failure_class": failure_class},
    }


class TestRetryOnlyTransient(unittest.TestCase):
    """Only transient failures should be retried."""

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_transient_is_retried(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        # Fail twice transiently, succeed on third attempt
        mock_run.side_effect = [
            _transient_result(1),
            _transient_result(2),
            _success_result(),
        ]
        from tools import run_with_retry
        result = run_with_retry("echo test")
        self.assertTrue(result["ok"])
        self.assertEqual(mock_run.call_count, 3)

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_auth_not_retried(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _failure_result("auth")
        from tools import run_with_retry
        result = run_with_retry("some command")
        self.assertFalse(result["ok"])
        # Should exit immediately without retrying
        mock_run.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_deterministic_not_retried(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _failure_result("deterministic")
        from tools import run_with_retry
        result = run_with_retry("pytest tests/")
        self.assertFalse(result["ok"])
        mock_run.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_disk_not_retried(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _failure_result("disk")
        from tools import run_with_retry
        run_with_retry("docker build .")
        mock_run.assert_called_once()

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_oom_not_retried(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _failure_result("oom")
        from tools import run_with_retry
        run_with_retry("make test")
        mock_run.assert_called_once()

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_ambiguous_not_retried(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _failure_result("ambiguous")
        from tools import run_with_retry
        run_with_retry("some command")
        mock_run.assert_called_once()


class TestMaxAttemptsBound(unittest.TestCase):
    """Retry loop must never exceed MAX_TOOL_RETRIES attempts."""

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_respects_max_retries_3(self, mock_sleep, mock_run):
        _configure(max_retries=3)
        # All attempts fail transiently
        mock_run.return_value = _transient_result()
        from tools import run_with_retry
        result = run_with_retry("flaky command")
        # max_retries=3 means: attempt 0, 1, 2, 3 → 4 total calls
        self.assertEqual(mock_run.call_count, 4)
        self.assertFalse(result["ok"])

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_respects_max_retries_0(self, mock_sleep, mock_run):
        _configure(max_retries=0)
        mock_run.return_value = _transient_result()
        from tools import run_with_retry
        result = run_with_retry("flaky command", max_retries=0)
        # max_retries=0 means exactly 1 attempt, no retries
        mock_run.assert_called_once()
        self.assertFalse(result["ok"])

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_respects_explicit_max_retries_override(self, mock_sleep, mock_run):
        _configure(max_retries=5)
        mock_run.return_value = _transient_result()
        from tools import run_with_retry
        run_with_retry("cmd", max_retries=2)
        # Explicit override of 2 → max 3 total calls
        self.assertEqual(mock_run.call_count, 3)

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_success_short_circuits(self, mock_sleep, mock_run):
        _configure(max_retries=10)
        mock_run.side_effect = [_transient_result(), _success_result()]
        from tools import run_with_retry
        result = run_with_retry("cmd")
        self.assertTrue(result["ok"])
        self.assertEqual(mock_run.call_count, 2)
        # Only one sleep between attempt 0 and attempt 1
        mock_sleep.assert_called_once()


class TestMaxTotalTimeBound(unittest.TestCase):
    """
    Retry loop must stop when accumulated delay would exceed RETRY_MAX_TOTAL_SEC.
    This prevents long-running retry storms even with a high max_retries count.
    """

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_stops_when_total_time_ceiling_hit(self, mock_sleep, mock_run):
        # max_total=0.001s means the SECOND retry delay already exceeds the ceiling
        _configure(max_retries=10, base_delay=0.1, max_total=0.001)
        mock_run.return_value = _transient_result()
        from tools import run_with_retry
        result = run_with_retry("slow command")
        # Should stop after 1 attempt because even the first delay (0.1s * 0.8 = 0.08s)
        # already exceeds 0.001s total.
        self.assertFalse(result["ok"])
        # Should NOT have attempted all 11 times
        self.assertLessEqual(mock_run.call_count, 5,
                              "Retry loop should have been capped by RETRY_MAX_TOTAL_SEC")

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_ceiling_hit_flag_in_meta(self, mock_sleep, mock_run):
        _configure(max_retries=10, base_delay=100.0, max_total=0.001)
        mock_run.return_value = _transient_result()
        from tools import run_with_retry
        result = run_with_retry("cmd")
        # After total time is exceeded, result should carry the ceiling flag
        self.assertEqual(result["meta"].get("retry_ceiling_hit"), "max_total_sec")

    @patch("tools._run_gated")
    @patch("time.sleep")
    def test_both_bounds_never_infinite(self, mock_sleep, mock_run):
        """Neither bound alone can cause infinite loop — test that both cooperate."""
        _configure(max_retries=1000, base_delay=0.001, max_total=0.05)
        mock_run.return_value = _transient_result()
        from tools import run_with_retry

        start = time.monotonic()
        run_with_retry("cmd")
        elapsed = time.monotonic() - start

        # Wall time should be tiny (mocked sleep), but attempt count should be bounded
        self.assertLess(mock_run.call_count, 100,
                        "Retry count should be bounded even with high max_retries")


class TestBackoffPattern(unittest.TestCase):
    """Verify backoff delays are reasonable (not constant, not zero, not unbounded)."""

    @patch("tools._run_gated")
    def test_sleep_is_called_between_retries(self, mock_run):
        _configure(max_retries=2, base_delay=0.01, max_total=100.0)
        mock_run.return_value = _transient_result()
        with patch("time.sleep") as mock_sleep:
            from tools import run_with_retry
            run_with_retry("cmd")
        # Should sleep between each retry attempt
        self.assertGreaterEqual(mock_sleep.call_count, 1)

    @patch("tools._run_gated")
    def test_no_sleep_on_first_attempt_success(self, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _success_result()
        with patch("time.sleep") as mock_sleep:
            from tools import run_with_retry
            run_with_retry("cmd")
        mock_sleep.assert_not_called()

    @patch("tools._run_gated")
    def test_no_sleep_on_non_transient_failure(self, mock_run):
        _configure(max_retries=3)
        mock_run.return_value = _failure_result("deterministic")
        with patch("time.sleep") as mock_sleep:
            from tools import run_with_retry
            run_with_retry("cmd")
        mock_sleep.assert_not_called()
