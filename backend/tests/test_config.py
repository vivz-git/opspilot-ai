"""Configuration fuses and planner selection (§17)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.agent.state import PlannerKind
from app.config import Environment, IntegrationMode, PlannerMode, Settings
from app.errors import ConfigurationError

pytestmark = [pytest.mark.unit]

KEY = "test-key-not-a-real-credential"


def settings(**overrides: object) -> Settings:
    """Construct settings without reading the developer's .env."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestPlannerSelection:
    def test_auto_degrades_to_rules_without_a_key(self) -> None:
        """A contributor with no API key must still be able to run everything."""
        s = settings(OPSPILOT_PLANNER=PlannerMode.AUTO)
        s.validate_runtime()
        assert s.effective_planner is PlannerKind.RULES

    def test_auto_uses_the_llm_when_a_key_is_present(self) -> None:
        s = settings(OPSPILOT_PLANNER=PlannerMode.AUTO, ANTHROPIC_API_KEY=KEY)
        assert s.effective_planner is PlannerKind.LLM

    def test_rules_ignores_a_present_key(self) -> None:
        s = settings(OPSPILOT_PLANNER=PlannerMode.RULES, ANTHROPIC_API_KEY=KEY)
        assert s.effective_planner is PlannerKind.RULES

    def test_explicit_llm_without_a_key_fails_fast(self) -> None:
        """Explicit request fails; automatic selection degrades. That
        distinction is the whole point of having three modes."""
        with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
            settings(OPSPILOT_PLANNER=PlannerMode.LLM).validate_runtime()


class TestStartupFuses:
    def test_real_integrations_refuse_to_start(self) -> None:
        with pytest.raises(ConfigurationError, match="not implemented"):
            settings(OPSPILOT_INTEGRATIONS=IntegrationMode.REAL).validate_runtime()

    def test_production_requires_an_auth_mode(self) -> None:
        """OpsPilot has no authentication; a fuse is worth more than a
        paragraph in a README (§16.6)."""
        with pytest.raises(ConfigurationError, match="OPSPILOT_AUTH_MODE"):
            settings(
                OPSPILOT_ENV=Environment.PRODUCTION, POSTGRES_PASSWORD="a-real-secret"
            ).validate_runtime()

    def test_production_refuses_a_placeholder_password(self) -> None:
        with pytest.raises(ConfigurationError, match="POSTGRES_PASSWORD"):
            settings(
                OPSPILOT_ENV=Environment.PRODUCTION, OPSPILOT_AUTH_MODE="oidc"
            ).validate_runtime()

    def test_production_refuses_wildcard_cors(self) -> None:
        with pytest.raises(ConfigurationError, match="CORS"):
            settings(
                OPSPILOT_ENV=Environment.PRODUCTION,
                OPSPILOT_AUTH_MODE="oidc",
                POSTGRES_PASSWORD="a-real-secret",
                CORS_ALLOW_ORIGINS="*",
            ).validate_runtime()

    def test_a_fully_configured_production_environment_starts(self) -> None:
        settings(
            OPSPILOT_ENV=Environment.PRODUCTION,
            OPSPILOT_AUTH_MODE="oidc",
            POSTGRES_PASSWORD="a-real-secret",
            CORS_ALLOW_ORIGINS="https://ops.example.com",
        ).validate_runtime()

    def test_a_sync_database_url_fails_loudly_at_startup(self) -> None:
        with pytest.raises(ConfigurationError, match="async driver"):
            settings(
                DATABASE_URL="postgresql://opspilot:opspilot@localhost:5432/opspilot"
            ).validate_runtime()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("OPSPILOT_MAX_RETRIES", -1),
            ("OPSPILOT_MAX_RETRIES", 11),
            ("OPSPILOT_MAX_STEPS", 0),
            ("OPSPILOT_RUN_DEADLINE_SECONDS", 0),
            ("OPSPILOT_TOOL_FAILURE_RATE", 1.5),
        ],
    )
    def test_budgets_cannot_be_unbounded_by_typo(self, field: str, value: object) -> None:
        """An unbounded-by-typo budget defeats §10.5."""
        with pytest.raises(ValidationError):
            settings(**{field: value})


class TestSecretHandling:
    def test_secrets_are_not_in_the_loggable_dump(self) -> None:
        dump = settings(ANTHROPIC_API_KEY=KEY).safe_dump()
        assert "anthropic_api_key" not in dump
        assert "database_url" not in dump
        assert "postgres_password" not in dump
        assert KEY not in str(dump)

    def test_safe_dump_still_reports_which_secrets_are_configured(self) -> None:
        dump = settings(ANTHROPIC_API_KEY=KEY).safe_dump()
        assert "anthropic_api_key" in dump["secrets_configured"]

    def test_secrets_are_hidden_in_repr(self) -> None:
        s = settings(ANTHROPIC_API_KEY=KEY, POSTGRES_PASSWORD="hunter2")
        assert KEY not in repr(s)
        assert "hunter2" not in repr(s)

    def test_secret_fields_are_secretstr(self) -> None:
        s = settings(ANTHROPIC_API_KEY=KEY)
        assert isinstance(s.anthropic_api_key, SecretStr)
        assert isinstance(s.database_url, SecretStr)


class TestDerivedValues:
    def test_budgets_are_derived_from_configuration(self) -> None:
        b = settings(OPSPILOT_MAX_RETRIES=3, OPSPILOT_MAX_STEPS=10).budgets
        assert (b.max_retries, b.max_steps) == (3, 10)

    def test_cors_origins_are_parsed_into_a_list(self) -> None:
        s = settings(CORS_ALLOW_ORIGINS="http://localhost:3000, https://ops.example.com")
        assert s.cors_origins == ["http://localhost:3000", "https://ops.example.com"]

    def test_defaults_match_the_documented_env_example(self) -> None:
        s = settings()
        assert (s.max_retries, s.max_replans, s.max_steps) == (2, 2, 25)
        assert s.run_deadline_seconds == 300
        assert s.retry_base_delay_ms == 250
        assert s.retry_max_delay_ms == 8_000
        assert s.approval_ttl_seconds == 86_400
        assert s.tool_failure_rate == 0.0
        assert s.seed == 1337
        assert s.integrations is IntegrationMode.MOCK
        assert s.trace_payload_max_bytes == 16_384
