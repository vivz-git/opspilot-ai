"""The single configuration surface (§17).

`Settings` is the only place in the application that reads the environment.
`tests/test_structure.py` asserts that no other module touches `os.environ`,
because a stray `os.getenv("GROQ_API_KEY")` is exactly the kind of thing
that ends up in a log line.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.state import Budgets, PlannerKind
from app.errors import ConfigurationError


class PlannerMode(StrEnum):
    AUTO = "auto"  # LLM when a key is present, else rules — degrades, never fails
    LLM = "llm"  # explicit request: fails fast without a key
    RULES = "rules"  # always deterministic


class IntegrationMode(StrEnum):
    MOCK = "mock"
    REAL = "real"  # reserved; refuses to start (§19.3)


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    EVAL = "eval"
    PRODUCTION = "production"


#: Placeholder values that must never reach a production deployment.
_PLACEHOLDER_PASSWORDS = frozenset(
    {"change-me-locally", "opspilot", "postgres", "password", "changeme", ""}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- LLM provider: Groq, OpenAI-compatible API (§4.2, ADR-025) ------
    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    groq_model: str = Field(default="openai/gpt-oss-120b", validation_alias="GROQ_MODEL")
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1", validation_alias="GROQ_BASE_URL"
    )
    llm_timeout_seconds: float = Field(
        default=60.0, gt=0, le=600, validation_alias="OPSPILOT_LLM_TIMEOUT_SECONDS"
    )

    # --- Agent behaviour -------------------------------------------------
    planner: PlannerMode = Field(default=PlannerMode.AUTO, validation_alias="OPSPILOT_PLANNER")
    max_retries: int = Field(default=2, ge=0, le=10, validation_alias="OPSPILOT_MAX_RETRIES")
    max_replans: int = Field(default=2, ge=0, le=10, validation_alias="OPSPILOT_MAX_REPLANS")
    max_steps: int = Field(default=25, ge=1, le=200, validation_alias="OPSPILOT_MAX_STEPS")
    run_deadline_seconds: int = Field(
        default=300, ge=1, validation_alias="OPSPILOT_RUN_DEADLINE_SECONDS"
    )
    retry_base_delay_ms: int = Field(
        default=250, ge=0, validation_alias="OPSPILOT_RETRY_BASE_DELAY_MS"
    )
    retry_max_delay_ms: int = Field(
        default=8_000, ge=0, validation_alias="OPSPILOT_RETRY_MAX_DELAY_MS"
    )

    # --- Integrations ----------------------------------------------------
    integrations: IntegrationMode = Field(
        default=IntegrationMode.MOCK, validation_alias="OPSPILOT_INTEGRATIONS"
    )
    tool_failure_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, validation_alias="OPSPILOT_TOOL_FAILURE_RATE"
    )
    seed: int = Field(default=1337, validation_alias="OPSPILOT_SEED")

    # --- HITL ------------------------------------------------------------
    approval_ttl_seconds: int = Field(
        default=86_400, ge=60, validation_alias="OPSPILOT_APPROVAL_TTL_SECONDS"
    )

    # --- Execution ownership (§2.4, DB-007) ---------------------------------
    lease_ttl_seconds: int = Field(default=30, ge=5, validation_alias="OPSPILOT_LEASE_TTL_SECONDS")
    heartbeat_interval_seconds: int = Field(
        default=10, ge=1, validation_alias="OPSPILOT_HEARTBEAT_INTERVAL_SECONDS"
    )

    # --- Database --------------------------------------------------------
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot"),
        validation_alias="DATABASE_URL",
    )
    postgres_password: SecretStr = Field(
        default=SecretStr("change-me-locally"), validation_alias="POSTGRES_PASSWORD"
    )

    # --- Backend ---------------------------------------------------------
    environment: Environment = Field(
        default=Environment.DEVELOPMENT, validation_alias="OPSPILOT_ENV"
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    cors_allow_origins: str = Field(
        default="http://localhost:3000", validation_alias="CORS_ALLOW_ORIGINS"
    )
    trace_payload_max_bytes: int = Field(
        default=16_384, ge=256, validation_alias="OPSPILOT_TRACE_PAYLOAD_MAX_BYTES"
    )

    # --- Auth (deliberately unimplemented; see §16.6) ---------------------
    auth_mode: str | None = Field(default=None, validation_alias="OPSPILOT_AUTH_MODE")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # --- Derived ---------------------------------------------------------
    @property
    def has_groq_key(self) -> bool:
        return bool(self.groq_api_key and self.groq_api_key.get_secret_value())

    @property
    def effective_planner(self) -> PlannerKind:
        """`auto` degrades to rules without a key; `llm` has already failed
        startup validation by this point, so it is safe to trust here."""
        if self.planner is PlannerMode.RULES:
            return PlannerKind.RULES
        if self.planner is PlannerMode.LLM:
            return PlannerKind.LLM
        return PlannerKind.LLM if self.has_groq_key else PlannerKind.RULES

    @property
    def database_password(self) -> str | None:
        """The password that actually grants database access.

        The fuse below checks this rather than POSTGRES_PASSWORD: the app
        connects via DATABASE_URL, and docker-compose composes POSTGRES_PASSWORD
        into it, so checking the URL covers both deployment shapes without
        falsely tripping on a deployment that sets only DATABASE_URL.
        """
        return urlsplit(self.database_url.get_secret_value()).password

    @property
    def lease_ttl(self) -> timedelta:
        return timedelta(seconds=self.lease_ttl_seconds)

    @property
    def heartbeat_interval(self) -> timedelta:
        return timedelta(seconds=self.heartbeat_interval_seconds)

    @property
    def budgets(self) -> Budgets:
        return Budgets(
            max_retries=self.max_retries,
            max_replans=self.max_replans,
            max_steps=self.max_steps,
            run_deadline_seconds=self.run_deadline_seconds,
        )

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def safe_dump(self) -> dict[str, Any]:
        """Loggable settings: every secret field omitted (§17.2)."""
        secret_fields = {
            name
            for name, field in type(self).model_fields.items()
            if field.annotation in (SecretStr, SecretStr | None)
        }
        return {k: v for k, v in self.model_dump(mode="json").items() if k not in secret_fields} | {
            "secrets_configured": sorted(
                name for name in secret_fields if getattr(self, name) is not None
            )
        }

    # --- Fail-fast startup validation (§17.3) -----------------------------
    def validate_runtime(self) -> None:
        """Refuse to serve traffic on a misconfiguration.

        These are fuses, not warnings. A paragraph in a README does not stop a
        system being deployed with no authentication.
        """
        problems: list[str] = []

        if self.planner is PlannerMode.LLM and not self.has_groq_key:
            problems.append(
                "OPSPILOT_PLANNER=llm requires GROQ_API_KEY "
                "(use 'auto' to degrade to the rule planner instead)"
            )

        if self.integrations is IntegrationMode.REAL:
            problems.append("OPSPILOT_INTEGRATIONS=real is not implemented; no real adapter exists")

        if self.heartbeat_interval_seconds * 2 > self.lease_ttl_seconds:
            problems.append(
                "OPSPILOT_HEARTBEAT_INTERVAL_SECONDS must be at most half of "
                "OPSPILOT_LEASE_TTL_SECONDS, or a single missed heartbeat makes the "
                "reconciler treat a healthy worker's run as orphaned (§2.4)"
            )

        if not self.database_url.get_secret_value().startswith(
            ("postgresql+asyncpg://", "postgresql+psycopg://")
        ):
            problems.append("DATABASE_URL must use an async driver (postgresql+asyncpg://)")

        if self.environment is Environment.PRODUCTION:
            if not self.auth_mode:
                problems.append(
                    "OPSPILOT_ENV=production requires OPSPILOT_AUTH_MODE; "
                    "OpsPilot has no authentication and must not be exposed (see §16.6)"
                )
            if (self.database_password or "") in _PLACEHOLDER_PASSWORDS:
                problems.append(
                    "OPSPILOT_ENV=production requires a real database password in DATABASE_URL"
                )
            if "*" in self.cors_allow_origins:
                problems.append("OPSPILOT_ENV=production forbids wildcard CORS_ALLOW_ORIGINS")

        if problems:
            raise ConfigurationError(
                "invalid configuration: " + "; ".join(problems), detail={"problems": problems}
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. FastAPI depends on this; tests construct `Settings`
    directly with overrides rather than mutating the environment."""
    return Settings()
