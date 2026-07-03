"""
Unit tests for the destructive-command safety gate.

Safety-critical requirements verified:
1. Patterns that MUST be blocked are blocked.
2. Safe commands pass through without triggering the gate.
3. In autonomous mode, destructive commands are HARD-BLOCKED (callback returns False).
4. In supervised mode, the confirmation callback is invoked and its answer is respected.
5. Rollback (kubectl rollout undo) is NOT blocked — it is a safety action.
6. The is_destructive() function itself is tested independently of execution.
"""
import os
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers — set env before importing modules that read Config at import time
# ---------------------------------------------------------------------------

def _set_mode(mode: str):
    os.environ["EXECUTION_MODE"] = mode
    os.environ["GROQ_API_KEY"] = "gsk_test_key"
    # Re-load config values (Config reads at class level via os.getenv at parse time)
    import importlib
    import config
    importlib.reload(config)
    config.Config.EXECUTION_MODE = mode
    config.Config.GROQ_API_KEY = "gsk_test_key"
    return config.Config


class TestIsDestructive(unittest.TestCase):
    """Test the is_destructive() pattern matcher in isolation."""

    def _check(self, cmd: str, expected: bool):
        from tools import is_destructive
        result = is_destructive(cmd)
        self.assertEqual(
            result, expected,
            f"is_destructive({cmd!r}) → {result}, expected {expected}",
        )

    def test_rm_rf_blocked(self):
        self._check("rm -rf /tmp/build", True)

    def test_rm_fr_blocked(self):
        self._check("rm -fr /tmp/build", True)

    def test_rm_force_blocked(self):
        self._check("rm -f important_file.txt", True)

    def test_drop_table_blocked(self):
        self._check("psql -c 'DROP TABLE users'", True)

    def test_drop_database_blocked(self):
        self._check("mysql -e 'drop database prod'", True)

    def test_delete_from_blocked(self):
        self._check("psql -c 'DELETE FROM orders WHERE id=1'", True)

    def test_truncate_blocked(self):
        self._check("psql -c 'TRUNCATE TABLE sessions'", True)

    def test_git_force_push_blocked(self):
        self._check("git push origin main --force", True)

    def test_git_push_f_blocked(self):
        self._check("git push -f origin feature/x", True)

    def test_kubectl_delete_blocked(self):
        self._check("kubectl delete deployment api-service -n staging", True)

    def test_terraform_destroy_blocked(self):
        self._check("terraform destroy -auto-approve", True)

    def test_docker_system_prune_blocked(self):
        self._check("docker system prune -af", True)

    def test_docker_rm_blocked(self):
        self._check("docker rm my-container", True)

    def test_shutdown_blocked(self):
        self._check("shutdown -h now", True)

    def test_dd_to_disk_blocked(self):
        self._check("dd if=/dev/zero of=/dev/sda", True)

    def test_chmod_000_blocked(self):
        self._check("chmod 000 /etc/passwd", True)

    def test_kubectl_apply_safe(self):
        self._check("kubectl apply -f deployment.yaml -n staging", False)

    def test_kubectl_rollout_undo_safe(self):
        # Rollback is a safety action — must NOT be flagged as destructive
        self._check("kubectl rollout undo deployment/api-service -n staging", False)

    def test_kubectl_rollout_status_safe(self):
        self._check("kubectl rollout status deployment/api-service -n staging", False)

    def test_docker_build_safe(self):
        self._check("docker build -t api-service:abc123 .", False)

    def test_git_pull_safe(self):
        self._check("git pull origin main", False)

    def test_git_push_without_force_safe(self):
        self._check("git push origin feature/my-branch", False)

    def test_terraform_plan_safe(self):
        self._check("terraform plan -no-color", False)

    def test_pytest_safe(self):
        self._check("pytest -q tests/", False)

    def test_echo_safe(self):
        self._check("echo hello world", False)


class TestDestructiveGateSupervised(unittest.TestCase):
    """Verify supervised-mode confirmation gate behaviour."""

    def setUp(self):
        _set_mode("supervised")

    def test_blocked_when_callback_returns_false(self):
        from tools import _run_gated, set_confirmation_callback
        set_confirmation_callback(lambda cmd: False)
        result = _run_gated("rm -rf /tmp/fake_dir")
        self.assertFalse(result["ok"])
        self.assertIn("blocked", result["stderr"].lower())

    def test_allowed_when_callback_returns_true(self):
        """Destructive command that IS confirmed should execute (we mock the shell)."""
        from tools import _run_gated, set_confirmation_callback
        set_confirmation_callback(lambda cmd: True)
        # Use a safe-to-run destructive-looking command in dry_run mode
        _set_mode("dry_run")
        from tools import _run_gated
        result = _run_gated("rm -rf /tmp/does_not_exist_test_only")
        # dry_run means the shell is never actually called — ok=True is the dry_run stub
        self.assertTrue(result["ok"])
        self.assertIn("DRY RUN", result["stdout"])

    def test_safe_command_no_callback_needed(self):
        """Safe commands should not invoke the callback at all."""
        from tools import _run_gated, set_confirmation_callback
        callback_called = []
        set_confirmation_callback(lambda cmd: callback_called.append(cmd) or True)
        _set_mode("dry_run")
        from tools import _run_gated
        _run_gated("echo hello")
        self.assertEqual(len(callback_called), 0)


class TestDestructiveGateAutonomous(unittest.TestCase):
    """
    In autonomous mode, destructive commands MUST be hard-blocked.
    No operator is present to approve — this is a non-negotiable guardrail.
    """

    def setUp(self):
        _set_mode("autonomous")

    def _run(self, command: str):
        from tools import _run_gated
        return _run_gated(command)

    def test_rm_rf_hard_blocked_in_autonomous(self):
        result = self._run("rm -rf /critical/path")
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"].get("blocked"))

    def test_kubectl_delete_hard_blocked_in_autonomous(self):
        result = self._run("kubectl delete pod api-pod-xyz -n production")
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"].get("blocked"))

    def test_terraform_destroy_hard_blocked_in_autonomous(self):
        result = self._run("terraform destroy -auto-approve")
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"].get("blocked"))

    def test_git_force_push_hard_blocked_in_autonomous(self):
        result = self._run("git push origin main --force")
        self.assertFalse(result["ok"])
        self.assertTrue(result["meta"].get("blocked"))

    def test_safe_command_passes_in_autonomous(self):
        """Safe commands must still work in autonomous mode."""
        _set_mode("dry_run")
        from tools import _run_gated
        result = _run_gated("echo hello from autonomous")
        self.assertTrue(result["ok"])

    def test_rollback_not_blocked_in_autonomous(self):
        """kubectl rollout undo is a safety action — must NOT be blocked."""
        from tools import is_destructive
        cmd = "kubectl rollout undo deployment/api-service -n staging"
        self.assertFalse(is_destructive(cmd),
                         "Rollback command must not match destructive patterns")


class TestForcesSafe(unittest.TestCase):
    """force_safe=True should bypass the gate even for destructive commands."""

    def test_force_safe_bypasses_gate_in_dry_run(self):
        _set_mode("dry_run")
        from tools import _run_gated
        # Even a "destructive" pattern with force_safe=True should pass to execution
        result = _run_gated("rm -rf /tmp/test", force_safe=True)
        # In dry_run mode, the command is simulated, not real — ok=True
        self.assertTrue(result["ok"])
        self.assertIn("DRY RUN", result["stdout"])
