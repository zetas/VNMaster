from __future__ import annotations

import pytest

from vnmaster.downloads.models import DownloadPlan, PlannedArtifact, ThreadInfo
from vnmaster.downloads.workflow import select_optional_artifacts


def _artifact(title: str, kind: str = "addon") -> PlannedArtifact:
    return PlannedArtifact(
        kind=kind,  # type: ignore[arg-type]
        title=title,
        version="v1",
        thread_id=1,
        thread_url="https://f95zone.to/threads/.1/",
        group_name="Mac" if kind == "game" else "Patch",
        platform="mac" if kind == "game" else None,
        host="MEGA",
        locator="https://f95zone.to/masked/mega.nz/x",
    )


def _candidate_plan() -> DownloadPlan:
    game = ThreadInfo(1, "A Game", "v1", None, "https://f95zone.to/threads/.1/", ())
    return DownloadPlan(
        game,
        (_artifact("A Game", "game"), _artifact("Patch"), _artifact("Walkthrough")),
    )


def test_select_optional_artifacts_uses_one_based_numbers() -> None:
    plan = select_optional_artifacts(_candidate_plan(), (2,))
    assert [artifact.title for artifact in plan.artifacts] == ["A Game", "Walkthrough"]


def test_select_optional_artifacts_rejects_out_of_range_number() -> None:
    with pytest.raises(ValueError, match="out of range"):
        select_optional_artifacts(_candidate_plan(), (3,))
