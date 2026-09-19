"""
DevOps Agent — Groq-powered autonomous deployment agent.

Usage:
    python agent.py "Deploy the api service from main branch to staging,
                     run the full test suite, and roll back if smoke tests fail."

Modes (set EXECUTION_MODE in .env):
    supervised  (default) — pauses for confirmation on destructive actions
    autonomous            — runs end-to-end; destructive actions are HARD-BLOCKED
    dry_run               — simulates every command, executes nothing real

See README.md for full setup and safety model documentation.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from groq import Groq

from config import Config
from logger import DeploymentLogger
from notifications import Notifier
from tools import TOOL_REGISTRY, TOOL_SCHEMAS, set_confirmation_callback


# ---------------------------------------------------------------------------
# Confirmation callback (wired at startup)
# ---------------------------------------------------------------------------

def _cli_confirmation_callback(command: str) -> bool:
    """
    Interactive confirmation for supervised mode.
    Logs the request regardless of outcome.
    """
    print(f"\n{'='*60}")
    print("⚠️  DESTRUCTIVE ACTION REQUESTED")
    print(f"{'='*60}")
    print(f"Command:\n  {command[:400]}")
    print("=" * 60)
    try:
        answer = input("Type 'yes' to allow, anything else to block: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer == "yes"


def _autonomous_block_callback(command: str) -> bool:
    """
    In autonomous mode destructive commands are HARD-BLOCKED — no operator present.
    This callback always returns False and prints a clear message.
    """
    print(
        f"\n[AUTONOMOUS GUARD] Destructive command hard-blocked "
        f"(no operator present to confirm):\n  {command[:300]}"
    )
    return False


# ---------------------------------------------------------------------------
# System prompt loader
# ---------------------------------------------------------------------------

def load_system_prompt() -> str:
    try:
        with open("system_prompt.md", encoding="utf-8") as f:
            base = f.read()
    except FileNotFoundError:
        base = "You are a DevOps agent. Follow safe deployment practices."

    # Inject runtime context so the model knows its actual operating constraints.
    runtime_ctx = f"""

---
## Runtime Context (this session)
- **Execution mode**: `{Config.EXECUTION_MODE}`
  - `supervised`: you will be asked to confirm destructive actions at the CLI
  - `autonomous`: destructive actions are HARD-BLOCKED (no operator present)
  - `dry_run`: all tool calls are simulated — nothing real executes
- **Max agent steps this run**: {Config.MAX_AGENT_STEPS}
- **Retry policy**: up to {Config.MAX_TOOL_RETRIES} retries, exponential backoff,
  capped at {Config.RETRY_MAX_TOTAL_SEC}s total — ONLY for `failure_class == "transient"`
- **Available tools**: {', '.join(TOOL_REGISTRY.keys())}

### Tool result interpretation
Every tool returns `{{ok, stdout, stderr, exit_code, duration_sec, meta}}`.
`meta.failure_class` tells you whether to retry or diagnose:
  - `transient` → eligible for retry (network/timeout/rate-limit)
  - `auth` → NEVER retry; surface immediately with root-cause
  - `deterministic` → code/config error; diagnose from stdout/stderr, do not retry
  - `disk` → storage issue; clear known-safe caches, retry once
  - `oom` → reduce parallelism, retry once, then escalate
  - `ambiguous` → treat as deterministic (fail closed)

### Key rules for this run
1. **Always call `kubectl_rollout_status` or `health_check` after every deploy** before
   declaring success. Exit code 0 alone is NOT sufficient proof.
2. **Rollback trigger**: if post-deploy health checks fail after one grace re-check,
   call `kubectl_rollback` immediately — this is a safety action, no confirmation needed.
3. **Secret scan**: run `secret_scan` on the build artifact/diff before deploying.
   Halt if findings are returned.
4. **Pre-flight**: call `check_disk` and verify credentials before the first real command.
5. **Final output**: always end with a structured summary:
   STATUS (SUCCESS / ROLLED BACK / HALTED), what was deployed, tests run and results,
   remediation/retries taken, rollback status if any, next steps.
"""
    return base + runtime_ctx


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class DevOpsAgent:
    def __init__(self) -> None:
        Config.validate()
        self.client = Groq(api_key=Config.GROQ_API_KEY)
        self.logger = DeploymentLogger(metadata=Config.as_dict())
        self.notifier = Notifier(logger=self.logger)

        # Wire the confirmation callback based on execution mode.
        if Config.EXECUTION_MODE == "supervised":
            callback = _cli_confirmation_callback
        else:
            callback = _autonomous_block_callback

        # Wrap to log the confirmation event
        def _logging_callback(cmd: str) -> bool:
            granted = callback(cmd)
            self.logger.confirmation_request(cmd, granted)
            return granted

        set_confirmation_callback(_logging_callback)

        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": load_system_prompt()}
        ]

    def _execute_tool_call(self, tool_call: Any) -> dict:
        """Dispatch a single LLM tool call, log it, and return the result."""
        name: str = tool_call.function.name
        try:
            args: dict = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        fn = TOOL_REGISTRY.get(name)
        if fn is None:
            result: dict = {
                "ok": False,
                "stdout": "",
                "stderr": f"Unknown tool: {name!r}. Available: {list(TOOL_REGISTRY.keys())}",
                "exit_code": -1,
                "duration_sec": 0,
                "meta": {"failure_class": "deterministic"},
            }
        else:
            try:
                result = fn(**args)
            except TypeError as exc:
                result = {
                    "ok": False, "stdout": "",
                    "stderr": f"Bad arguments for {name}: {exc}",
                    "exit_code": -1, "duration_sec": 0,
                    "meta": {"failure_class": "deterministic"},
                }
            except Exception as exc:  # noqa: BLE001
                result = {
                    "ok": False, "stdout": "",
                    "stderr": f"Tool raised unexpected exception: {exc}\n"
                              f"{traceback.format_exc()[-800:]}",
                    "exit_code": -1, "duration_sec": 0,
                    "meta": {"failure_class": "ambiguous"},
                }

        self.logger.tool_call(name, args, result)
        return result

    def _print_step(self, step: int, tool_name: str, args: dict,
                    result: dict) -> None:
        ok_icon = "✅" if result.get("ok") else "❌"
        fc = result.get("meta", {}).get("failure_class", "")
        print(
            f"\n[Step {step+1}] {ok_icon} {tool_name}"
            f"  exit={result.get('exit_code')}  "
            f"dur={result.get('duration_sec', 0):.2f}s"
            + (f"  class={fc}" if fc and fc != "none" else "")
        )
        if result.get("stdout") and len(result["stdout"]) < 600:
            print(f"  stdout: {result['stdout'][:500]}")
        if result.get("stderr") and not result.get("ok"):
            print(f"  stderr: {result['stderr'][:400]}")

    def run(self, task: str) -> str:
        """
        Main agent loop.

        Appends the task as a user message, then iterates:
          1. Call Groq chat.completions with tool schemas
          2. If the model returns tool_calls, execute each, append results
          3. If the model returns plain text (no tool_calls), that is the final answer
          4. If MAX_AGENT_STEPS is reached, halt safely with a clear message
        """
        print(f"\n{'='*60}\n[AGENT] Starting run: {self.logger.run_id}\n{'='*60}")
        self.messages.append({"role": "user", "content": task})
        self.logger.stage("run_started", "in_progress", task[:300])
        self.notifier.stage_transition("agent", "run", "started")

        final_answer = ""

        for step in range(Config.MAX_AGENT_STEPS):
            try:
                response = self.client.chat.completions.create(
                    model=Config.GROQ_MODEL,
                    messages=self.messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    temperature=Config.TEMPERATURE,
                    max_completion_tokens=Config.MAX_COMPLETION_TOKENS,
                )
            except Exception as exc:  # noqa: BLE001
                err = f"Groq API error at step {step+1}: {exc}"
                self.logger.stage("api_error", "failed", err)
                self.notifier.deploy_failed("agent", err, self.logger.run_id)
                return err

            choice = response.choices[0]
            msg = choice.message

            # Serialize message for history — handle both Pydantic and plain dicts
            try:
                msg_dict = msg.model_dump(exclude_none=True)
            except AttributeError:
                msg_dict = dict(msg)
            self.messages.append(msg_dict)

            # ---- No tool calls → final answer --------------------------------
            if not msg.tool_calls:
                final_answer = msg.content or "(no response)"
                status = "SUCCESS" if step > 0 else "NO_ACTION_TAKEN"
                self.logger.final_report(status, final_answer)
                return final_answer

            # ---- Print agent reasoning (if any) before tool calls ------------
            if msg.content:
                print(f"\n[AGENT REASONING]\n{msg.content}\n")

            # ---- Execute all tool calls in this turn -------------------------
            for tool_call in msg.tool_calls:
                print(f"\n[TOOL] → {tool_call.function.name}({tool_call.function.arguments[:200]})")
                result = self._execute_tool_call(tool_call)
                self._print_step(step, tool_call.function.name,
                                 json.loads(tool_call.function.arguments or "{}"), result)

                # Append tool result to message history (truncated to avoid token overflow)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result)[:8000],
                })

        # ---- Step cap reached without natural stop ---------------------------
        summary = (
            f"Agent halted: reached max_agent_steps ({Config.MAX_AGENT_STEPS}) "
            f"without completing. Review {self.logger.log_path} for last known state. "
            f"Consider increasing MAX_AGENT_STEPS or breaking the task into smaller steps."
        )
        self.logger.final_report("HALTED", summary)
        self.notifier.deploy_failed("agent", "max steps reached", self.logger.run_id)
        return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Groq-powered autonomous DevOps deployment agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent.py "Deploy api-service from main to staging, run full test suite"
  python agent.py --mode dry_run "Deploy api-service to production"
  python agent.py  # interactive mode (will prompt for task)

Execution modes (override with --mode or EXECUTION_MODE env var):
  supervised  pause + confirm before destructive actions (default)
  autonomous  run end-to-end; destructive actions still hard-blocked
  dry_run     simulate everything, execute nothing real
        """,
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Deployment task in natural language",
    )
    parser.add_argument(
        "--mode",
        choices=["supervised", "autonomous", "dry_run"],
        help="Override EXECUTION_MODE from .env",
    )
    parser.add_argument(
        "--model",
        help="Override GROQ_MODEL from .env",
    )
    args = parser.parse_args()

    # CLI overrides take precedence over .env
    if args.mode:
        import os
        os.environ["EXECUTION_MODE"] = args.mode
        Config.EXECUTION_MODE = args.mode
    if args.model:
        import os
        os.environ["GROQ_MODEL"] = args.model
        Config.GROQ_MODEL = args.model

    task = args.task
    if not task:
        try:
            task = input("Describe the deployment task: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo task provided. Exiting.")
            sys.exit(0)

    if not task:
        print("No task provided. Exiting.")
        sys.exit(0)

    agent = DevOpsAgent()
    try:
        final = agent.run(task)
        print(f"\n{'='*60}\n{final}\n{'='*60}")
    except KeyboardInterrupt:
        print("\n\n[INTERRUPTED] Operator interrupted run. Partial state in logs.")
        sys.exit(1)
    except EnvironmentError as exc:
        print(f"\n[CONFIG ERROR] {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
