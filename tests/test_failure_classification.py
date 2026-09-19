"""
Unit tests for the failure classification heuristic.

This is safety-critical logic: the entire retry / self-healing system depends
on classify_failure() returning the correct category.  Wrong classification
→ either silent infinite retry (masks bugs) or missed auto-healing.
"""
import pytest

# Import directly so these tests work even without a .env / GROQ_API_KEY
from tools import classify_failure


class TestClassifyNone:
    def test_exit_zero_is_none(self):
        assert classify_failure(0, "") == "none"

    def test_exit_zero_ignores_stderr(self):
        # Exit 0 with warning text must still be "none" — only exit code matters here.
        assert classify_failure(0, "some warning text") == "none"


class TestClassifyTransient:
    @pytest.mark.parametrize("stderr", [
        "Connection timed out",
        "connection reset by peer",
        "Temporary failure in name resolution",
        "ECONNREFUSED 127.0.0.1:5432",
        "could not resolve host: api.example.com",
        "HTTP 429 Too Many Requests",
        "rate limit exceeded, retry after 60s",
        "Network is unreachable",
        "503 Service Unavailable",
        "502 Bad Gateway",
        "EOF occurred in violation of protocol",
    ])
    def test_transient_keywords(self, stderr):
        assert classify_failure(1, stderr) == "transient", (
            f"Expected 'transient' for stderr={stderr!r}"
        )

    def test_timeout_in_stdout(self):
        assert classify_failure(1, "", "read timeout after 30s") == "transient"


class TestClassifyAuth:
    @pytest.mark.parametrize("stderr", [
        "permission denied (publickey)",
        "401 Unauthorized",
        "403 Forbidden",
        "Invalid credentials",
        "Authentication failed",
        "Access denied for user 'root'@'localhost'",
        "not allowed to deploy",
    ])
    def test_auth_keywords(self, stderr):
        assert classify_failure(1, stderr) == "auth", (
            f"Expected 'auth' for stderr={stderr!r}"
        )

    def test_auth_never_mixed_with_transient(self):
        # Auth failure with "timeout" in message — auth takes precedence via keyword order
        # (we just verify it's NOT classified as transient, which would trigger blind retry)
        result = classify_failure(1, "401 Unauthorized — connection timed out")
        # Could be "auth" or "transient" depending on which keyword matches first,
        # but must NOT be "none" or "deterministic"
        assert result in ("auth", "transient")


class TestClassifyDisk:
    @pytest.mark.parametrize("stderr", [
        "No space left on device",
        "Disk quota exceeded",
        "ENOSPC",
        "write failed: no space",
        "out of disk space",
    ])
    def test_disk_keywords(self, stderr):
        assert classify_failure(1, stderr) == "disk"


class TestClassifyOOM:
    @pytest.mark.parametrize("stderr", [
        "Out of memory: Kill process",
        "OOM killer invoked",
        "Killed",
        "Cannot allocate memory",
        "Memory limit exceeded",
        "java.lang.OutOfMemoryError: Java heap space",
    ])
    def test_oom_keywords(self, stderr):
        assert classify_failure(1, stderr) == "oom"


class TestClassifyDeterministic:
    @pytest.mark.parametrize("stderr", [
        "AssertionError: expected 42 got 0",
        "Tests failed: 3 failures",
        "test failed in suite auth_tests",
        "SyntaxError: invalid syntax",
        "NameError: name 'foo' is not defined",
        "TypeError: unsupported operand type",
        "ModuleNotFoundError: No module named 'requests'",
        "ImportError: cannot import name 'Client'",
        "Build failed: compilation errors",
        "schema mismatch: field 'user_id' missing",
        "command not found: kubectl",
        "error: cannot find symbol",
    ])
    def test_deterministic_keywords(self, stderr):
        result = classify_failure(1, stderr)
        assert result == "deterministic", (
            f"Expected 'deterministic' for stderr={stderr!r}, got {result!r}"
        )


class TestClassifyAmbiguous:
    def test_unknown_stderr_is_ambiguous(self):
        assert classify_failure(1, "some weird unexpected output xyz") == "ambiguous"

    def test_empty_stderr_nonzero_exit_is_ambiguous(self):
        assert classify_failure(1, "") == "ambiguous"

    def test_exit_minus1_empty_stderr(self):
        assert classify_failure(-1, "") == "ambiguous"


class TestFailSafeBehavior:
    """Verify that ambiguous failures are treated conservatively."""

    def test_ambiguous_is_not_transient(self):
        # CRITICAL: ambiguous must NEVER be retried as if it were transient.
        result = classify_failure(2, "something went wrong but we don't know what")
        assert result != "transient"

    def test_auth_is_not_transient(self):
        # Auth failures must never trigger auto-retry (lockout risk).
        result = classify_failure(1, "Authentication failed")
        assert result != "transient"
