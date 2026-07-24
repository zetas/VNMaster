from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vnmaster.llm.changelog import (
    BudgetExceeded,
    ChangelogExtractor,
    ExtractionIncomplete,
    ExtractionResult,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "changelogs" / "v07_eternum.txt"


def _fake_response(payload: dict, stop_reason: str = "tool_use") -> MagicMock:
    response = MagicMock()
    block = MagicMock()
    block.type = "tool_use"
    block.input = payload
    response.content = [block]
    response.stop_reason = stop_reason
    response.usage.input_tokens = 800
    response.usage.cache_read_input_tokens = 600
    response.usage.cache_creation_input_tokens = 200
    response.usage.output_tokens = 120
    return response


def _truncated_response() -> MagicMock:
    """Simulates an Anthropic response truncated by hitting max_tokens."""
    response = MagicMock()
    # SDK parses incomplete JSON → input == {}
    block = MagicMock()
    block.type = "tool_use"
    block.input = {}
    response.content = [block]
    response.stop_reason = "max_tokens"
    response.usage.input_tokens = 800
    response.usage.cache_read_input_tokens = 0
    response.usage.cache_creation_input_tokens = 200
    response.usage.output_tokens = 1024
    return response


def test_extract_returns_versions_from_tool_use() -> None:
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        {"versions": [{
            "version": "0.7.0", "released_at": "2026-04-01",
            "renders": 800, "animations": 35, "words": 12000,
            "scenes": None, "new_locations": 1, "new_characters": 1,
            "bugfix_only": False, "summary_one_line": "Maya story + university",
        }]}
    )
    extractor = ChangelogExtractor(
        client=client, model="claude-haiku-4-5",
        budget_tracker=MagicMock(remaining_usd=lambda: 10.0, record=lambda *a, **k: None),
    )
    result = extractor.extract(FIXTURE.read_text(), title="Eternum")
    assert isinstance(result, ExtractionResult)
    assert result.method == "llm"
    assert len(result.versions) == 1
    assert result.versions[0]["renders"] == 800
    assert result.cost_usd > 0


def test_tool_schema_and_prompt_request_normalized_version() -> None:
    from vnmaster.llm.prompts import SYSTEM_PROMPT, TOOL_SCHEMA

    props = (
        TOOL_SCHEMA["input_schema"]["properties"]["versions"]["items"]["properties"]
    )
    assert "version_normalized" in props
    assert "normalized" in SYSTEM_PROMPT.lower()


def test_budget_exhausted_raises() -> None:
    client = MagicMock()
    budget = MagicMock()
    budget.remaining_usd.return_value = 0.0
    extractor = ChangelogExtractor(
        client=client, model="claude-haiku-4-5", budget_tracker=budget
    )
    with pytest.raises(BudgetExceeded):
        extractor.extract("anything", title="X")
    client.messages.create.assert_not_called()


def test_extractor_passes_cache_control_on_system_prompt() -> None:
    client = MagicMock()
    client.messages.create.return_value = _fake_response({"versions": []})
    extractor = ChangelogExtractor(
        client=client, model="claude-haiku-4-5",
        budget_tracker=MagicMock(remaining_usd=lambda: 10.0, record=lambda *a, **k: None),
    )
    extractor.extract("v0.1\n- 1 render", title="X")
    kwargs = client.messages.create.call_args.kwargs
    system_param = kwargs["system"]
    assert isinstance(system_param, list)
    assert system_param[0].get("cache_control") == {"type": "ephemeral"}


def test_messages_create_called_with_max_tokens_8192() -> None:
    """messages.create must use _MAX_OUTPUT_TOKENS (8192), not the old 1024."""
    client = MagicMock()
    client.messages.create.return_value = _fake_response({"versions": []})
    extractor = ChangelogExtractor(
        client=client, model="claude-haiku-4-5",
        budget_tracker=MagicMock(remaining_usd=lambda: 10.0, record=lambda *a, **k: None),
    )
    extractor.extract("v0.1\n- 1 render", title="X")
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 8192


def test_truncated_response_raises_extraction_incomplete() -> None:
    """stop_reason='max_tokens' must raise ExtractionIncomplete, never silently return []."""
    client = MagicMock()
    client.messages.create.return_value = _truncated_response()
    extractor = ChangelogExtractor(
        client=client, model="claude-haiku-4-5",
        budget_tracker=MagicMock(remaining_usd=lambda: 10.0, record=lambda *a, **k: None),
    )
    with pytest.raises(ExtractionIncomplete):
        extractor.extract("v0.1\n- many things", title="Long Changelog")


def test_non_truncated_tool_use_response_returns_versions() -> None:
    """Regression: a normal stop_reason='tool_use' response is parsed correctly."""
    client = MagicMock()
    client.messages.create.return_value = _fake_response(
        {"versions": [{"version": "1.0.0", "bugfix_only": False}]},
        stop_reason="tool_use",
    )
    extractor = ChangelogExtractor(
        client=client, model="claude-haiku-4-5",
        budget_tracker=MagicMock(remaining_usd=lambda: 10.0, record=lambda *a, **k: None),
    )
    result = extractor.extract("v1.0.0\n- big update", title="Game")
    assert result.method == "llm"
    assert len(result.versions) == 1
    assert result.versions[0]["version"] == "1.0.0"
