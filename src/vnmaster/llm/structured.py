"""Provider-neutral JSON-schema constrained generation over HTTP APIs."""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from vnmaster.config import ConfigError, ForumParserConfig, Secrets


class StructuredOutputError(RuntimeError):
    pass


class StructuredOutputClient:
    def __init__(
        self,
        settings: ForumParserConfig,
        secrets: Secrets,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._api_key = _provider_api_key(settings.provider, secrets)
        self._client = client or httpx.Client(timeout=settings.timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "StructuredOutputClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        provider = self.settings.provider
        if provider == "openai":
            text = self._openai(system_prompt, user_prompt, schema, schema_name)
        elif provider == "anthropic":
            text = self._anthropic(system_prompt, user_prompt, schema)
        elif provider == "ollama":
            text = self._ollama(system_prompt, user_prompt, schema)
        else:
            text = self._openai_compatible(
                system_prompt, user_prompt, schema, schema_name
            )
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(
                f"{provider} returned invalid JSON despite schema constraints"
            ) from exc
        if not isinstance(value, dict):
            raise StructuredOutputError(f"{provider} returned a non-object JSON value")
        return value

    def _openai(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        response = self._client.post(
            _endpoint(self.settings.base_url or "https://api.openai.com/v1", "responses"),
            headers={
                "Authorization": f"Bearer {self._required_key()}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.model,
                "instructions": system_prompt,
                "input": user_prompt,
                "max_output_tokens": self.settings.max_output_tokens,
                # "none" means send no reasoning block; non-reasoning models
                # reject the field outright.
                **(
                    {"reasoning": {"effort": self.settings.reasoning_effort}}
                    if self.settings.reasoning_effort != "none"
                    else {}
                ),
                "store": False,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": _openai_strict_schema(schema),
                    }
                },
            },
        )
        payload = _checked_json(response, "OpenAI")
        for output in payload.get("output", []):
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    if isinstance(content.get("text"), str):
                        return str(content["text"])
                if isinstance(content, dict) and content.get("type") == "refusal":
                    raise StructuredOutputError("OpenAI refused the manifest extraction")
        raise StructuredOutputError("OpenAI response did not contain structured output text")

    def _anthropic(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> str:
        response = self._client.post(
            _endpoint(self.settings.base_url or "https://api.anthropic.com", "v1/messages"),
            headers={
                "x-api-key": self._required_key(),
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.model,
                "max_tokens": self.settings.max_output_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
                "output_config": {
                    "format": {"type": "json_schema", "schema": schema}
                },
            },
        )
        payload = _checked_json(response, "Anthropic")
        for content in payload.get("content", []):
            if isinstance(content, dict) and content.get("type") == "text":
                if isinstance(content.get("text"), str):
                    return str(content["text"])
        raise StructuredOutputError("Anthropic response did not contain structured output text")

    def _ollama(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = self._client.post(
            _endpoint(self.settings.base_url or "http://localhost:11434", "api/chat"),
            headers=headers,
            json={
                "model": self.settings.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "think": self.settings.thinking,
                "format": schema,
                "options": {
                    "temperature": 0,
                    "num_predict": self.settings.max_output_tokens,
                },
            },
        )
        payload = _checked_json(response, "Ollama")
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return str(message["content"])
        raise StructuredOutputError("Ollama response did not contain a message")

    def _openai_compatible(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> str:
        mode = self.settings.constraint_mode
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.settings.max_output_tokens,
            "temperature": 0,
        }
        if mode == "vllm":
            body["structured_outputs"] = {"json": schema}
        elif mode == "llama_cpp":
            body["response_format"] = {"type": "json_schema", "schema": schema}
        else:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        response = self._client.post(
            _endpoint(
                self.settings.base_url or "http://localhost:8000/v1",
                "chat/completions",
            ),
            headers=headers,
            json=body,
        )
        payload = _checked_json(response, "OpenAI-compatible server")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return str(message["content"])
        raise StructuredOutputError(
            "OpenAI-compatible response did not contain a message"
        )

    def _required_key(self) -> str:
        if not self._api_key:
            raise ConfigError(
                f"No API key is configured for forum parser provider "
                f"{self.settings.provider!r}"
            )
        return self._api_key


def _provider_api_key(provider: str, secrets: Secrets) -> str | None:
    if provider == "openai":
        return secrets.openai_api_key or os.environ.get("OPENAI_API_KEY")
    if provider == "anthropic":
        return secrets.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
    return secrets.forum_parser_api_key or os.environ.get("VNMASTER_FORUM_PARSER_API_KEY")


def _endpoint(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def _checked_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _provider_error_detail(response)
        suffix = f": {detail}" if detail else ""
        raise StructuredOutputError(
            f"{provider} structured-output request failed with HTTP "
            f"{response.status_code}{suffix}"
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise StructuredOutputError(f"{provider} returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise StructuredOutputError(f"{provider} returned an invalid response object")
    return payload


def _provider_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return ""
    concise = " ".join(message.split())[:500]
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", concise)


def _openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate Pydantic discriminated unions to OpenAI's JSON Schema subset."""

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "discriminator":
                continue
            normalized["anyOf" if key == "oneOf" else key] = normalize(item)
        return normalized

    normalized = normalize(schema)
    if not isinstance(normalized, dict):
        raise StructuredOutputError("OpenAI schema normalization returned a non-object")
    return normalized
