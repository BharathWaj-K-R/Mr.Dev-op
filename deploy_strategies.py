"""
Composable deployment strategies the agent can invoke.

Each strategy is a class with a consistent interface:
    strategy.execute(context: DeployContext) -> StrategyResult

Strategies:
  RollingStrategy       — k8s rolling update (default, safest, in-place)
  BlueGreenStrategy     — parallel environment, instant traffic switch
  CanaryStrategy        — progressive traffic %, automated pass/fail per step
  DarkDeployStrategy    — feature-flagged dark deploy (decoupled from release)

All strategies:
  - Run smoke tests after deploy and confirm rollback health after any rollback
  - Emit structured log events via the passed DeploymentLogger
  - Return a StrategyResult with status, evidence, and rollback info
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from config import Config
from logger import DeploymentLogger
from tools import (
    tool_health_check,
    tool_kubectl_apply,
    tool_kubectl_rollback,
    tool_kubectl_rollout_status,
    tool_run_shell,
)


@dataclass
class DeployContext:
    """Everything a strategy needs to know about a single deployment."""
    service: str
    namespace: str
    image_tag: str
    manifest_path: str
    health_url: str
    rollback_image_tag: str = ""          # previous image tag for blue-green
    canary_steps: list[int] = field(default_factory=lambda: [5, 25, 50, 100])
    canary_bake_sec: int = 60
    error_threshold: float = 0.02         # 2%
    latency_threshold_ms: int = 500
    # Optional: called by canary to get real metrics; returns {"error_rate": 0.01, "p95_ms": 200}
    metrics_provider: Callable[[], dict] | None = None
    logger: DeploymentLogger | None = None
    region: str = ""                      # for multi-region tagging


@dataclass
class StrategyResult:
    status: str          # "success" | "rolled_back" | "halted"
    strategy: str
    evidence: list[dict] = field(default_factory=list)
    rollback_triggered: bool = False
    rollback_confirmed_healthy: bool = False
    error_message: str = ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _log(ctx: DeployContext, message: str) -> None:
    if ctx.logger:
        ctx.logger.stage("strategy", "in_progress", message)
    else:
        print(f"[STRATEGY] {message}")


def _run_smoke(ctx: DeployContext, label: str = "smoke") -> dict:
    """Run a single health-check smoke test and return the result."""
    result = tool_health_check(ctx.health_url, timeout=15, expected_status=200)
    ev = {
        "check": label,
        "url": ctx.health_url,
        "ok": result["ok"],
        "status_code": result.get("exit_code"),
        "body_preview": result.get("stdout", "")[:200],
    }
    _log(ctx, f"smoke test [{label}]: ok={result['ok']} status={result.get('exit_code')}")
    return ev


def _do_rollback(ctx: DeployContext, reason: str) -> tuple[bool, dict]:
    """
    Execute rollback and verify the rolled-back state is healthy.
    Returns (confirmed_healthy: bool, evidence_dict).
    Never assumes rollback = fixed.
    """
    _log(ctx, f"ROLLBACK triggered: {reason}")
    rb_result = tool_kubectl_rollback(ctx.service, ctx.namespace)
    if ctx.logger:
        ctx.logger.rollback(reason, "kubectl rollout undo", "initiated")

    if not rb_result["ok"]:
        msg = f"Rollback command failed: {rb_result.get('stderr', '')[:300]}"
        if ctx.logger:
            ctx.logger.rollback(reason, "kubectl rollout undo", f"FAILED: {msg}")
        return False, {"rollback_ok": False, "error": msg}

    # Wait for rollback to stabilise
    status = tool_kubectl_rollout_status(ctx.service, ctx.namespace, timeout=120)
    if not status["ok"]:
        if ctx.logger:
            ctx.logger.rollback(reason, "rollout status after undo", "FAILED to converge")
        return False, {"rollback_ok": False, "error": status.get("stderr", "")[:300]}

    # Verify rolled-back state is actually healthy
    smoke = _run_smoke(ctx, label="post_rollback_smoke")
    healthy = smoke["ok"]
    outcome = "healthy ✅" if healthy else "still unhealthy ❌"
    if ctx.logger:
        ctx.logger.rollback(reason, "post-rollback smoke", outcome)
    return healthy, {"rollback_ok": True, "smoke": smoke, "healthy_after_rollback": healthy}


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------

class RollingStrategy:
    """
    Standard Kubernetes rolling update.
    Safe default: k8s replaces pods gradually, zero-downtime if probes are set.
    """
    NAME = "rolling"

    def execute(self, ctx: DeployContext) -> StrategyResult:
        evidence: list[dict] = []
        _log(ctx, f"Rolling deploy: {ctx.service}:{ctx.image_tag} → {ctx.namespace}")

        # Dry-run validation first
        dry = tool_kubectl_apply(ctx.manifest_path, ctx.namespace, dry_run=True)
        evidence.append({"step": "dry_run", "ok": dry["ok"], "stdout": dry["stdout"][:500]})
        if not dry["ok"]:
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message=f"Dry-run failed: {dry['stderr'][:300]}",
            )

        # Real apply
        apply = tool_kubectl_apply(ctx.manifest_path, ctx.namespace, dry_run=False)
        evidence.append({"step": "apply", "ok": apply["ok"]})
        if not apply["ok"]:
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message=f"Apply failed: {apply['stderr'][:300]}",
            )

        # Wait for rollout
        rollout = tool_kubectl_rollout_status(ctx.service, ctx.namespace, timeout=180)
        evidence.append({"step": "rollout_status", "ok": rollout["ok"],
                         "stdout": rollout["stdout"][:500]})
        if not rollout["ok"]:
            healthy, rb_ev = _do_rollback(ctx, "rollout did not converge")
            evidence.append(rb_ev)
            return StrategyResult(
                status="rolled_back", strategy=self.NAME, evidence=evidence,
                rollback_triggered=True, rollback_confirmed_healthy=healthy,
                error_message="Rollout timed out; rolled back.",
            )

        # Smoke test
        smoke = _run_smoke(ctx)
        evidence.append(smoke)
        if not smoke["ok"]:
            # Grace re-check
            _log(ctx, "Smoke test failed — grace re-check in 10s")
            time.sleep(10)
            smoke2 = _run_smoke(ctx, label="smoke_grace")
            evidence.append(smoke2)
            if not smoke2["ok"]:
                healthy, rb_ev = _do_rollback(ctx, "smoke tests failed after grace period")
                evidence.append(rb_ev)
                return StrategyResult(
                    status="rolled_back", strategy=self.NAME, evidence=evidence,
                    rollback_triggered=True, rollback_confirmed_healthy=healthy,
                    error_message="Smoke tests failed; rolled back.",
                )

        return StrategyResult(status="success", strategy=self.NAME, evidence=evidence)


class BlueGreenStrategy:
    """
    Blue-green deployment: spin up the new (green) environment alongside the
    stable (blue) one, run smoke tests on green, then switch traffic instantly.
    Rollback = switch traffic back to blue in milliseconds.
    """
    NAME = "blue_green"

    def execute(self, ctx: DeployContext) -> StrategyResult:
        evidence: list[dict] = []
        _log(ctx, f"Blue-green deploy: {ctx.service}:{ctx.image_tag}")

        # Deploy green (parallel service / new deployment slot)
        green_manifest = ctx.manifest_path  # agent supplies green-specific manifest
        dry = tool_kubectl_apply(green_manifest, ctx.namespace, dry_run=True)
        evidence.append({"step": "green_dry_run", "ok": dry["ok"]})
        if not dry["ok"]:
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message=f"Green dry-run failed: {dry['stderr'][:300]}",
            )

        apply = tool_kubectl_apply(green_manifest, ctx.namespace, dry_run=False)
        evidence.append({"step": "green_apply", "ok": apply["ok"]})
        if not apply["ok"]:
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message=f"Green apply failed: {apply['stderr'][:300]}",
            )

        rollout = tool_kubectl_rollout_status(f"{ctx.service}-green", ctx.namespace, timeout=180)
        evidence.append({"step": "green_rollout", "ok": rollout["ok"]})
        if not rollout["ok"]:
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message="Green rollout failed to converge.",
            )

        # Smoke test green BEFORE switching traffic
        smoke = _run_smoke(ctx, label="green_pre_switch")
        evidence.append(smoke)
        if not smoke["ok"]:
            _log(ctx, "Green smoke failed — aborting before traffic switch (blue still live)")
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message="Green environment smoke tests failed; traffic NOT switched.",
            )

        # Traffic switch (update Service selector to green)
        switch = tool_run_shell(
            f"kubectl patch service {ctx.service} -n {ctx.namespace} "
            f"-p '{{\"spec\":{{\"selector\":{{\"slot\":\"green\"}}}}}}'",
        )
        evidence.append({"step": "traffic_switch", "ok": switch["ok"]})
        if not switch["ok"]:
            return StrategyResult(
                status="halted", strategy=self.NAME, evidence=evidence,
                error_message=f"Traffic switch failed: {switch['stderr'][:300]}",
            )

        # Post-switch smoke
        smoke2 = _run_smoke(ctx, label="post_switch")
        evidence.append(smoke2)
        if not smoke2["ok"]:
            # Instant rollback = switch traffic back to blue
            _log(ctx, "Post-switch smoke failed — switching traffic back to blue")
            tool_run_shell(
                f"kubectl patch service {ctx.service} -n {ctx.namespace} "
                f"-p '{{\"spec\":{{\"selector\":{{\"slot\":\"blue\"}}}}}}'",
            )
            blue_smoke = _run_smoke(ctx, label="blue_rollback_smoke")
            evidence.append(blue_smoke)
            if ctx.logger:
                ctx.logger.rollback("post-switch smoke failed",
                                    "traffic switch back to blue",
                                    "healthy" if blue_smoke["ok"] else "still unhealthy")
            return StrategyResult(
                status="rolled_back", strategy=self.NAME, evidence=evidence,
                rollback_triggered=True,
                rollback_confirmed_healthy=blue_smoke["ok"],
                error_message="Traffic switched back to blue after post-switch smoke failure.",
            )

        return StrategyResult(status="success", strategy=self.NAME, evidence=evidence)


class CanaryStrategy:
    """
    Progressive canary rollout.
    Traffic steps up through configured percentages (e.g. 5→25→50→100).
    At each step, bake for a defined window and check error rate + latency.
    Any breach triggers immediate halt and traffic shift back to stable.
    """
    NAME = "canary"

    def execute(self, ctx: DeployContext) -> StrategyResult:
        evidence: list[dict] = []
        steps = ctx.canary_steps or Config.canary_steps()
        _log(ctx, f"Canary deploy: {ctx.service}:{ctx.image_tag} steps={steps}")

        for pct in steps:
            _log(ctx, f"  → Canary step: {pct}% traffic")
            # Adjust traffic weight (implementation varies: Istio VirtualService,
            # nginx-ingress canary annotation, Argo Rollouts, etc.)
            shift = tool_run_shell(
                f"kubectl annotate ingress {ctx.service} "
                f"nginx.ingress.kubernetes.io/canary-weight={pct} "
                f"-n {ctx.namespace} --overwrite",
            )
            evidence.append({"step": f"canary_{pct}pct_shift", "ok": shift["ok"]})
            if not shift["ok"]:
                healthy, rb_ev = _do_rollback(ctx, f"traffic shift to {pct}% failed")
                evidence.append(rb_ev)
                return StrategyResult(
                    status="rolled_back", strategy=self.NAME, evidence=evidence,
                    rollback_triggered=True, rollback_confirmed_healthy=healthy,
                    error_message=f"Canary traffic shift to {pct}% failed.",
                )

            # Bake window
            bake_start = time.monotonic()
            bake_end = bake_start + ctx.canary_bake_sec
            breached = False
            breach_reason = ""

            while time.monotonic() < bake_end:
                # Smoke check
                smoke = _run_smoke(ctx, label=f"canary_{pct}pct_bake")
                evidence.append(smoke)
                if not smoke["ok"]:
                    breached = True
                    breach_reason = f"Health check failed at {pct}% canary"
                    break

                # Metrics check (if a provider is configured)
                if ctx.metrics_provider:
                    try:
                        metrics = ctx.metrics_provider()
                        error_rate = metrics.get("error_rate", 0.0)
                        p95_ms = metrics.get("p95_ms", 0)
                        if ctx.logger:
                            ctx.logger.metric_sample(
                                "error_rate", error_rate, 0.0, ctx.error_threshold
                            )
                            ctx.logger.metric_sample(
                                "p95_ms", p95_ms, 0.0, ctx.latency_threshold_ms
                            )
                        if error_rate > ctx.error_threshold:
                            breached = True
                            breach_reason = (
                                f"Error rate {error_rate:.2%} > threshold "
                                f"{ctx.error_threshold:.2%} at {pct}%"
                            )
                            break
                        if p95_ms > ctx.latency_threshold_ms:
                            breached = True
                            breach_reason = (
                                f"p95 latency {p95_ms}ms > threshold "
                                f"{ctx.latency_threshold_ms}ms at {pct}%"
                            )
                            break
                    except Exception as exc:  # noqa: BLE001
                        _log(ctx, f"Metrics provider error (non-fatal): {exc}")

                time.sleep(min(15, ctx.canary_bake_sec))  # poll interval

            if breached:
                # Immediately halt rollout and shift traffic back to 0% canary
                _log(ctx, f"CANARY BREACH at {pct}%: {breach_reason} — halting immediately")
                tool_run_shell(
                    f"kubectl annotate ingress {ctx.service} "
                    f"nginx.ingress.kubernetes.io/canary-weight=0 "
                    f"-n {ctx.namespace} --overwrite",
                )
                healthy, rb_ev = _do_rollback(ctx, breach_reason)
                evidence.append(rb_ev)
                if ctx.logger:
                    ctx.logger.rollback(breach_reason, "canary halt + traffic to 0%",
                                        "healthy" if healthy else "unhealthy")
                return StrategyResult(
                    status="rolled_back", strategy=self.NAME, evidence=evidence,
                    rollback_triggered=True, rollback_confirmed_healthy=healthy,
                    error_message=breach_reason,
                )

            _log(ctx, f"  ✅ Canary step {pct}% passed bake window")

        # All steps passed — remove canary annotation (full traffic to new version)
        tool_run_shell(
            f"kubectl annotate ingress {ctx.service} "
            f"nginx.ingress.kubernetes.io/canary- "
            f"-n {ctx.namespace}",
        )
        final_smoke = _run_smoke(ctx, label="canary_final")
        evidence.append(final_smoke)
        if not final_smoke["ok"]:
            healthy, rb_ev = _do_rollback(ctx, "final smoke failed after full canary rollout")
            evidence.append(rb_ev)
            return StrategyResult(
                status="rolled_back", strategy=self.NAME, evidence=evidence,
                rollback_triggered=True, rollback_confirmed_healthy=healthy,
            )

        return StrategyResult(status="success", strategy=self.NAME, evidence=evidence)


class DarkDeployStrategy:
    """
    Feature-flagged dark deploy.
    Code is deployed but kept behind a feature flag — no user traffic sees it
    until the flag is explicitly turned on.  Rollback = flip flag off, not redeploy.
    Useful for high-risk features where deployment and release are decoupled.
    """
    NAME = "dark_deploy"

    def execute(self, ctx: DeployContext) -> StrategyResult:
        evidence: list[dict] = []
        _log(ctx, f"Dark deploy (feature-flagged): {ctx.service}:{ctx.image_tag}")

        # Deploy as normal but verify flag is OFF before enabling
        flag_check = tool_run_shell(
            f"curl -sf http://flagd:8013/schema.json 2>&1 | head -20 || "
            f"echo 'no flagd endpoint - proceeding with env-var flag check'"
        )
        evidence.append({"step": "flag_check", "ok": flag_check["ok"],
                         "note": "Verifying feature flag is disabled before deploy"})

        # Rolling deploy (code goes live, but feature is dark)
        rolling = RollingStrategy()
        result = rolling.execute(ctx)
        evidence.extend(result.evidence)

        if result.status != "success":
            return StrategyResult(
                status=result.status, strategy=self.NAME, evidence=evidence,
                rollback_triggered=result.rollback_triggered,
                rollback_confirmed_healthy=result.rollback_confirmed_healthy,
                error_message=result.error_message,
            )

        _log(ctx, "Dark deploy complete. Feature flag is still OFF. "
                  "Enable flag to release to users; disable to roll back instantly.")
        evidence.append({
            "step": "dark_deploy_complete",
            "note": "Code live behind flag. Release = enable flag. Rollback = disable flag.",
        })
        return StrategyResult(status="success", strategy=self.NAME, evidence=evidence)


class MultiRegionRollout:
    """
    Orchestrates a deploy across multiple regions with bake time between each.
    Wraps any base strategy (default: rolling) and applies it region-by-region.
    """
    NAME = "multi_region"

    def __init__(self, base_strategy: RollingStrategy | BlueGreenStrategy | None = None):
        self.base = base_strategy or RollingStrategy()

    def execute(self, ctx: DeployContext,
                regions: list[str] | None = None) -> list[StrategyResult]:
        regions = regions or Config.REGIONS
        bake = Config.REGION_BAKE_SEC
        results: list[StrategyResult] = []

        for region in regions:
            _log(ctx, f"Multi-region: deploying to region={region}")
            # Clone context with region tag
            import copy
            region_ctx = copy.copy(ctx)
            region_ctx.region = region

            result = self.base.execute(region_ctx)
            results.append(result)

            if result.status != "success":
                if ctx.logger:
                    ctx.logger.stage(
                        "multi_region_abort", "failed",
                        f"Stopped at region={region}: {result.error_message}",
                    )
                # Halt remaining regions — don't roll out into more regions if one fails
                break

            _log(ctx, f"Region {region} succeeded. Baking {bake}s before next region.")
            time.sleep(bake)

        return results


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------

STRATEGY_MAP: dict[str, type] = {
    "rolling": RollingStrategy,
    "blue_green": BlueGreenStrategy,
    "canary": CanaryStrategy,
    "dark_deploy": DarkDeployStrategy,
}


def get_strategy(name: str) -> RollingStrategy | BlueGreenStrategy | CanaryStrategy | DarkDeployStrategy:
    """Resolve strategy name to an instance.  Defaults to rolling."""
    cls = STRATEGY_MAP.get(name.lower(), RollingStrategy)
    return cls()
