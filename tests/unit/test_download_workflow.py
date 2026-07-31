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


def _artifact_p(kind: str, title: str, part: str | None = None) -> PlannedArtifact:
    return PlannedArtifact(
        kind=kind, title=title, version="v1", thread_id=1,
        thread_url="https://f95zone.to/threads/.1/", group_name=title,
        platform=None, host="MEGA", locator="https://x", part=part,
    )


def _plan_p(*artifacts: PlannedArtifact) -> DownloadPlan:
    game = ThreadInfo(1, "G", "v1", None, "https://f95zone.to/threads/.1/", ())
    return DownloadPlan(game=game, artifacts=artifacts)


def test_all_game_artifacts_are_required() -> None:
    plan = _plan_p(
        _artifact_p("game", "G", part="Part 1"),
        _artifact_p("game", "G", part="Part 2"),
        _artifact_p("addon", "G walkthrough"),
    )
    result = select_optional_artifacts(plan, ())
    assert [a.part for a in result.artifacts if a.kind == "game"] == [
        "Part 1", "Part 2",
    ]
    assert all(a.kind == "game" for a in result.artifacts)


def test_optional_numbering_counts_addons_only() -> None:
    plan = _plan_p(
        _artifact_p("game", "G", part="Part 1"),
        _artifact_p("game", "G", part="Part 2"),
        _artifact_p("addon", "walkthrough"),
        _artifact_p("addon", "gallery unlocker"),
    )
    result = select_optional_artifacts(plan, (2,))
    addons = [a for a in result.artifacts if a.kind == "addon"]
    assert [a.title for a in addons] == ["gallery unlocker"]
