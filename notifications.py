"""
Pluggable notification dispatcher.

Off by default (NOTIFICATIONS_ENABLED=false in .env).
Supports Slack (incoming webhook) and a generic JSON webhook out of the box.

Usage in agent code:
    from notifications import Notifier
    notifier = Notifier()
    notifier.send("deploy_started", {"service": "api", "target": "staging"})

Adding a new channel:
    1. Implement a function _send_<channel>(event, payload) -> bool
    2. Register it in Notifier._dispatch()

Events emitted by the agent:
    plan_ready          — written plan is available, execution starting
    stage_transition    — any named stage changed status
    rollback_triggered  — automatic rollback started
    rollback_confirmed  — rollback confirmed (healthy or not)
    deploy_success      — final status: success
    deploy_failed       — final status: halted or rolled back
    canary_breach       — canary threshold breached
    secret_leak_found   — secret scan returned findings
    confirmation_needed — operator confirmation required (supervised mode)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from config import Config
from logger import DeploymentLogger


def _post_json(url: str, payload: dict, timeout: int = 10) -> bool:
    """Send a JSON POST and return True on success."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "devops-agent/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 201, 202, 204)
    except (urllib.error.URLError, urllib.error.HTTPError):
        return False


def _send_slack(event: str, payload: dict, webhook_url: str) -> bool:
    """Format and send a Slack message via incoming webhook."""
    icons = {
        "deploy_success": "✅",
        "deploy_failed": "❌",
        "rollback_triggered": "⚠️",
        "rollback_confirmed": "🔄",
        "canary_breach": "🚨",
        "secret_leak_found": "🔒",
        "plan_ready": "📋",
        "stage_transition": "▶️",
        "confirmation_needed": "⏸️",
    }
    icon = icons.get(event, "ℹ️")
    service = payload.get("service", "unknown")
    ts = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
    detail = payload.get("detail", "")

    text = f"{icon} *[DevOps Agent]* `{event}` — service=`{service}` {detail}\n_ts: {ts}_"
    return _post_json(webhook_url, {"text": text})


def _send_generic_webhook(event: str, payload: dict, webhook_url: str) -> bool:
    """Send the raw payload as JSON to a generic webhook endpoint."""
    envelope = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "devops-agent",
        **payload,
    }
    return _post_json(webhook_url, envelope)


class Notifier:
    """
    Central notification dispatcher.  No-op when NOTIFICATIONS_ENABLED=false.
    """

    def __init__(self, logger: DeploymentLogger | None = None):
        self.enabled = Config.NOTIFICATIONS_ENABLED
        self.logger = logger

    def send(self, event: str, payload: dict[str, Any]) -> None:
        """
        Dispatch a notification to all configured channels.
        Never raises — notification failure must not interrupt a deployment.
        """
        if not self.enabled:
            return

        payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        successes = []
        failures = []

        try:
            for channel, ok in self._dispatch(event, payload):
                (successes if ok else failures).append(channel)
        except Exception:  # noqa: BLE001
            pass  # notification error never masks deployment state

        if self.logger:
            for ch in successes:
                self.logger.notification_sent(ch, event, success=True)
            for ch in failures:
                self.logger.notification_sent(ch, event, success=False)

    def _dispatch(self, event: str, payload: dict) -> list[tuple[str, bool]]:
        """Yield (channel_name, success) pairs for every configured channel."""
        results = []

        if Config.SLACK_WEBHOOK_URL:
            ok = _send_slack(event, payload, Config.SLACK_WEBHOOK_URL)
            results.append(("slack", ok))

        if Config.NOTIFY_WEBHOOK_URL:
            ok = _send_generic_webhook(event, payload, Config.NOTIFY_WEBHOOK_URL)
            results.append(("webhook", ok))

        return results

    # ------------------------------------------------------------------
    # Convenience methods for common events
    # ------------------------------------------------------------------

    def plan_ready(self, service: str, plan_summary: str) -> None:
        self.send("plan_ready", {"service": service, "detail": plan_summary[:300]})

    def stage_transition(self, service: str, stage: str, status: str) -> None:
        self.send("stage_transition", {
            "service": service,
            "stage": stage,
            "status": status,
            "detail": f"stage={stage} → {status}",
        })

    def rollback_triggered(self, service: str, reason: str) -> None:
        self.send("rollback_triggered", {
            "service": service,
            "detail": f"reason: {reason[:200]}",
        })

    def rollback_confirmed(self, service: str, healthy: bool) -> None:
        self.send("rollback_confirmed", {
            "service": service,
            "detail": f"post-rollback health: {'✅ OK' if healthy else '❌ still unhealthy'}",
        })

    def deploy_success(self, service: str, image_tag: str, run_id: str) -> None:
        self.send("deploy_success", {
            "service": service,
            "image_tag": image_tag,
            "run_id": run_id,
            "detail": f"deployed {image_tag}",
        })

    def deploy_failed(self, service: str, reason: str, run_id: str) -> None:
        self.send("deploy_failed", {
            "service": service,
            "run_id": run_id,
            "detail": f"failed: {reason[:200]}",
        })

    def canary_breach(self, service: str, pct: int, metric: str, value: float) -> None:
        self.send("canary_breach", {
            "service": service,
            "detail": f"Canary {pct}% breached {metric}={value:.4f}",
        })

    def secret_leak_found(self, service: str, finding_count: int) -> None:
        self.send("secret_leak_found", {
            "service": service,
            "detail": f"{finding_count} potential secret(s) found in artifact",
        })

    def confirmation_needed(self, service: str, action: str) -> None:
        self.send("confirmation_needed", {
            "service": service,
            "detail": f"Waiting for operator confirmation: {action[:200]}",
        })
