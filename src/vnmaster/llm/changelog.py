"""Anthropic-based changelog extractor.

Uses prompt caching on the system block so per-call cost is dominated by the
short user message (the changelog body).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from anthropic import Anthropic
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolChoiceToolParam,
    ToolParam,
)

from vnmaster.llm.prompts import SYSTEM_PROMPT, TOOL_SCHEMA


# Pricing — claude-haiku-4-5 per 1M tokens (USD). Update if Anthropic re-prices.
_INPUT_PER_M = 1.00
_OUTPUT_PER_M = 5.00
_CACHE_WRITE_PER_M = 1.25
_CACHE_READ_PER_M = 0.10

# Output token cap.  Real multi-version changelogs can easily exceed 1024 tokens
# when serialised as structured tool-call JSON.  8192 is sufficient for the
# largest F95Zone changelogs encountered in production; Anthropic bills on
# actual output tokens, not the cap, so raising this has no cost downside.
_MAX_OUTPUT_TOKENS = 8192


class BudgetExceeded(Exception):
    pass


class ExtractionIncomplete(Exception):
    """Raised when the API truncates the response before the tool call completes.

    Signals that ``stop_reason == "max_tokens"`` was returned; the partial
    ``input`` dict from the truncated tool-call JSON must NOT be cached as a
    successful empty extraction.  Callers should fall back to regex extraction.
    """
    pass


class BudgetTracker(Protocol):
    def remaining_usd(self) -> float: ...
    def record(self, cost_usd: float) -> None: ...


@dataclass(frozen=True)
class ExtractionResult:
    method: str  # 'llm' | 'regex_fallback'
    versions: list[dict[str, Any]]
    cost_usd: float


class ChangelogExtractor:
    def __init__(
        self, client: Anthropic, model: str, budget_tracker: BudgetTracker
    ) -> None:
        self._client = client
        self._model = model
        self._budget = budget_tracker

    def extract(self, raw_changelog: str, *, title: str) -> ExtractionResult:
        if self._budget.remaining_usd() <= 0:
            raise BudgetExceeded("monthly LLM budget exhausted")

        system: list[TextBlockParam] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        tools = [cast(ToolParam, TOOL_SCHEMA)]
        tool_choice: ToolChoiceToolParam = {
            "type": "tool",
            "name": "record_changelog",
        }
        messages: list[MessageParam] = [
            {
                "role": "user",
                "content": f"Title: {title}\n\nChangelog:\n{raw_changelog}",
            }
        ]
        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
        )

        if getattr(response, "stop_reason", None) == "max_tokens":
            raise ExtractionIncomplete(
                f"Anthropic truncated the tool-call output at {_MAX_OUTPUT_TOKENS} "
                "output tokens; the partial JSON cannot be used as an extraction result."
            )

        versions = _extract_tool_output(response)
        cost = _compute_cost(response)
        self._budget.record(cost)

        return ExtractionResult(method="llm", versions=versions, cost_usd=cost)


def _extract_tool_output(response: Any) -> list[dict[str, Any]]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            payload = block.input
            return list(payload.get("versions", []))
    return []


def _compute_cost(response: Any) -> float:
    u = response.usage
    return (
        getattr(u, "input_tokens", 0) / 1_000_000 * _INPUT_PER_M
        + getattr(u, "cache_creation_input_tokens", 0) / 1_000_000 * _CACHE_WRITE_PER_M
        + getattr(u, "cache_read_input_tokens", 0) / 1_000_000 * _CACHE_READ_PER_M
        + getattr(u, "output_tokens", 0) / 1_000_000 * _OUTPUT_PER_M
    )
