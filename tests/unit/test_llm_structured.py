from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from vnmaster.config import ForumParserConfig, Secrets
from vnmaster.llm.structured import StructuredOutputClient, StructuredOutputError


def _secrets() -> Secrets:
    return Secrets(
        discord_bot_token="synthetic-discord-token",
        anthropic_api_key="synthetic-anthropic-key",
        openai_api_key="synthetic-openai-key",
        forum_parser_api_key="synthetic-local-key",
    )


def _call(
    settings: ForumParserConfig,
    response_body: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response_body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = StructuredOutputClient(settings, _secrets(), client=http)
    result = client.generate(
        system_prompt="system",
        user_prompt="user",
        schema=schema or {"type": "object", "additionalProperties": False},
        schema_name="manifest",
    )
    request_body = json.loads(seen[0].content)
    return result, request_body, seen[0]


def test_openai_uses_responses_strict_json_schema() -> None:
    result, body, request = _call(
        ForumParserConfig(enabled=True, provider="openai", model="gpt-test"),
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                }
            ]
        },
    )
    assert result == {"ok": True}
    assert request.url.path == "/v1/responses"
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "medium"}
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["type"] == "object"


def test_openai_normalizes_discriminated_union_schema() -> None:
    _, body, _ = _call(
        ForumParserConfig(enabled=True, provider="openai", model="gpt-test"),
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                }
            ]
        },
        schema={
            "type": "object",
            "properties": {
                "item": {
                    "oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}],
                    "discriminator": {
                        "propertyName": "kind",
                        "mapping": {"a": "#/$defs/A", "b": "#/$defs/B"},
                    },
                }
            },
            "additionalProperties": False,
        },
    )
    item = body["text"]["format"]["schema"]["properties"]["item"]
    assert "oneOf" not in item
    assert "discriminator" not in item
    assert item["anyOf"] == [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]


def test_anthropic_uses_output_config_schema() -> None:
    result, body, request = _call(
        ForumParserConfig(enabled=True, provider="anthropic", model="claude-test"),
        {"content": [{"type": "text", "text": '{"ok": true}'}]},
    )
    assert result == {"ok": True}
    assert request.url.path == "/v1/messages"
    assert body["output_config"]["format"]["type"] == "json_schema"


def test_ollama_uses_native_format_schema() -> None:
    result, body, request = _call(
        ForumParserConfig(enabled=True, provider="ollama", model="qwen-test"),
        {"message": {"content": '{"ok": true}'}},
    )
    assert result == {"ok": True}
    assert request.url.path == "/api/chat"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"]["type"] == "object"
    assert body["options"]["num_predict"] == 8192


@pytest.mark.parametrize(
    ("mode", "field"),
    [("auto", "response_format"), ("openai", "response_format"), ("vllm", "structured_outputs"), ("llama_cpp", "response_format")],
)
def test_openai_compatible_constraint_modes(mode: str, field: str) -> None:
    result, body, request = _call(
        ForumParserConfig(
            enabled=True,
            provider="openai_compatible",
            model="local-test",
            constraint_mode=mode,  # type: ignore[arg-type]
        ),
        {"choices": [{"message": {"content": '{"ok": true}'}}]},
    )
    assert result == {"ok": True}
    assert request.url.path == "/v1/chat/completions"
    assert field in body
    if mode == "vllm":
        assert body["structured_outputs"]["json"]["type"] == "object"
    elif mode == "llama_cpp":
        assert body["response_format"]["schema"]["type"] == "object"
    else:
        assert body["response_format"]["json_schema"]["strict"] is True


def test_http_error_includes_safe_provider_diagnostic() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid schema at path sk-synthetic-secret-value"
                }
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = StructuredOutputClient(
        ForumParserConfig(enabled=True, provider="openai", model="gpt-test"),
        _secrets(),
        client=http,
    )
    with pytest.raises(
        StructuredOutputError,
        match=r"Invalid schema at path \[redacted\]",
    ):
        client.generate(
            system_prompt="system",
            user_prompt="user",
            schema={"type": "object", "additionalProperties": False},
            schema_name="manifest",
        )


def test_openai_omits_reasoning_block_when_effort_is_none() -> None:
    _, body, _ = _call(
        ForumParserConfig(
            enabled=True, provider="openai", model="gpt-test", reasoning_effort="none"
        ),
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                }
            ]
        },
    )
    assert "reasoning" not in body
