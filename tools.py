"""
Tool implementations exposed to the LLM via Groq's function-calling API.

Design rules:
- Every tool returns a uniform dict: {ok, stdout, stderr, exit_code, duration_sec, meta}
- meta.failure_class is populated by a shared heuristic so the agent loop can
  decide retry eligibility without inspecting raw stderr.
- Destructive commands are intercepted by a pattern guard and require human
  confirmation — even in autonomous mode (non-negotiable guardrail).
- Every tool is registered in TOOL_REGISTRY (name → callable) and TOOL_SCHEMAS
  (JSON schema for Groq function calling). Adding a new tool never requires
  touching agent.py.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from config import Config

# ---------------------------------------------------------------------------
# Non-negotiable destructive command patterns
# ANY shell command matching one of these requires explicit confirmation before
# execution — regardless of EXECUTION_MODE.  Fail-safe over fail-open.
# ---------------------------------------------------------------------------
DESTRUCTIVE_PATTERNS: list[str] = [
    r"\brm\s+(-\S*f\S*|-\S*r\S*){1,2}\s",   # rm -rf / rm -fr / rm -f
    r"\bdrop\s+(table|database|schema|index)\b",
    r"\bdelete\s+from\b",
    r"\btruncate\s+(table\s+)?\w",
    r"\bgit\s+(push\s+.*--force|push\s+-f\b)",
    r"\bkubectl\s+delete\b",
    r"\bterraform\s+destroy\b",
    r"\bdocker\s+system\s+prune\b",
    r"\bdocker\s+rm\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmkfs\b",
    r">\s*/dev/sd",
    r"\bdd\s+.*of=",
    r"\bchmod\s+000\b",
    r"\bdropdb\b",
    r"\bpg_drop_replication_slot\b",
]

_COMPILED_DESTRUCTIVE = [re.compile(p, re.IGNORECASE) for p in DESTRUCTIVE_PATTERNS]

# Callback set by agent.py; returns True = confirmed / proceed, False = blocked.
_CONFIRM_CALLBACK: Callable[[str], bool] | None = None


def set_confirmation_callback(fn: Callable[[str], bool]) -> None:
    global _CONFIRM_CALLBACK
    _CONFIRM_CALLBACK = fn


def is_destructive(command: str) -> bool:
    """Return True if the command matches any known-dangerous pattern."""
    return any(p.search(command) for p in _COMPILED_DESTRUCTIVE)


# ---------------------------------------------------------------------------
# Failure classification heuristic
# ---------------------------------------------------------------------------

def classify_failure(exit_code: int, stderr: str, stdout: str = "") -> str:
    """
    Classify a non-zero exit into a category that drives retry decisions.

    Returns one of:
      "none"          — exit_code == 0 (no failure)
      "transient"     — safe to retry with backoff (network, timeout, rate-limit)
      "auth"          — credentials/permissions; NEVER retry blindly
      "disk"          — storage exhausted; needs remediation, not retry
      "oom"           — memory exhausted; reduce parallelism then retry once
      "deterministic" — code/config/test failure; retry won't help
      "ambiguous"     — unknown; treat as deterministic (fail closed)
    """
    if exit_code == 0:
        return "none"

    combined = (stderr + " " + stdout).lower()

    transient_keywords = [
        "timed out", "timeout", "connection reset", "temporary failure",
        "econnrefused", "connection refused", "could not resolve host",
        "network is unreachable", "429", "rate limit", "service unavailable",
        "502 bad gateway", "503", "read timeout", "eof occurred",
    ]
    auth_keywords = [
        "permission denied", "unauthorized", "401", "403",
        "invalid credentials", "authentication failed", "access denied",
        "forbidden", "not allowed", "no permissions",
    ]
    disk_keywords = [
        "no space left", "disk quota exceeded", "enospc",
        "write failed: no space", "out of disk",
    ]
    oom_keywords = [
        "out of memory", "oom", "killed", "cannot allocate memory",
        "memory limit exceeded", "java.lang.outofmemoryerror",
    ]
    deterministic_keywords = [
        "assertionerror", "tests failed", "test failed",
        "syntaxerror", "nameerror", "typeerror", "compileerror", "build failed",
        "schema mismatch", "migration failed", "no module named",
        "modulenotfounderror", "importerror", "command not found",
        "error: cannot find", "undefined reference",
    ]
    # "assert" and "expected" need word-boundary style checks to avoid false positives
    # e.g. "unexpected" contains "expected" — check the word at boundaries only
    deterministic_word_patterns = ["assert", r"\bexpected\b"]

    if any(k in combined for k in transient_keywords):
        return "transient"
    if any(k in combined for k in auth_keywords):
        return "auth"
    if any(k in combined for k in disk_keywords):
        return "disk"
    if any(k in combined for k in oom_keywords):
        return "oom"
    if any(k in combined for k in deterministic_keywords):
        return "deterministic"
    # Word-boundary checks to avoid false positives (e.g. "unexpected" ≠ "expected")
    if any(re.search(p, combined) for p in deterministic_word_patterns):
        return "deterministic"
    return "ambiguous"


# ---------------------------------------------------------------------------
# Secret scan (gitleaks-style pattern matching)
# ---------------------------------------------------------------------------

# Patterns that look like accidentally committed secrets
_SECRET_SCAN_PATTERNS = [
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "Groq API key"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "OpenAI-style key"),
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"), "Generic API key"),
    (re.compile(r"(?i)secret\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"), "Secret value"),
    (re.compile(r"(?i)password\s*[:=]\s*['\"]?\S{8,}"), "Password"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+]{40}"), "AWS secret"),
    (re.compile(r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----"), "Private key"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub token"),
    (re.compile(r"glpat-[A-Za-z0-9\-]{20,}"), "GitLab PAT"),
    (re.compile(r"(?i)stripe[_-]?(?:secret|api)[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}"), "Stripe key"),
]


def scan_for_secrets(content: str) -> list[dict]:
    """
    Scan text for accidentally committed secrets.
    Returns a list of findings: [{pattern_name, line_number, redacted_match}]
    Never returns the actual secret value.
    """
    findings = []
    for i, line in enumerate(content.splitlines(), start=1):
        for pattern, name in _SECRET_SCAN_PATTERNS:
            if pattern.search(line):
                # Redact — never log the actual value
                redacted = pattern.sub(f"***{name} REDACTED***", line).strip()
                findings.append({
                    "pattern": name,
                    "line": i,
                    "redacted_match": redacted[:200],
                })
    return findings


# ---------------------------------------------------------------------------
# Core execution engine
# ---------------------------------------------------------------------------

def _make_result(ok: bool, stdout: str, stderr: str, exit_code: int,
                 duration_sec: float, extra_meta: dict | None = None) -> dict:
    meta: dict[str, Any] = {"failure_class": classify_failure(exit_code, stderr, stdout)}
    if extra_meta:
        meta.update(extra_meta)
    return {
        "ok": ok,
        "stdout": stdout[-8000:],
        "stderr": stderr[-8000:],
        "exit_code": exit_code,
        "duration_sec": round(duration_sec, 3),
        "meta": meta,
    }


def _run_raw(command: str, cwd: str | None = None,
             timeout: int | None = None) -> dict:
    """
    Execute a shell command unconditionally (no safety gate, no retry).
    Used internally after safety checks have already passed.
    Config is read at call time so test reloads are respected.
    """
    from config import Config as _Config  # re-import so test reloads take effect
    if _Config.EXECUTION_MODE == "dry_run":
        return _make_result(True, f"[DRY RUN] would execute: {command}", "",
                            0, 0.0, {"dry_run": True})

    effective_timeout = timeout or _Config.DEFAULT_COMMAND_TIMEOUT_SEC
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command, shell=True, cwd=cwd,
            timeout=effective_timeout,
            capture_output=True, text=True,
        )
        dur = time.monotonic() - start
        return _make_result(
            proc.returncode == 0,
            proc.stdout, proc.stderr,
            proc.returncode, dur,
        )
    except subprocess.TimeoutExpired:
        dur = time.monotonic() - start
        return _make_result(
            False, "", f"Command timed out after {effective_timeout}s",
            -1, dur, {"failure_class": "transient"},
        )
    except Exception as exc:  # noqa: BLE001
        dur = time.monotonic() - start
        return _make_result(False, "", str(exc), -1, dur)


def _run_gated(command: str, cwd: str | None = None,
               timeout: int | None = None,
               force_safe: bool = False) -> dict:
    """
    Safety-gated execution.  If the command matches a destructive pattern:
      - In supervised mode: invoke the confirmation callback (CLI prompt or test stub).
      - In autonomous mode: HARD BLOCK (no operator is present to approve).
      - force_safe=True: bypass the gate (used for rollback — a safety action).
    Returns immediately with ok=False if blocked.

    NOTE: Config.EXECUTION_MODE is read at call time (not import time) so that
    tests which reload Config between calls see the correct mode.
    """
    from config import Config as _Config  # re-import so test reloads take effect
    if not force_safe and is_destructive(command):
        if _Config.EXECUTION_MODE == "autonomous":
            # Non-negotiable guardrail: block in autonomous mode — no approval available.
            msg = (
                f"AUTONOMOUS MODE: Destructive command hard-blocked. "
                f"Command requires explicit operator approval: {command[:200]}"
            )
            if _CONFIRM_CALLBACK:
                _CONFIRM_CALLBACK(command)
            return _make_result(False, "", msg, -1, 0.0,
                                {"blocked": True, "failure_class": "deterministic"})

        granted = _CONFIRM_CALLBACK(command) if _CONFIRM_CALLBACK else False
        if not granted:
            return _make_result(
                False, "", "Destructive command blocked: operator did not confirm.",
                -1, 0.0, {"blocked": True, "failure_class": "deterministic"},
            )

    return _run_raw(command, cwd=cwd, timeout=timeout)


def run_with_retry(command: str, cwd: str | None = None,
                   timeout: int | None = None,
                   max_retries: int | None = None,
                   force_safe: bool = False) -> dict:
    """
    Run a command, auto-retrying only transient failures with exponential backoff.

    Bounded by BOTH max_retries (count) AND RETRY_MAX_TOTAL_SEC (wall time) —
    whichever is hit first.  Never retries auth/deterministic failures.
    """
    from config import Config as _Config  # re-import so test module reloads take effect
    max_r = max_retries if max_retries is not None else _Config.MAX_TOOL_RETRIES
    attempt = 0
    total_elapsed = 0.0
    last: dict = {}

    while attempt <= max_r:
        result = _run_gated(command, cwd=cwd, timeout=timeout, force_safe=force_safe)
        result["meta"]["attempt"] = attempt + 1
        last = result

        if result["ok"]:
            return result

        fclass = result["meta"].get("failure_class", "ambiguous")
        # Only transient failures are retried automatically.
        if fclass != "transient" or attempt >= max_r:
            return result

        delay = _Config.RETRY_BASE_DELAY_SEC * (2 ** attempt)
        # Add jitter: ±20% of delay
        import random
        delay = delay * (0.8 + 0.4 * random.random())
        total_elapsed += delay
        if total_elapsed > _Config.RETRY_MAX_TOTAL_SEC:
            result["meta"]["retry_ceiling_hit"] = "max_total_sec"
            return result

        time.sleep(min(delay, 10))  # cap sleep to 10s for interactive use
        attempt += 1

    return last


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------

def tool_run_shell(command: str, cwd: str | None = None,
                   timeout: int | None = None, allow_retry: bool = True) -> dict:
    """General-purpose shell executor with optional auto-retry for transient failures."""
    if allow_retry:
        return run_with_retry(command, cwd=cwd, timeout=timeout)
    return _run_gated(command, cwd=cwd, timeout=timeout)


def tool_run_tests(test_command: str, cwd: str | None = None,
                   timeout: int | None = None) -> dict:
    """
    Run a test suite.  Test failures are deterministic — not auto-retried.
    A flaky-test retry must be triggered explicitly by the agent with the
    classification logged as 'flaky' in the final report.
    """
    result = _run_gated(test_command, cwd=cwd, timeout=timeout or 300)
    result["meta"]["is_test"] = True
    return result


def tool_git(subcommand: str, cwd: str | None = None) -> dict:
    """Run a git subcommand.  Force-push and similar ops are caught by the destructive gate."""
    return run_with_retry(f"git {subcommand}", cwd=cwd)


def tool_docker_build(tag: str, dockerfile: str = "Dockerfile",
                      context: str = ".") -> dict:
    """Build a Docker image with a pinned tag.  Never use :latest beyond dev."""
    cmd = (
        f"docker build "
        f"-f {shlex.quote(dockerfile)} "
        f"-t {shlex.quote(tag)} "
        f"{shlex.quote(context)}"
    )
    return run_with_retry(cmd, timeout=600)


def tool_docker_push(tag: str) -> dict:
    """Push a Docker image to its registry."""
    return run_with_retry(f"docker push {shlex.quote(tag)}", timeout=600)


def tool_kubectl_apply(manifest_path: str, namespace: str = "default",
                       dry_run: bool = False) -> dict:
    """Apply a Kubernetes manifest.  Use dry_run=true to validate before a real apply."""
    flag = " --dry-run=server" if dry_run else ""
    cmd = (
        f"kubectl apply -f {shlex.quote(manifest_path)} "
        f"-n {shlex.quote(namespace)}{flag}"
    )
    return run_with_retry(cmd)


def tool_kubectl_rollout_status(deployment: str, namespace: str = "default",
                                timeout: int = 180) -> dict:
    """Block until a rollout completes or times out.  Primary k8s post-deploy signal."""
    cmd = (
        f"kubectl rollout status deployment/{shlex.quote(deployment)} "
        f"-n {shlex.quote(namespace)} --timeout={timeout}s"
    )
    return _run_raw(cmd, timeout=timeout + 15)  # no blind retry — IS the health signal


def tool_kubectl_rollback(deployment: str, namespace: str = "default") -> dict:
    """
    Roll back a deployment to its previous revision.
    This is a SAFETY action — force_safe=True bypasses the destructive gate
    because this is the remediation mechanism, not an attack.
    """
    cmd = (
        f"kubectl rollout undo deployment/{shlex.quote(deployment)} "
        f"-n {shlex.quote(namespace)}"
    )
    return _run_raw(cmd)  # bypass gate: rollback is safety, not destructive


def tool_terraform_plan(directory: str = ".") -> dict:
    """Run terraform plan (read-only preview).  Always run before terraform_apply."""
    return run_with_retry("terraform plan -no-color -detailed-exitcode",
                          cwd=directory, timeout=300)


def tool_terraform_apply(directory: str = ".", auto_approve: bool = False) -> dict:
    """
    Apply terraform changes.  Without auto_approve, terraform will prompt interactively
    (or use -auto-approve only after a clean plan review).
    The destructive gate catches destroy-containing plans.
    """
    flag = " -auto-approve" if auto_approve else ""
    return _run_gated(f"terraform apply -no-color{flag}",
                      cwd=directory, timeout=600)


def tool_health_check(url: str, timeout: int = 10,
                      expected_status: int = 200) -> dict:
    """HTTP GET a health/version endpoint and verify the response status."""
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "devops-agent/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4000).decode(errors="replace")
            ok = resp.status == expected_status
            dur = time.monotonic() - start
            return _make_result(ok, body, "", resp.status, dur,
                                {"status_code": resp.status, "url": url})
    except urllib.error.HTTPError as exc:
        dur = time.monotonic() - start
        return _make_result(False, "", str(exc), exc.code, dur,
                            {"failure_class": "deterministic", "url": url})
    except Exception as exc:  # noqa: BLE001
        dur = time.monotonic() - start
        return _make_result(False, "", str(exc), -1, dur,
                            {"failure_class": "transient", "url": url})


def tool_secret_scan(path: str) -> dict:
    """
    Scan a file or directory diff for accidentally committed secrets.
    Never prints actual secret values — only redacted matches.
    """
    import os
    findings: list[dict] = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            findings = scan_for_secrets(content)
        except OSError as exc:
            return _make_result(False, "", str(exc), -1, 0.0)
    elif os.path.isdir(path):
        for root, _dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        content = f.read(1_000_000)  # 1 MB cap per file
                    for hit in scan_for_secrets(content):
                        hit["file"] = fpath
                        findings.append(hit)
                except OSError:
                    pass
    else:
        return _make_result(False, "", f"Path not found: {path}", 1, 0.0)

    clean = len(findings) == 0
    summary = f"{len(findings)} potential secret(s) found" if findings else "No secrets detected"
    return _make_result(clean, summary, "", 0 if clean else 1, 0.0,
                        {"findings": findings, "scanned_path": path})


def tool_check_disk(path: str = ".") -> dict:
    """Check available disk space at the given path."""
    import shutil
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        pct_used = usage.used / usage.total * 100
        summary = f"Disk: {free_gb:.1f} GB free of {total_gb:.1f} GB ({pct_used:.0f}% used)"
        ok = free_gb > 1.0  # warn if < 1 GB free
        return _make_result(ok, summary, "" if ok else "Low disk space warning",
                            0 if ok else 1, 0.0,
                            {"free_gb": round(free_gb, 2), "pct_used": round(pct_used, 1)})
    except Exception as exc:  # noqa: BLE001
        return _make_result(False, "", str(exc), -1, 0.0)


# ---------------------------------------------------------------------------
# Tool registry + JSON schemas for Groq function calling
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    "run_shell": tool_run_shell,
    "run_tests": tool_run_tests,
    "git": tool_git,
    "docker_build": tool_docker_build,
    "docker_push": tool_docker_push,
    "kubectl_apply": tool_kubectl_apply,
    "kubectl_rollout_status": tool_kubectl_rollout_status,
    "kubectl_rollback": tool_kubectl_rollback,
    "terraform_plan": tool_terraform_plan,
    "terraform_apply": tool_terraform_apply,
    "health_check": tool_health_check,
    "secret_scan": tool_secret_scan,
    "check_disk": tool_check_disk,
}

TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "run_shell",
        "description": (
            "Run an arbitrary shell command. Transient failures are auto-retried with "
            "exponential backoff. Destructive commands (rm -rf, kubectl delete, "
            "terraform destroy, force-push, DROP TABLE, etc.) always require operator "
            "confirmation — even in autonomous mode."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (optional)"},
                "allow_retry": {"type": "boolean",
                                "description": "Auto-retry transient failures (default true)"},
            },
            "required": ["command"],
        },
    }},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": (
            "Run a test suite (unit/integration/smoke/e2e/lint/contract). "
            "Failures are treated as deterministic evidence — not blindly retried. "
            "Use this rather than run_shell for test commands so failures are "
            "correctly classified and logged."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "test_command": {"type": "string",
                                 "description": "e.g. 'pytest -q', 'npm test', 'go test ./...'"},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["test_command"],
        },
    }},
    {"type": "function", "function": {
        "name": "git",
        "description": (
            "Run a git subcommand, e.g. 'checkout main', 'pull', 'log -1 --oneline', "
            "'tag v1.2.3'. Force-push is caught by the destructive-action gate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subcommand": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["subcommand"],
        },
    }},
    {"type": "function", "function": {
        "name": "docker_build",
        "description": (
            "Build a Docker image. Tag must be unambiguous (service:git-sha or :version). "
            "Never use :latest for anything promoted past dev."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "dockerfile": {"type": "string"},
                "context": {"type": "string"},
            },
            "required": ["tag"],
        },
    }},
    {"type": "function", "function": {
        "name": "docker_push",
        "description": "Push a built Docker image to its registry.",
        "parameters": {
            "type": "object",
            "properties": {"tag": {"type": "string"}},
            "required": ["tag"],
        },
    }},
    {"type": "function", "function": {
        "name": "kubectl_apply",
        "description": (
            "Apply a Kubernetes manifest. Pass dry_run=true first to validate. "
            "Use real apply only after dry_run succeeds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "manifest_path": {"type": "string"},
                "namespace": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["manifest_path"],
        },
    }},
    {"type": "function", "function": {
        "name": "kubectl_rollout_status",
        "description": (
            "Block until a deployment rollout completes or times out. "
            "Primary post-deploy health signal for k8s. "
            "Call this after every kubectl_apply to confirm the deploy actually took effect."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "namespace": {"type": "string"},
                "timeout": {"type": "integer"},
            },
            "required": ["deployment"],
        },
    }},
    {"type": "function", "function": {
        "name": "kubectl_rollback",
        "description": (
            "Roll back a Kubernetes deployment to its previous revision. "
            "This is a SAFETY action — call immediately when rollback trigger conditions "
            "are met. No confirmation required."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "deployment": {"type": "string"},
                "namespace": {"type": "string"},
            },
            "required": ["deployment"],
        },
    }},
    {"type": "function", "function": {
        "name": "terraform_plan",
        "description": (
            "Run terraform plan (read-only, drift detection). "
            "ALWAYS run before terraform_apply. Halt on unexpected drift."
        ),
        "parameters": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
            "required": [],
        },
    }},
    {"type": "function", "function": {
        "name": "terraform_apply",
        "description": (
            "Apply terraform changes. auto_approve=true only after a clean plan review. "
            "Destructions in the plan trigger the confirmation gate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {"type": "string"},
                "auto_approve": {"type": "boolean"},
            },
            "required": [],
        },
    }},
    {"type": "function", "function": {
        "name": "health_check",
        "description": (
            "HTTP GET a health/version/readiness endpoint and verify the response code. "
            "Use for smoke tests and post-deploy verification. "
            "A 200 response body that includes the expected version is stronger evidence "
            "than exit code 0 alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {"type": "integer"},
                "expected_status": {"type": "integer"},
            },
            "required": ["url"],
        },
    }},
    {"type": "function", "function": {
        "name": "secret_scan",
        "description": (
            "Scan a file or directory for accidentally committed secrets before deploying. "
            "Never prints actual secret values. Returns findings with redacted context. "
            "Halt deployment if findings are returned."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "File path or directory to scan"},
            },
            "required": ["path"],
        },
    }},
    {"type": "function", "function": {
        "name": "check_disk",
        "description": (
            "Check available disk space. Use during pre-flight checks and before builds. "
            "Returns ok=false if free space < 1 GB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Path to check (default '.')"},
            },
            "required": [],
        },
    }},
]
