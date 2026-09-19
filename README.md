# DevOps Agent

A production-grade autonomous DevOps agent powered by the [Groq API](https://console.groq.com).
It plans, deploys, tests, self-heals transient failures, and rolls back automatically
when safety thresholds are breached — all with a hard confirmation gate on anything destructive.

---

## Features

- **Autonomous deployment lifecycle**: plan → pre-flight → build → test → deploy → smoke → verify → report
- **Five deploy strategies**: rolling update (default), blue-green, canary (progressive %), dark deploy (feature-flagged), multi-region
- **Self-healing**: classifies every failure (`transient/auth/disk/oom/deterministic/ambiguous`) and auto-remediates or escalates appropriately
- **Non-negotiable safety guardrails**: destructive commands hard-blocked in all modes
- **Secret scan**: scans build artifacts/diffs for accidentally committed secrets before deploy
- **Structured JSONL audit log**: one file per run, fully greppable
- **Pluggable notifications**: Slack and generic webhook (off by default)
- **Three execution modes**: supervised, autonomous, dry_run

---

## Setup

**Requirements**: Python 3.11+

```bash
# 1. Clone / enter the project directory
cd Mr.Dev-Op

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp env.example .env
# Edit .env — at minimum, set GROQ_API_KEY
```

**.env minimum configuration**:
```
GROQ_API_KEY=gsk_your_key_here
EXECUTION_MODE=supervised
```

---

## Running

```bash
# Describe the task as a CLI argument:
python agent.py "Deploy api-service from main branch to staging, run full test suite, roll back on failure"

# Or interactively:
python agent.py

# Force dry-run mode (simulates all commands, executes nothing):
python agent.py --mode dry_run "Deploy api-service to production"

# Force autonomous mode (no prompts; destructive actions still hard-blocked):
python agent.py --mode autonomous "Deploy api-service to staging"

# Override model:
python agent.py --model llama-3.3-70b-versatile "..."
```

---

## Execution Modes

| Mode | Destructive actions | Prompts operator | Real commands |
|---|---|---|---|
| `supervised` (default) | Pause + confirm via CLI | Yes | Yes |
| `autonomous` | **Hard-blocked** (no operator present) | Never | Yes |
| `dry_run` | Hard-blocked | Never | No — simulated only |

Set via `EXECUTION_MODE` in `.env` or `--mode` CLI flag.

**Use `dry_run`** when:
- Testing the agent's planning/reasoning against a new environment
- CI/CD pipeline dry-run gates
- Reviewing what the agent would do before granting it access

**Use `autonomous`** when:
- CI/CD pipelines with pre-approved, non-destructive task scope
- You trust the task is bounded (e.g. staging-only, no prod targets)
- Destructive actions (delete, force, destroy) are acceptable to hard-block

---

## Safety Model

### Failure Classification
Every tool result includes `meta.failure_class`:

| Class | Meaning | Retry? |
|---|---|---|
| `transient` | Network blip, timeout, rate-limit | Yes — exponential backoff |
| `auth` | Credentials/permissions | **Never** (lockout risk) |
| `deterministic` | Test failure, compile error, schema mismatch | No — diagnose |
| `disk` | Storage full | Clear caches, retry once |
| `oom` | Memory exhausted | Reduce parallelism, retry once |
| `ambiguous` | Unknown | No (fail closed) |

### Retry Bounds
Auto-retries are bounded by **both** `MAX_TOOL_RETRIES` (count) **and** `RETRY_MAX_TOTAL_SEC`
(wall time) — whichever is hit first. Neither bound alone can cause an infinite loop.

### Destructive Command Guard
Any shell command matching a known-dangerous pattern (`rm -rf`, `kubectl delete`,
`terraform destroy`, `git push --force`, `DROP TABLE`, `DELETE FROM`, etc.) is
intercepted and routed through a confirmation callback before execution.

- **Supervised mode**: CLI prompt — type `yes` to allow, anything else blocks.
- **Autonomous mode**: Hard-blocked — the agent prints a clear message and continues safely.
- **Rollback commands** (`kubectl rollout undo`) are explicitly exempted — they are safety
  actions, not destructive operations.

### Secret Scan
Before every deploy, `secret_scan` is run on the build artifact or working directory.
Findings halt the deployment. Actual secret values are **never** returned or logged —
only redacted placeholders appear in audit records.

---

## Project Structure

```
Mr.Dev-Op/
├── agent.py              # Main agent loop (Groq chat.completions + tool dispatch)
├── tools.py              # Tool implementations, registry, JSON schemas, safety gate
├── config.py             # All settings (env vars / .env), startup validation
├── logger.py             # Structured JSONL audit logger (one file per run)
├── deploy_strategies.py  # Rolling, blue-green, canary, dark-deploy, multi-region
├── notifications.py      # Slack + generic webhook notifier (no-op by default)
├── system_prompt.md      # Agent operating doctrine (plan→deploy→verify→rollback)
├── requirements.txt      # Pinned dependencies
├── env.example           # .env template (copy to .env, never commit .env)
├── README.md             # This file
└── tests/
    ├── test_failure_classification.py   # Heuristic classification tests
    ├── test_destructive_guard.py        # Safety gate + autonomous block tests
    ├── test_retry_backoff.py            # Retry bounds + backoff tests
    └── test_secret_scan.py              # Secret pattern detection tests
```

---

## Running the Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific suite
pytest tests/test_destructive_guard.py -v

# Run with coverage (if pytest-cov is installed)
pytest tests/ --cov=. --cov-report=term-missing
```

Tests cover:
- Failure classification heuristic (all 6 classes, edge cases)
- Destructive command pattern matching (blocked / safe commands)
- Destructive gate behaviour in supervised vs autonomous vs force_safe
- Retry count bound (`MAX_TOOL_RETRIES`)
- Retry total time bound (`RETRY_MAX_TOTAL_SEC`)
- Both bounds together (neither causes infinite loop)
- Secret scan detection and redaction

---

## Adding a New Tool

1. Implement a function in `tools.py` that returns the standard shape:
   ```python
   def tool_my_tool(param1: str, param2: int = 0) -> dict:
       result = run_with_retry(f"my-cli {param1} --count {param2}")
       result["meta"]["tool_specific_data"] = "..."
       return result
   # Shape: {ok, stdout, stderr, exit_code, duration_sec, meta}
   ```

2. Register it in `TOOL_REGISTRY`:
   ```python
   TOOL_REGISTRY["my_tool"] = tool_my_tool
   ```

3. Add its JSON schema to `TOOL_SCHEMAS`:
   ```python
   {"type": "function", "function": {
       "name": "my_tool",
       "description": "What this tool does and when to use it.",
       "parameters": {
           "type": "object",
           "properties": {
               "param1": {"type": "string", "description": "..."},
               "param2": {"type": "integer"},
           },
           "required": ["param1"],
       },
   }}
   ```

No changes to `agent.py` — the loop is fully generic over the tool registry.

---

## Configuration Reference

All values set via `.env` or environment variables. See `env.example` for full list.

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | — | **Required.** Groq API key |
| `GROQ_MODEL` | `moonshotai/kimi-k2-instruct` | LLM model name |
| `EXECUTION_MODE` | `supervised` | `supervised` / `autonomous` / `dry_run` |
| `MAX_AGENT_STEPS` | `40` | Hard cap on tool-call turns |
| `MAX_TOOL_RETRIES` | `3` | Max retries for transient failures |
| `RETRY_MAX_TOTAL_SEC` | `300` | Wall-time ceiling for retries |
| `CANARY_STEPS` | `5,25,50,100` | Canary traffic percentages |
| `CANARY_BAKE_SEC` | `60` | Seconds to hold at each canary step |
| `CANARY_ERROR_THRESHOLD` | `0.02` | Auto-rollback error rate threshold (2%) |
| `SECRET_SCAN_ENABLED` | `true` | Scan for secrets before deploy |
| `NOTIFICATIONS_ENABLED` | `false` | Enable Slack/webhook notifications |
| `SLACK_WEBHOOK_URL` | — | Slack incoming webhook URL |
| `LOG_DIR` | `./deployment_logs` | JSONL audit log directory |

---

## Deploying to Render

This agent runs as a **background worker** on Render (not a web service, since it's
a CLI tool rather than an HTTP server).

### Steps

1. **Push to GitHub** (or connect your repository to Render).

2. **Create a new Background Worker** on [render.com](https://render.com):
   - Environment: **Python**
   - Build command: `pip install -r requirements.txt`
   - Start command: `python agent.py --mode autonomous "$DEPLOY_TASK"`

3. **Set environment variables** in the Render dashboard (Environment tab):
   - `GROQ_API_KEY` — your Groq API key
   - `EXECUTION_MODE` — `autonomous` (no TTY for supervised prompts in CI)
   - `DEPLOY_TASK` — the deployment task string
   - Any other variables from `env.example` you want to override

4. **Set `LOG_DIR`** to a Render persistent disk path (e.g. `/var/data/deployment_logs`)
   if you want logs to survive restarts. Otherwise logs are ephemeral.

> **Note**: In `autonomous` mode on Render, destructive commands are hard-blocked since
> there's no terminal for operator confirmation. Scope your tasks to non-destructive
> operations (deploy, test, rollback) and avoid tasks that require `kubectl delete`,
> `terraform destroy`, etc.

### Render `render.yaml` (optional)

```yaml
services:
  - type: worker
    name: devops-agent
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: python agent.py --mode autonomous "$DEPLOY_TASK"
    envVars:
      - key: GROQ_API_KEY
        sync: false   # Set via Render dashboard, not committed to repo
      - key: EXECUTION_MODE
        value: autonomous
      - key: LOG_DIR
        value: /var/data/deployment_logs
      - key: NOTIFICATIONS_ENABLED
        value: "true"
```

---

## Audit Logs

Every run writes to `deployment_logs/<run-id>.jsonl`. Each line is a JSON record:

```jsonl
{"type":"run_start","run_id":"run-20250703T120000Z","config":{...},"ts":"..."}
{"type":"stage","stage":"plan","status":"in_progress","ts":"..."}
{"type":"tool_call","tool":"check_disk","args":{},"result":{"ok":true,...},"ts":"..."}
{"type":"rollback","reason":"smoke test failed","mechanism":"kubectl rollout undo","outcome":"healthy","ts":"..."}
{"type":"final","status":"ROLLED_BACK","summary":"...","ts":"..."}
```

Grep examples:
```bash
# All rollback events across all runs
grep '"type":"rollback"' deployment_logs/*.jsonl | jq .

# All tool calls that failed
grep '"type":"tool_call"' deployment_logs/run-*.jsonl | jq 'select(.result.ok == false)'

# Final status for all runs
grep '"type":"final"' deployment_logs/*.jsonl | jq '{run_id, status: .status}'
```

---

## Model Selection

The default model (`moonshotai/kimi-k2-instruct`) is a strong tool-use + long-context
model available on Groq as of July 2025.

Check [https://console.groq.com/docs/models](https://console.groq.com/docs/models) for
the current model list — Groq's lineup changes frequently. Set `GROQ_MODEL` in `.env`
to switch models without touching code.

For cost-sensitive or lower-risk tasks, `llama-3.3-70b-versatile` is a reliable fallback.

---

## Security Notes

- This agent executes real shell commands on the machine it runs on.
- Run in a sandboxed/CI environment for anything beyond local testing.
- Always start with `dry_run` mode against a new environment.
- Rotate your `GROQ_API_KEY` if it's ever printed in logs (the logger scrubs common patterns
  but treat key rotation as mandatory on any suspected exposure).
- Never commit `.env` to version control.
#   M r . D e v - o p  
 #   M r . D e v - o p  
 