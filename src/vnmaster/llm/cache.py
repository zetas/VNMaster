"""SQLite-backed cache: extract-once-per-content-hash.

Falls back to regex extraction when the LLM raises BudgetExceeded or any
anthropic SDK error, so a digest run never hard-fails on LLM problems.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Protocol

from anthropic import APIError
from sqlalchemy import Engine, select

from vnmaster.db.engine import session_scope
from vnmaster.db.models import ChangelogExtraction
from vnmaster.llm.changelog import BudgetExceeded, ExtractionIncomplete, ExtractionResult
from vnmaster.llm.fallback_regex import extract_with_regex
from vnmaster.logging_setup import get_logger

log = get_logger(__name__)


class InnerExtractor(Protocol):
    def extract(self, raw_changelog: str, *, title: str) -> ExtractionResult: ...


class CachedExtractor:
    def __init__(
        self,
        inner: InnerExtractor,
        engine: Engine,
        clock: Callable[[], int],
    ) -> None:
        self._inner = inner
        self._engine = engine
        self._clock = clock

    def extract_for(
        self, thread_id: int, raw: str, title: str
    ) -> ExtractionResult:
        content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with session_scope(self._engine) as s:
            existing = s.execute(
                select(ChangelogExtraction).where(
                    ChangelogExtraction.f95_thread_id == thread_id,
                    ChangelogExtraction.content_hash == content_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return ExtractionResult(
                    method=existing.extraction_method,
                    versions=json.loads(existing.versions_json),
                    cost_usd=0.0,
                )

        try:
            result = self._inner.extract(raw, title=title)
        except BudgetExceeded:
            log.warning(
                "LLM budget exhausted for thread %d; using regex fallback",
                thread_id,
            )
            result = ExtractionResult(
                method="regex_fallback",
                versions=extract_with_regex(raw)["versions"],
                cost_usd=0.0,
            )
        except ExtractionIncomplete:
            log.warning(
                "LLM extraction truncated for thread %d; using regex fallback",
                thread_id,
            )
            result = ExtractionResult(
                method="regex_fallback",
                versions=extract_with_regex(raw)["versions"],
                cost_usd=0.0,
            )
        except APIError as e:
            log.warning(
                "Anthropic API error for thread %d (%s); using regex fallback",
                thread_id, type(e).__name__,
            )
            result = ExtractionResult(
                method="regex_fallback",
                versions=extract_with_regex(raw)["versions"],
                cost_usd=0.0,
            )

        with session_scope(self._engine) as s:
            s.add(
                ChangelogExtraction(
                    f95_thread_id=thread_id,
                    content_hash=content_hash,
                    extraction_method=result.method,
                    versions_json=json.dumps(result.versions),
                    extracted_at=self._clock(),
                )
            )
        return result
