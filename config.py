"""
Configuration for the DevOps Agent.

All values are overridable via environment variables / .env file.
The Config.validate() call at agent startup raises a clear error if any
required value is missing or invalid, so the agent never starts silently
misconfigured.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ---------------------------------------------------------------------------
    # Groq API
    # ---------------------------------------------------------------------------
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # Default: moonshotai/kimi-k2-instruct — strong tool-use + reasoning on Groq (July 2025).
    # Other good choices (check https://console.groq.com/docs/models for current list):
    #   llama-3.3-70b-versatile   (stable, broad availability)
    #   llama-3.1-70b-versatile   (slightly older, still solid)
    #   llama3-70b-8192           (original 70B, fast)
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "moonshotai/kimi-k2-instruct")
    TEMPERATURE: float = float(os.getenv("GROQ_TEMPERATURE", "0.2"))  # low = deterministic ops
    MAX_COMPLETION_TOKENS: int = int(os.getenv("GROQ_MAX_TOKENS", "4096"))

    # ---------------------------------------------------------------------------
    # Agent loop safety
    # ---------------------------------------------------------------------------
    MAX_AGENT_STEPS: int = int(os.getenv("MAX_AGENT_STEPS", "40"))         # hard cap on tool-call turns
    MAX_TOOL_RETRIES: int = int(os.getenv("MAX_TOOL_RETRIES", "3"))        # only for transient failures
    RETRY_BASE_DELAY_SEC: float = float(os.getenv("RETRY_BASE_DELAY_SEC", "2.0"))
    RETRY_MAX_TOTAL_SEC: float = float(os.getenv("RETRY_MAX_TOTAL_SEC", "300.0"))
    DEFAULT_COMMAND_TIMEOUT_SEC: int = int(os.getenv("DEFAULT_COMMAND_TIMEOUT_SEC", "120"))

    # ---------------------------------------------------------------------------
    # Execution mode
    # "supervised"  — pause + ask for confirmation on destructive actions (default)
    # "autonomous"  — run end-to-end; destructive actions are still HARD-BLOCKED
    # "dry_run"     — never execute real commands; prints what it would do
    # ---------------------------------------------------------------------------
    EXECUTION_MODE: str = os.getenv("EXECUTION_MODE", "supervised")

    # ---------------------------------------------------------------------------
    # Deploy / canary configuration
    # ---------------------------------------------------------------------------
    CANARY_STEPS: str = os.getenv("CANARY_STEPS", "5,25,50,100")        # traffic % stages
    CANARY_BAKE_SEC: int = int(os.getenv("CANARY_BAKE_SEC", "60"))       # seconds per stage
    CANARY_ERROR_THRESHOLD: float = float(os.getenv("CANARY_ERROR_THRESHOLD", "0.02"))  # 2 %
    CANARY_LATENCY_THRESHOLD_MS: int = int(os.getenv("CANARY_LATENCY_THRESHOLD_MS", "500"))
    BAKE_WINDOW_SEC: int = int(os.getenv("BAKE_WINDOW_SEC", "300"))       # monitoring window
    MULTI_REGION: bool = os.getenv("MULTI_REGION", "false").lower() == "true"
    REGIONS: list = [r.strip() for r in os.getenv("REGIONS", "us-east-1").split(",")]
    REGION_BAKE_SEC: int = int(os.getenv("REGION_BAKE_SEC", "120"))

    # ---------------------------------------------------------------------------
    # Notifications (off by default)
    # ---------------------------------------------------------------------------
    SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")
    NOTIFY_WEBHOOK_URL: str = os.getenv("NOTIFY_WEBHOOK_URL", "")
    NOTIFICATIONS_ENABLED: bool = os.getenv("NOTIFICATIONS_ENABLED", "false").lower() == "true"

    # ---------------------------------------------------------------------------
    # Logging
    # ---------------------------------------------------------------------------
    LOG_DIR: str = os.getenv("LOG_DIR", "./deployment_logs")

    # ---------------------------------------------------------------------------
    # Security scan
    # ---------------------------------------------------------------------------
    SECRET_SCAN_ENABLED: bool = os.getenv("SECRET_SCAN_ENABLED", "true").lower() == "true"

    # ---------------------------------------------------------------------------
    # Change correlation metadata
    # ---------------------------------------------------------------------------
    DEPLOY_AUTHOR: str = os.getenv("DEPLOY_AUTHOR", "")
    TICKET_ID: str = os.getenv("TICKET_ID", "")

    @classmethod
    def validate(cls) -> None:
        """Fail fast at startup if required configuration is missing or invalid."""
        errors: list[str] = []

        if not cls.GROQ_API_KEY:
            errors.append(
                "GROQ_API_KEY is not set. Export it or put it in a .env file:\n"
                "  export GROQ_API_KEY=gsk_xxx"
            )
        if cls.EXECUTION_MODE not in ("supervised", "autonomous", "dry_run"):
            errors.append(
                f"EXECUTION_MODE must be supervised|autonomous|dry_run, got: {cls.EXECUTION_MODE!r}"
            )
        if cls.MAX_AGENT_STEPS < 1:
            errors.append(f"MAX_AGENT_STEPS must be >= 1, got {cls.MAX_AGENT_STEPS}")
        if cls.MAX_TOOL_RETRIES < 0:
            errors.append(f"MAX_TOOL_RETRIES must be >= 0, got {cls.MAX_TOOL_RETRIES}")
        if cls.RETRY_MAX_TOTAL_SEC <= 0:
            errors.append(f"RETRY_MAX_TOTAL_SEC must be > 0, got {cls.RETRY_MAX_TOTAL_SEC}")

        if errors:
            raise EnvironmentError(
                "DevOps Agent configuration errors:\n  - " + "\n  - ".join(errors)
            )

    @classmethod
    def canary_steps(cls) -> list[int]:
        """Return the list of canary traffic percentages as integers."""
        return [int(x) for x in cls.CANARY_STEPS.split(",") if x.strip()]

    @classmethod
    def as_dict(cls) -> dict:
        """Return non-secret config as a loggable dict."""
        return {
            "model": cls.GROQ_MODEL,
            "execution_mode": cls.EXECUTION_MODE,
            "max_agent_steps": cls.MAX_AGENT_STEPS,
            "max_tool_retries": cls.MAX_TOOL_RETRIES,
            "retry_max_total_sec": cls.RETRY_MAX_TOTAL_SEC,
            "canary_steps": cls.CANARY_STEPS,
            "canary_bake_sec": cls.CANARY_BAKE_SEC,
            "canary_error_threshold": cls.CANARY_ERROR_THRESHOLD,
            "multi_region": cls.MULTI_REGION,
            "regions": cls.REGIONS,
            "notifications_enabled": cls.NOTIFICATIONS_ENABLED,
            "secret_scan_enabled": cls.SECRET_SCAN_ENABLED,
            "log_dir": cls.LOG_DIR,
        }
