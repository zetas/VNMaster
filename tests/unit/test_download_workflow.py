from __future__ import annotations

import json

import httpx
import pytest

from vnmaster.downloads.models import DownloadPlan, PlannedArtifact, ThreadInfo
from vnmaster.downloads.workflow import (
    ProviderPolicyError,
    ThreadDiscovery,
    apply_provider_policy,
    available_providers,
    provider_name,
    build_plan_from_discovery,
    discover_thread,
    select_optional_artifacts,
)


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


def test_provider_policy_removes_disabled_primary_and_promotes_fallback() -> None:
    from vnmaster.downloads.models import DownloadMirror

    artifact = _artifact("A Game", "game")
    artifact = PlannedArtifact(
        **{
            **artifact.__dict__,
            "alternate_mirrors": (
                DownloadMirror("PixelDrain", "https://pixeldrain.com/u/game"),
                DownloadMirror("MediaFire", "https://mediafire.com/game"),
            ),
        }
    )
    plan = DownloadPlan(_candidate_plan().game, (artifact,))

    filtered = apply_provider_policy(plan, ("PixelDrain", "MediaFire"))

    assert filtered.artifacts[0].host == "PIXELDRAIN"
    assert [mirror.name for mirror in filtered.artifacts[0].mirrors] == [
        "PIXELDRAIN",
        "MEDIAFIRE",
    ]
    assert available_providers(plan) == ("MEGA", "PIXELDRAIN", "MEDIAFIRE")


def test_provider_policy_rejects_artifact_with_no_enabled_provider() -> None:
    with pytest.raises(ProviderPolicyError, match="A Game"):
        apply_provider_policy(_candidate_plan(), ("PixelDrain",))


def test_provider_name_uses_locator_instead_of_descriptive_link_caption() -> None:
    from vnmaster.downloads.models import DownloadMirror

    assert provider_name(
        DownloadMirror("Part 2+", "https://mega.nz/file/patch2")
    ) == "MEGA"
    assert provider_name(
        DownloadMirror(
            "Download here",
            "https://f95zone.to/masked/drive.proton.me/example",
        )
    ) == "PROTONDRIVE"


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


def test_build_plan_no_addons_excludes_embedded_addons() -> None:
    from vnmaster.downloads.models import DownloadGroup, DownloadMirror

    game = ThreadInfo(
        1,
        "G",
        "v1",
        None,
        "https://f95zone.to/threads/.1/",
        (
            DownloadGroup("Mac", (DownloadMirror("MEGA", "game"),)),
            DownloadGroup("Walkthrough", (DownloadMirror("ATTACHMENT", "guide"),)),
        ),
    )
    plan = build_plan_from_discovery(
        ThreadDiscovery(game, (), ()),
        platform_priority=["mac"],
        preferred_hosts=["mega"],
        include_addons=False,
    )
    assert [artifact.kind for artifact in plan.artifacts] == ["game"]


def test_discover_thread_loads_addon_thread_linked_from_game_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/full/42":
            return httpx.Response(
                200,
                json={
                    "name": "A Game",
                    "version": "v1.2",
                    "downloads": json.dumps(
                        [
                            ["Mac", [["MEGA", "https://mega.nz/file/game"]]],
                            [
                                "WALKTHROUGH MOD",
                                [["MOD", "https://f95zone.to/threads/mod.99/"]],
                            ],
                        ]
                    ),
                },
            )
        if request.url.path == "/full/99":
            return httpx.Response(
                200,
                json={"name": "A Game Walkthrough Mod", "version": "v1.2", "downloads": []},
            )
        if request.url.path == "/threads/.99/":
            return httpx.Response(
                200,
                text=(
                    '<article class="message-threadStarterPost"><div class="bbWrapper">'
                    '<a href="https://attachments.f95zone.to/2026/07/mod-v1.2.zip">'
                    "mod</a></div></article>"
                ),
            )
        if request.url.path == "/search/":
            return httpx.Response(
                200,
                text=(
                    '<form action="/search/search">'
                    '<input name="_xfToken" value="token"></form>'
                ),
            )
        if request.url.path == "/search/search":
            return httpx.Response(200, text="")
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        discovery = discover_thread("42", client=client)
    assert [(addon.thread_id, addon.title) for addon in discovery.addons] == [
        (99, "A Game Walkthrough Mod")
    ]
    assert discovery.addons[0].downloads[0].mirrors[0].locator.endswith("mod-v1.2.zip")
