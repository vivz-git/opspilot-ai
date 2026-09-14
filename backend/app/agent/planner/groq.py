"""The Groq transport: one structured chat completion over the OpenAI-compatible API.

This is the only module in the application that talks to an LLM provider.
It implements `StructuredCompletionClient` for the `LLMPlanner` and nothing
else: no retries (the planner's own bound is one repair turn; anything beyond
that is `recover`'s business), no streaming, no tool calling, no logging of
request or response bodies. The API key is read from `Settings` by the
composition root and handed in as a `SecretStr`; it is placed in one header
and never appears in an exception message, a log line or a trace.

Model and endpoint are configuration (`GROQ_MODEL`, `GROQ_BASE_URL`). The
default model, `openai/gpt-oss-120b`, supports Groq's JSON-schema structured
outputs, which is what makes "the response is a plan or it is rejected"
cheap to enforce.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
from pydantic import SecretStr

from app.agent.planner.llm import LLMProviderError

__all__ = [
    "DEFAULT_GROQ_BASE_URL",
    "GroqStructuredClient",
]

DEFAULT_GROQ_BASE_URL: Final = "https://api.groq.com/openai/v1"
_CHAT_COMPLETIONS: Final = "/chat/completions"
#: Enough for the largest plan the budgets admit, with room for rationale.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 4096


class GroqStructuredClient:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str = DEFAULT_GROQ_BASE_URL,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.get_secret_value():
            raise LLMProviderError("GROQ_API_KEY is empty")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        #: Injected in tests (`httpx.MockTransport`); `None` means real HTTP.
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    def request_body(
        self, *, system: str, user: str, schema_name: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """The exact request the provider receives (exposed for tests)."""
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema},
            },
            "temperature": 0,
            "max_completion_tokens": self._max_output_tokens,
        }

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> str:
        body = self.request_body(system=system, user=user, schema_name=schema_name, schema=schema)
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(_CHAT_COMPLETIONS, json=body, headers=headers)
        except httpx.HTTPError as exc:
            # The exception text can carry the URL but never the header; the
            # class name is enough to classify the failure.
            raise LLMProviderError(
                f"groq request failed: {type(exc).__name__}",
                detail={"model": self._model, "error": type(exc).__name__},
            ) from exc

        if response.status_code != 200:
            raise LLMProviderError(
                f"groq returned HTTP {response.status_code}",
                detail={
                    "model": self._model,
                    "status": response.status_code,
                    "retry_after": response.headers.get("retry-after"),
                    "error": _provider_error_message(response),
                },
            )
        return _completion_text(response)


def _provider_error_message(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message)[:300] if message is not None else None


def _completion_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMProviderError("groq returned a non-JSON body") from exc
    try:
        choices = payload["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMProviderError("groq response carried no completion") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMProviderError("groq completion was empty")
    return content
