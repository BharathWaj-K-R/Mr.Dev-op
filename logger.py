"""
Structured, append-only audit log for every agent action.

Writes JSONL (one JSON object per line) to deployment_logs/<run-id>.jsonl.
Every record has a consistent shape so the file is greppable with:
  jq 'select(.type == "tool_call" and .tool == "kubectl_rollback")' <run>.jsonl

Design choices:
- Never raises on write failure; falls back to stderr so log loss doesn't
  mask deployment outcomes.
- Sensitive values (secrets, tokens) must NOT be passed through here — callers
  are responsible for redacting before logging.  This class will scrub common
  patterns as a second line of defence.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from config import Config

# Patterns that look like secrets — redact values that match these.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|password|token|passwd|credential)[\"']?\s*[:=]\s*[\"']?(\S+)", re.I),
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),         # Groq API key prefix
    re.compile(r"sk-[A-Za-z0-9]{20,}"),           # OpenAI-style key prefix
    re.compile(r"[A-Za-z0-9/+]{40,}={0,2}"),     # base64 blobs that might be creds
]


def _redact(text: str) -> str:
    """Very conservative secret scrubber — used as a last-resort defence in logs."""
    if not isinstance(text, str):
        return text
    for pat in _SECRET_PATTERNS:
        # Replace the full match or just the captured group (group 2 if present)
        def _replace(m: re.Match) -> str:  # noqa: B023
            groups = m.groups()
            if len(groups) >= 2:
                return m.group(0).replace(groups[1], "***REDACTED***")
            return "***REDACTED***"
        text = pat.sub(_replace, text)
    return text


def _safe_truncate(obj: Any, max_len: int = 4096) -> Any:
    """Truncate long strings inside dicts/lists so logs don't balloon."""
    if isinstance(obj, str):
        return obj[:max_len] + ("…[truncated]" if len(obj) > max_len else "")
    if isinstance(obj, dict):
        return {k: _safe_truncate(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_truncate(v, max_len) for v in obj]
    return obj


class DeploymentLogger:
    def __init__(self, run_id: str | None = None, metadata: dict | None = None):
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        self.run_id = run_id or datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
        self.path = os.path.join(Config.LOG_DIR, f"{self.run_id}.jsonl")
        self._metadata = metadata or {}
        # Write a header record so every log file is self-describing.
        self._write({
            "type": "run_start",
            "run_id": self.run_id,
            "config": Config.as_dict(),
            "metadata": self._metadata,
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, record: dict) -> None:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        record.setdefault("run_id", self.run_id)
        # Redact secrets as a defensive measure.
        try:
            line = json.dumps(_safe_truncate(record))
        except (TypeError, ValueError):
            line = json.dumps({"type": "log_error", "raw": repr(record)[:500]})
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            # Logging failure must not mask a real deployment result.
            import sys
            print(f"[LOGGER WARN] Could not write to {self.path}: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stage(self, name: str, status: str, detail: str = "") -> None:
        """Record a lifecycle stage transition (plan, build, test, deploy, etc.)."""
        self._write({"type": "stage", "stage": name, "status": status, "detail": detail[:500]})
        badge = {"in_progress": "▶", "success": "✅", "failed": "❌", "skipped": "⏭"}.get(status, "·")
        print(f"[STAGE] {badge} {name} → {status}" + (f"  {detail}" if detail else ""))

    def tool_call(self, tool: str, args: dict, result: dict, attempt: int = 1) -> None:
        """Record a single tool invocation with its arguments and result."""
        # Never log the raw command if it could contain embedded secrets.
        safe_args = _safe_truncate(args)
        safe_result = _safe_truncate(result)
        self._write({
            "type": "tool_call",
            "tool": tool,
            "args": safe_args,
            "attempt": attempt,
            "result": {
                "ok": safe_result.get("ok"),
                "exit_code": safe_result.get("exit_code"),
                "duration_sec": safe_result.get("duration_sec"),
                "stdout": _redact(str(safe_result.get("stdout", ""))),
                "stderr": _redact(str(safe_result.get("stderr", ""))),
                "meta": safe_result.get("meta", {}),
            },
        })

    def remediation(self, reason: str, action: str, outcome: str) -> None:
        """Record an automatic self-healing action."""
        self._write({"type": "remediation", "reason": reason, "action": action, "outcome": outcome})
        print(f"[REMEDIATE] reason={reason!r}  action={action!r}  outcome={outcome!r}")

    def rollback(self, reason: str, mechanism: str, outcome: str) -> None:
        """Record a rollback event (trigger, mechanism, and whether it confirmed healthy)."""
        self._write({"type": "rollback", "reason": reason, "mechanism": mechanism, "outcome": outcome})
        print(f"[ROLLBACK] ⚠️  reason={reason!r}  mechanism={mechanism!r}  outcome={outcome!r}")

    def confirmation_request(self, action: str, granted: bool) -> None:
        """Record a destructive-action confirmation request and its outcome."""
        self._write({"type": "confirmation", "action": action[:500], "granted": granted})
        verdict = "GRANTED ✅" if granted else "BLOCKED ❌"
        print(f"[CONFIRM] {verdict}  action={action[:120]!r}")

    def security_event(self, event_type: str, detail: str) -> None:
        """Record security-relevant findings (secret leak scan, drift detection, etc.)."""
        self._write({"type": "security", "event": event_type, "detail": detail[:1000]})
        print(f"[SECURITY] ⚠️  {event_type}: {detail[:120]}")

    def metric_sample(self, metric: str, value: float, baseline: float, threshold: float) -> None:
        """Record a bake-window metric observation."""
        breach = value > threshold
        self._write({
            "type": "metric",
            "metric": metric,
            "value": value,
            "baseline": baseline,
            "threshold": threshold,
            "breach": breach,
        })
        tag = "BREACH ⚠️" if breach else "ok"
        print(f"[METRIC] {metric}={value:.4f} (baseline={baseline:.4f}, threshold={threshold:.4f}) [{tag}]")

    def notification_sent(self, channel: str, event: str, success: bool) -> None:
        """Record that a notification was dispatched."""
        self._write({"type": "notification", "channel": channel, "event": event, "success": success})

    def final_report(self, status: str, summary: str) -> None:
        """Close the run with a machine-readable status and human-readable summary."""
        self._write({"type": "final", "status": status, "summary": summary})
        bar = "=" * 60
        print(f"\n{bar}\n[FINAL] Run {self.run_id}: {status}\n{summary}\n{bar}")

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def tail(self, n: int = 20) -> list[dict]:
        """Return the last n log records as parsed dicts (useful for agent context)."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            lines = f.readlines()
        records = []
        for line in lines[-n:]:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return records

    def read_all(self) -> list[dict]:
        """Return all log records for the current run."""
        return self.tail(n=999_999)

    @property
    def log_path(self) -> str:
        return self.path
