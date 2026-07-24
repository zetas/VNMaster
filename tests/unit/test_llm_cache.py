import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, ChangelogExtraction
from vnmaster.llm.cache import CachedExtractor
from vnmaster.llm.changelog import BudgetExceeded, ExtractionIncomplete, ExtractionResult


class _FakeExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, raw_changelog: str, *, title: str) -> ExtractionResult:
        self.calls += 1
        return ExtractionResult(
            method="llm",
            versions=[{"version": "0.1.0", "bugfix_only": False}],
            cost_usd=0.01,
        )


class _BudgetExhaustedExtractor:
    def extract(self, raw_changelog: str, *, title: str) -> ExtractionResult:
        raise BudgetExceeded("test budget exhausted")


class _TruncatedExtractor:
    """Simulates an LLM response that was cut off at max_tokens."""
    def extract(self, raw_changelog: str, *, title: str) -> ExtractionResult:
        raise ExtractionIncomplete("response truncated at max_tokens")


def _setup_db(tmp_path: Path):
    engine = create_engine_for(tmp_path / "v.db")
    Base.metadata.create_all(engine)
    return engine


def test_first_call_invokes_inner_extractor(tmp_path: Path) -> None:
    engine = _setup_db(tmp_path)
    inner = _FakeExtractor()
    cached = CachedExtractor(inner=inner, engine=engine, clock=lambda: 100)
    result = cached.extract_for(thread_id=1, raw="changelog body", title="X")
    assert inner.calls == 1
    assert result.method == "llm"


def test_second_call_same_content_uses_cache(tmp_path: Path) -> None:
    engine = _setup_db(tmp_path)
    inner = _FakeExtractor()
    cached = CachedExtractor(inner=inner, engine=engine, clock=lambda: 100)
    cached.extract_for(thread_id=1, raw="body", title="X")
    cached.extract_for(thread_id=1, raw="body", title="X")
    assert inner.calls == 1


def test_content_change_invalidates_cache(tmp_path: Path) -> None:
    engine = _setup_db(tmp_path)
    inner = _FakeExtractor()
    cached = CachedExtractor(inner=inner, engine=engine, clock=lambda: 100)
    cached.extract_for(thread_id=1, raw="body A", title="X")
    cached.extract_for(thread_id=1, raw="body B", title="X")
    assert inner.calls == 2


def test_cache_persists_to_db(tmp_path: Path) -> None:
    engine = _setup_db(tmp_path)
    inner = _FakeExtractor()
    cached = CachedExtractor(inner=inner, engine=engine, clock=lambda: 100)
    cached.extract_for(thread_id=1, raw="payload", title="X")
    with session_scope(engine) as s:
        row = s.execute(select(ChangelogExtraction)).scalar_one()
        assert row.f95_thread_id == 1
        expected_hash = hashlib.sha256(b"payload").hexdigest()
        assert row.content_hash == expected_hash
        assert json.loads(row.versions_json) == [
            {"version": "0.1.0", "bugfix_only": False}
        ]


def test_budget_exhausted_falls_back_to_regex(tmp_path: Path) -> None:
    """When the LLM raises BudgetExceeded, regex fallback must take over.

    Without this guard the digest pipeline crashes on the first call after the
    monthly cap is hit, instead of degrading gracefully as the spec requires.
    """
    engine = _setup_db(tmp_path)
    cached = CachedExtractor(
        inner=_BudgetExhaustedExtractor(), engine=engine, clock=lambda: 100
    )
    raw = "v0.7.0\n- 800 new renders\n- 35 animations\n"
    result = cached.extract_for(thread_id=42, raw=raw, title="X")
    assert result.method == "regex_fallback"
    assert result.cost_usd == 0.0
    assert len(result.versions) == 1
    assert result.versions[0]["version"] == "0.7.0"
    assert result.versions[0]["renders"] == 800

    # And the fallback result is cached — second call doesn't re-invoke.
    second = cached.extract_for(thread_id=42, raw=raw, title="X")
    assert second.method == "regex_fallback"
    assert second.cost_usd == 0.0


def test_extraction_incomplete_falls_back_to_regex(tmp_path: Path) -> None:
    """When inner.extract raises ExtractionIncomplete (truncated LLM output),
    extract_for must fall back to regex, cache the result, and not re-raise.

    This prevents a cached empty-versions result from poisoning digest deltas
    (the root cause of the max_tokens=1024 truncation bug).
    """
    engine = _setup_db(tmp_path)
    cached = CachedExtractor(
        inner=_TruncatedExtractor(), engine=engine, clock=lambda: 100
    )
    raw = "v0.8.0\n- 1200 new renders\n- 50 animations\n"
    result = cached.extract_for(thread_id=99, raw=raw, title="Truncated Game")
    assert result.method == "regex_fallback"
    assert result.cost_usd == 0.0
    assert len(result.versions) == 1
    assert result.versions[0]["version"] == "0.8.0"

    # Result is cached so a second call does not re-raise either.
    second = cached.extract_for(thread_id=99, raw=raw, title="Truncated Game")
    assert second.method == "regex_fallback"
