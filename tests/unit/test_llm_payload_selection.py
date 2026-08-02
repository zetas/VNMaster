from __future__ import annotations

import json
from typing import Any

import pytest

from vnmaster.downloads.gallery import GalleryCandidate
from vnmaster.downloads.models import PlannedArtifact
from vnmaster.llm.payload_selection import (
    PayloadSelectionError,
    PayloadSelectionInterpreter,
)


class FakeGenerator:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


def _artifact() -> PlannedArtifact:
    return PlannedArtifact(
        kind="game",
        title="Grandma's House Part 7",
        version="v0.108",
        thread_id=94140,
        thread_url="https://f95zone.to/threads/.94140/",
        group_name="Mac",
        platform="mac",
        host="GOFILE",
        locator="https://gofile.io/d/redacted",
        part="Part 7",
    )


def _candidates() -> tuple[GalleryCandidate, ...]:
    return (
        GalleryCandidate(
            "payload-0001", 1, "GrandmasHouse-0.108-mac-UpdatePatch.zip", 546376552
        ),
        GalleryCandidate(
            "payload-0002", 2, "GrandmasHouse-0.108-mac.zip", 5069551863
        ),
        GalleryCandidate(
            "payload-0003", 3, "GrandmasHouse-0.108-pc-UpdatePatch.zip", 583099102
        ),
        GalleryCandidate(
            "payload-0004", 4, "GrandmasHouse-0.108-pc.zip", 5105946737
        ),
        GalleryCandidate(
            "payload-0005",
            5,
            "moonbox.grandmashouse-108P-universal-release.apk",
            1223607229,
        ),
    )


def test_selects_only_matching_full_build_and_never_sends_urls() -> None:
    generator = FakeGenerator(
        {
            "selected_candidate_ids": ["payload-0002"],
            "confidence": 0.99,
            "reason": "The filename identifies the full Mac v0.108 build.",
            "ambiguities": [],
        }
    )
    selected = PayloadSelectionInterpreter(generator).select(_artifact(), _candidates())

    assert selected == (_candidates()[1],)
    prompt = json.loads(generator.calls[0]["user_prompt"])
    assert prompt["artifact_contract"]["platform"] == "mac"
    assert [item["candidate_id"] for item in prompt["provider_candidates"]] == [
        "payload-0001",
        "payload-0002",
        "payload-0003",
        "payload-0004",
        "payload-0005",
    ]
    assert "gofile.io" not in generator.calls[0]["user_prompt"]


@pytest.mark.parametrize("candidate_id", ["payload-0001", "payload-0004", "payload-0005"])
def test_deterministic_contract_rejects_unsafe_model_choice(candidate_id: str) -> None:
    generator = FakeGenerator(
        {
            "selected_candidate_ids": [candidate_id],
            "confidence": 0.99,
            "reason": "synthetic model choice",
            "ambiguities": [],
        }
    )

    with pytest.raises(PayloadSelectionError):
        PayloadSelectionInterpreter(generator).select(_artifact(), _candidates())


def test_low_confidence_or_ambiguous_choice_fails_closed() -> None:
    generator = FakeGenerator(
        {
            "selected_candidate_ids": ["payload-0002"],
            "confidence": 0.72,
            "reason": "The naming is unclear.",
            "ambiguities": ["Could be an incremental build."],
        }
    )

    with pytest.raises(PayloadSelectionError, match="confidence"):
        PayloadSelectionInterpreter(generator).select(_artifact(), _candidates())


def test_unknown_candidate_id_is_rejected() -> None:
    generator = FakeGenerator(
        {
            "selected_candidate_ids": ["payload-9999"],
            "confidence": 0.99,
            "reason": "synthetic unknown selection",
            "ambiguities": [],
        }
    )

    with pytest.raises(PayloadSelectionError, match="unknown provider candidate"):
        PayloadSelectionInterpreter(generator).select(_artifact(), _candidates())
