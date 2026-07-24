import os
from pathlib import Path

import pytest
from anthropic import Anthropic

from vnmaster.llm.budget import InMemoryBudget
from vnmaster.llm.changelog import ChangelogExtractor

FIXTURE = Path(__file__).parent.parent / "fixtures" / "changelogs" / "v07_eternum.txt"


@pytest.mark.live_llm
@pytest.mark.skipif(
    os.environ.get("VNMASTER_LIVE_LLM") != "1",
    reason="set VNMASTER_LIVE_LLM=1 to hit the real Anthropic API",
)
def test_live_extraction_matches_expectations() -> None:
    client = Anthropic()
    extractor = ChangelogExtractor(
        client=client,
        model="claude-haiku-4-5",
        budget_tracker=InMemoryBudget(cap_usd=1.0),
    )
    result = extractor.extract(FIXTURE.read_text(), title="Eternum")
    assert result.method == "llm"
    v = result.versions[0]
    assert v["version"] == "0.7.0"
    assert v["renders"] == 800
    assert v["animations"] == 35
