from __future__ import annotations

from dataclasses import replace
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Input, RichLog, SelectionList, Static

from vnmaster.downloads.fetch_session import (
    FetchRunResult,
    FetchSnapshot,
    ResolutionResult,
)
from vnmaster.downloads.f95 import AmbiguousGameError
from vnmaster.downloads.models import (
    DetectedPart,
    DownloadMirror,
    DownloadPlan,
    PartDetection,
    PlannedArtifact,
    ResolvedDownload,
    ThreadInfo,
)
from vnmaster.downloads.workflow import ThreadDiscovery
from vnmaster.f95_search import F95SearchHit
from vnmaster.logging_setup import get_logger
from vnmaster.paths import VNMasterPaths
from vnmaster.tui import VNMasterApp, _activity_text, run_tui


def _artifact(
    kind: str,
    title: str,
    *,
    part: str | None = None,
    host: str = "MEGA",
    warning: str | None = None,
) -> PlannedArtifact:
    return PlannedArtifact(
        kind=kind,  # type: ignore[arg-type]
        title=title,
        version="v1",
        thread_id=94140,
        thread_url="https://f95zone.to/threads/.94140/",
        group_name=part or title,
        platform="mac",
        host=host,
        locator=(
            "https://mega.nz/file/example"
            if host == "MEGA"
            else f"https://{host.casefold()}.example/file"
        ),
        warning=warning,
        alternate_mirrors=(
            DownloadMirror(
                "PixelDrain",
                f"https://pixeldrain.com/u/{title.replace(' ', '-')}",
                platform="mac",
                group_name=part or title,
            ),
        ),
        part=part,
        install_action="separate" if kind == "addon" else None,
    )


class FakeBackend:
    destination = Path("/tmp/Games")
    excluded_hosts = ("MEGA",)

    def __init__(self) -> None:
        game = ThreadInfo(
            94140,
            "Grandma's House",
            "Part 7 v0.108",
            None,
            "https://f95zone.to/threads/.94140/",
            (),
        )
        self.snapshot = FetchSnapshot(
            discovery=ThreadDiscovery(game, (), ()),
            manifest=None,
            detection=PartDetection(
                "part",
                (
                    DetectedPart(6, "Part 6", ()),
                    DetectedPart(7, "Part 7", ()),
                ),
            ),
            parser_summary="ollama/qwen3.6:27b (cache)",
            notes=(),
            installed_parts={6: "0.95"},
        )
        self.plan = DownloadPlan(
            game,
            (
                _artifact("game", "Grandma's House", part="Part 6"),
                _artifact("game", "Grandma's House", part="Part 7"),
                _artifact("addon", "Part 7 walkthrough", part="Part 7"),
            ),
        )
        self.selected_parts: tuple[int, ...] | None = None
        self.executed: DownloadPlan | None = None
        self.closed = False

    def discover(self, query: str, *, include_addons: bool = True) -> FetchSnapshot:
        assert query == "grandma's house"
        assert include_addons
        return self.snapshot

    def build_plan(
        self, snapshot: FetchSnapshot, selected_parts: tuple[int, ...] | None
    ) -> DownloadPlan:
        assert snapshot is self.snapshot
        self.selected_parts = selected_parts
        return self.plan

    def resolve_plan(
        self, plan: DownloadPlan, supplied_urls: dict[int, str] | None = None
    ) -> ResolutionResult:
        return ResolutionResult(
            tuple(
                (
                    ResolvedDownload(
                        artifact.host,
                        artifact.locator,
                        f"https://pixeldrain.com/u/{index}",
                        platform=artifact.platform,
                        group_name=artifact.group_name,
                    ),
                )
                for index, artifact in enumerate(plan.artifacts)
            )
        )

    def execute(
        self,
        plan: DownloadPlan,
        resolved_downloads: tuple[tuple[ResolvedDownload, ...], ...],
    ) -> FetchRunResult:
        assert len(resolved_downloads) == len(plan.artifacts)
        self.executed = plan
        mode = "optionals" if all(item.kind == "addon" for item in plan.artifacts) else "game"
        return FetchRunResult(
            (Path("/tmp/Games/Grandma/Part 7"),),
            (() if mode == "optionals" else (12,)),
            mode=mode,
        )

    def close(self) -> None:
        self.closed = True


class AmbiguousBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    def discover(self, query: str, *, include_addons: bool = True) -> FetchSnapshot:
        self.queries.append(query)
        if len(self.queries) == 1:
            raise AmbiguousGameError(
                query,
                [
                    F95SearchHit(
                        "Grandma's House",
                        94140,
                        "https://f95zone.to/threads/.94140/",
                        creator="MoonBox",
                        version="Part 7 v0.108",
                    ),
                    F95SearchHit(
                        "Grandma's Other House",
                        100,
                        "https://f95zone.to/threads/.100/",
                    ),
                ],
            )
        assert query == "94140"
        return self.snapshot


@pytest.mark.asyncio
async def test_tui_multipart_provider_selection_and_execution() -> None:
    backend = FakeBackend()
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        query = app.query_one("#game-query", Input)
        query.value = "grandma's house"
        await pilot.click("#search-button")
        await pilot.pause()

        parts = app.query_one("#parts", SelectionList)
        parts.select(6).select(7)
        await pilot.pause()

        assert backend.selected_parts == (6, 7)
        providers = app.query_one("#providers", SelectionList)
        assert providers.selected == ["PIXELDRAIN"]
        assert app._selected_plan is not None
        assert {item.host for item in app._selected_plan.artifacts} == {"PIXELDRAIN"}

        addons = app.query_one("#addons", SelectionList)
        addons.select(1)
        app.plan_selection_changed()
        assert app._selected_plan is not None
        assert len(app._selected_plan.artifacts) == 3

        await pilot.click("#download-button")
        await pilot.pause()
        await pilot.click("#accept-confirm")
        await pilot.pause()
        await pilot.pause()

        assert backend.executed is not None
        assert [item.kind for item in backend.executed.artifacts] == [
            "game",
            "game",
            "addon",
        ]
        assert "Download complete" in str(app.query_one("#status", Static).render())

    assert backend.closed


@pytest.mark.asyncio
async def test_tui_keeps_download_disabled_when_every_provider_is_off() -> None:
    backend = FakeBackend()
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        app.query_one("#game-query", Input).value = "grandma's house"
        await pilot.click("#search-button")
        await pilot.pause()
        parts = app.query_one("#parts", SelectionList)
        parts.select(7)
        await pilot.pause()

        providers = app.query_one("#providers", SelectionList)
        providers.deselect_all()
        app.plan_selection_changed()

        assert app.query_one("#download-button", Button).disabled
        assert app._selected_plan is None


@pytest.mark.asyncio
async def test_tui_resolves_ambiguous_search_with_modal_choice() -> None:
    backend = AmbiguousBackend()
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        app.query_one("#game-query", Input).value = "grandma"
        await pilot.click("#search-button")
        await pilot.pause()
        await pilot.click("#accept-game-choice")
        await pilot.pause()
        await pilot.pause()

        assert backend.queries == ["grandma", "94140"]
        assert app._snapshot is backend.snapshot


@pytest.mark.asyncio
async def test_tui_keyboard_navigation_and_automatic_replanning() -> None:
    backend = FakeBackend()
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        app.query_one("#game-query", Input).value = "grandma's house"
        await pilot.click("#search-button")
        await pilot.pause()

        parts = app.query_one("#parts", SelectionList)
        assert parts.has_focus

        await pilot.press("space")
        await pilot.pause()
        assert backend.selected_parts == (6,)
        assert app._candidate_plan is not None
        assert parts.has_focus

        await pilot.press("down", "space")
        await pilot.pause()
        assert backend.selected_parts == (6, 7)
        assert parts.has_focus

        await pilot.press("enter")
        await pilot.pause()
        addons = app.query_one("#addons", SelectionList)
        assert addons.has_focus
        await pilot.press("space")
        await pilot.pause()
        assert addons.selected == [1]

        await pilot.press("right")
        await pilot.pause()
        providers = app.query_one("#providers", SelectionList)
        assert providers.has_focus

        await pilot.press("space")
        await pilot.pause()
        assert set(providers.selected) == {"MEGA", "PIXELDRAIN"}

        await pilot.press("left", "left", "space")
        await pilot.pause()
        assert backend.selected_parts == (6,)
        assert parts.has_focus
        assert addons.selected == [1]
        assert set(providers.selected) == {"MEGA", "PIXELDRAIN"}


@pytest.mark.asyncio
async def test_tui_uses_compact_controls_without_a_replan_button() -> None:
    app = VNMasterApp(backend=FakeBackend())

    async with app.run_test(size=(150, 52)):
        assert len(app.query("#build-button")) == 0
        assert app.query_one("#search-button", Button).compact
        assert app.query_one("#download-button", Button).compact
        assert app.query_one("#game-query", Input).compact
        assert app.query_one("#include-addons", Checkbox).compact
        assert app.query_one("#optionals-only-mode", Checkbox).compact
        footer = app.query_one(Footer)
        assert footer.compact
        assert not footer.show_command_palette
        assert app.query_one("#plan-preview", RichLog).border_title == "Selected plan"
        assert app.query_one("#activity", RichLog).border_title == "Activity"


def test_activity_messages_have_distinct_visual_levels() -> None:
    progress = _activity_text("Downloading 'Part 7' via GOFILE...")
    success = _activity_text("Downloaded and extracted 'Part 7' via GOFILE.")
    warning = _activity_text("VIKINGFILE failed: browser confirmation required")
    error = _activity_text("Failed: Optional Walkthrough Mod: all mirrors failed")

    assert progress.plain.startswith("• ")
    assert success.plain.startswith("✓ ")
    assert warning.plain.startswith("⚠ ")
    assert error.plain.startswith("✗ ")
    assert any(str(span.style) == "bold green" for span in success.spans)
    assert any(str(span.style) == "bold yellow" for span in warning.spans)
    assert any(str(span.style) == "bold red" for span in error.spans)


@pytest.mark.asyncio
async def test_tui_confirmation_shows_selected_compatibility_warnings() -> None:
    backend = FakeBackend()
    backend.plan = replace(
        backend.plan,
        artifacts=(
            *backend.plan.artifacts[:2],
            _artifact(
                "addon",
                "Walkthrough mod",
                part="Part 7",
                warning="Targets v0.107 while the selected game is v0.108.",
            ),
            _artifact(
                "addon",
                "Incest patch",
                part="Part 7",
                warning="The post does not state compatibility with v0.108.",
            ),
        ),
    )
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        app.query_one("#game-query", Input).value = "grandma's house"
        await pilot.click("#search-button")
        await pilot.pause()
        app.query_one("#parts", SelectionList).select(7)
        await pilot.pause()
        app.query_one("#addons", SelectionList).select_all()
        await pilot.pause()

        await pilot.click("#download-button")
        await pilot.pause()

        copy = app.screen.query_one("#confirm-copy", Static)
        warning_text = str(copy.render())
        assert "Compatibility warnings (2):" in warning_text
        assert "Walkthrough mod [Part 7]: Targets v0.107" in warning_text
        assert "Incest patch [Part 7]: The post does not state" in warning_text
        assert "game will still be kept if an optional download fails" in warning_text
        assert app.screen.query_one("#confirm-summary", VerticalScroll).can_focus


@pytest.mark.asyncio
async def test_tui_partial_success_keeps_ready_game_prominent() -> None:
    app = VNMasterApp(backend=FakeBackend())

    async with app.run_test(size=(150, 52)):
        app._accept_execution(
            FetchRunResult(
                (Path("/tmp/Games/Grandma/Part 7"),),
                (12,),
                failures=("Optional Walkthrough Mod: browser confirmation required",),
            )
        )

        status = str(app.query_one("#status", Static).render())
        assert "1 game part kept" in status
        assert "1 selected item(s) failed" in status


@pytest.mark.asyncio
async def test_tui_confirmation_can_download_only_selected_optionals() -> None:
    backend = FakeBackend()
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        app.query_one("#game-query", Input).value = "grandma's house"
        await pilot.click("#search-button")
        await pilot.pause()
        app.query_one("#parts", SelectionList).select(7)
        await pilot.pause()
        app.query_one("#addons", SelectionList).select(1)
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()

        await pilot.click("#download-button")
        await pilot.pause()
        await pilot.click("#accept-confirm")
        await pilot.pause()
        await pilot.pause()

        assert backend.executed is not None
        assert [item.kind for item in backend.executed.artifacts] == ["addon"]
        assert "Optional downloads complete" in str(app.query_one("#status", Static).render())


@pytest.mark.asyncio
async def test_optional_only_plan_does_not_require_a_game_provider() -> None:
    backend = FakeBackend()
    game = replace(backend.plan.artifacts[1], alternate_mirrors=())
    addon = replace(
        backend.plan.artifacts[2],
        host="VIKINGFILE",
        locator="https://vikingfile.com/f/mod",
        alternate_mirrors=(),
    )
    backend.plan = replace(backend.plan, artifacts=(game, addon))
    app = VNMasterApp(backend=backend)

    async with app.run_test(size=(150, 52)) as pilot:
        app.query_one("#game-query", Input).value = "grandma's house"
        await pilot.click("#search-button")
        await pilot.pause()
        app.query_one("#parts", SelectionList).select(7)
        await pilot.pause()
        app.query_one("#addons", SelectionList).select(1)
        await pilot.pause()

        assert app.query_one("#download-button", Button).disabled
        await pilot.press("o")
        await pilot.pause()

        assert not app.query_one("#download-button", Button).disabled
        assert app._selected_plan is not None
        assert [artifact.kind for artifact in app._selected_plan.artifacts] == ["addon"]
        assert [artifact.host for artifact in app._selected_plan.artifacts] == ["VIKINGFILE"]


def test_run_tui_persists_session_activity(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "tui.log"

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            assert kwargs["activity_log"] == log_path

        def run(self) -> None:
            get_logger("vnmaster.tui").info("captured TUI activity")

    monkeypatch.setattr(
        VNMasterPaths,
        "defaults_for_macos",
        lambda: SimpleNamespace(log_dir=tmp_path),
    )
    monkeypatch.setattr("vnmaster.tui.VNMasterApp", FakeApp)

    try:
        run_tui()
        contents = log_path.read_text()
        assert "TUI session started" in contents
        assert "captured TUI activity" in contents
        assert "TUI session ended" in contents
    finally:
        root = logging.getLogger()
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
