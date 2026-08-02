"""Textual frontend for VNMaster's interactive download workflow."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
import webbrowser

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    RichLog,
    Select,
    SelectionList,
    Static,
)

from vnmaster.downloads.downloader import is_url_for_host
from vnmaster.downloads.f95 import AmbiguousGameError
from vnmaster.downloads.fetch_session import (
    FetchBackend,
    FetchRunResult,
    FetchSnapshot,
    ProtectedDownload,
    ResolutionResult,
    VNMasterFetchBackend,
)
from vnmaster.downloads.models import DownloadPlan, PlannedArtifact
from vnmaster.downloads.workflow import (
    ProviderPolicyError,
    apply_provider_policy,
    available_providers,
    select_optional_artifacts,
)
from vnmaster.f95_search import F95SearchHit
from vnmaster.logging_setup import configure_logging, get_logger
from vnmaster.paths import VNMasterPaths


log = get_logger(__name__)

ActivityLevel = Literal["info", "success", "warning", "error"]

_ACTIVITY_MARKERS: dict[ActivityLevel, tuple[str, str, str]] = {
    "info": ("•", "bold cyan", ""),
    "success": ("✓", "bold green", "green"),
    "warning": ("⚠", "bold yellow", "yellow"),
    "error": ("✗", "bold red", "red"),
}


def _activity_level(message: str) -> ActivityLevel:
    normalized = message.casefold()
    if normalized.startswith(("failed:", "error:", "download failed", "discovery failed")):
        return "error"
    if normalized.startswith("part ") and " failed:" in normalized:
        return "error"
    if normalized.startswith("optional download") and (
        " failed" in normalized or "could not" in normalized
    ):
        return "error"
    if (
        normalized.startswith(
            ("skipping ", "could not resolve ", "note:", "warning:", "schema-constrained ")
        )
        or " failed:" in normalized
    ):
        return "warning"
    if normalized.startswith(
        (
            "ready:",
            "verified:",
            "installed add-on ",
            "recorded install state:",
            "optional download ready:",
            "downloaded and extracted ",
            "published completed download:",
        )
    ):
        return "success"
    return "info"


def _activity_text(message: str, level: ActivityLevel | None = None) -> Text:
    level = level or _activity_level(message)
    marker, marker_style, body_style = _ACTIVITY_MARKERS[level]
    rendered = Text()
    rendered.append(f"{marker} ", style=marker_style)
    label, separator, detail = message.partition(":")
    if separator and len(label) <= 32:
        rendered.append(f"{label}:", style=marker_style)
        rendered.append(detail, style=body_style)
    else:
        rendered.append(message, style=body_style)
    return rendered


def _status_text(message: str, level: ActivityLevel) -> Text:
    marker, marker_style, body_style = _ACTIVITY_MARKERS[level]
    return Text.assemble(
        (f" {marker} ", marker_style),
        (message, f"bold {body_style}".strip()),
    )


def _game_choice_label(hit: F95SearchHit) -> str:
    details = [
        value
        for value in (
            hit.version,
            f"by {hit.creator}" if hit.creator else None,
        )
        if value
    ]
    suffix = f" · {' · '.join(details)}" if details else ""
    return f"{hit.title}{suffix} · thread #{hit.thread_id}"


class PaneSelectionList(SelectionList[object]):
    """A selection list that can move horizontally between choice panes."""

    class MovePane(Message):
        def __init__(self, source_id: str, direction: int) -> None:
            super().__init__()
            self.source_id = source_id
            self.direction = direction

    class Continue(Message):
        def __init__(self, source_id: str) -> None:
            super().__init__()
            self.source_id = source_id

    BINDINGS = [
        *SelectionList.BINDINGS,
        Binding("left", "previous_pane", "Previous pane", show=False),
        Binding("right", "next_pane", "Next pane", show=False),
        Binding("enter", "continue", "Continue", show=False),
    ]

    def action_previous_pane(self) -> None:
        self.post_message(self.MovePane(self.id or "", -1))

    def action_next_pane(self) -> None:
        self.post_message(self.MovePane(self.id or "", 1))

    def action_continue(self) -> None:
        self.post_message(self.Continue(self.id or ""))


@dataclass(frozen=True)
class ConfirmDecision:
    confirmed: bool
    optionals_only: bool = False


class ConfirmDownloadScreen(ModalScreen[ConfirmDecision | None]):
    """Final confirmation before network and filesystem changes."""

    def __init__(self, summary: str, *, optionals_only: bool) -> None:
        super().__init__()
        self.summary = summary
        self.optionals_only = optionals_only

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog", classes="dialog"):
            yield Label("Download and extract this plan?", classes="dialog-title")
            with VerticalScroll(id="confirm-summary", can_focus=True):
                yield Static(
                    self.summary,
                    id="confirm-copy",
                    markup=False,
                    classes="dialog-copy",
                )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel-confirm", compact=True)
                yield Button("Download", id="accept-confirm", variant="success", compact=True)

    @on(Button.Pressed, "#cancel-confirm")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#accept-confirm")
    def accept(self) -> None:
        self.dismiss(ConfirmDecision(True, self.optionals_only))


class GameChoiceScreen(ModalScreen[int | None]):
    """Disambiguate a title search without dropping back to a shell prompt."""

    def __init__(self, hits: list[F95SearchHit]) -> None:
        super().__init__()
        self.hits = hits

    def compose(self) -> ComposeResult:
        with Vertical(id="game-choice-dialog", classes="dialog"):
            yield Label("Select the intended F95 game", classes="dialog-title")
            yield Select[int](
                ((_game_choice_label(hit), hit.thread_id) for hit in self.hits),
                allow_blank=False,
                value=self.hits[0].thread_id,
                id="game-choice",
            )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel-game-choice", compact=True)
                yield Button(
                    "Continue",
                    id="accept-game-choice",
                    variant="primary",
                    compact=True,
                )

    @on(Button.Pressed, "#cancel-game-choice")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#accept-game-choice")
    def accept(self) -> None:
        value = self.query_one("#game-choice", Select).value
        self.dismiss(value if isinstance(value, int) else None)


class CaptchaScreen(ModalScreen[str | None]):
    """Collect the final host URL after a user completes F95's CAPTCHA."""

    def __init__(self, protected: ProtectedDownload) -> None:
        super().__init__()
        self.protected = protected

    def compose(self) -> ComposeResult:
        host = self.protected.mirror.name
        with Vertical(id="captcha-dialog", classes="dialog"):
            yield Label("Browser step required", classes="dialog-title")
            yield Static(
                f"F95 protected {self.protected.artifact_title!r}. Open the "
                f"challenge, continue to {host}, then paste the resulting URL.",
                markup=False,
                classes="dialog-copy",
            )
            yield Input(
                placeholder=f"Paste the resulting {host} URL",
                id="captcha-url",
                compact=True,
            )
            yield Static("", id="captcha-error", markup=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel-captcha", compact=True)
                yield Button(
                    "Open challenge",
                    id="open-captcha",
                    variant="primary",
                    compact=True,
                )
                yield Button(
                    "Use URL",
                    id="accept-captcha",
                    variant="success",
                    compact=True,
                )

    @on(Button.Pressed, "#open-captcha")
    def open_challenge(self) -> None:
        webbrowser.open(self.protected.protected_url)

    @on(Button.Pressed, "#cancel-captcha")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#accept-captcha")
    @on(Input.Submitted, "#captcha-url")
    def accept(self) -> None:
        value = self.query_one("#captcha-url", Input).value.strip()
        if not is_url_for_host(self.protected.mirror.name, value):
            self.query_one("#captcha-error", Static).update(
                f"That is not a valid HTTPS {self.protected.mirror.name} URL."
            )
            return
        self.dismiss(value)


class VNMasterApp(App[None]):
    """Search, configure, review, and execute a VNMaster download plan."""

    TITLE = "VNMaster Downloads"
    SUB_TITLE = "F95 discovery with schema-constrained forum parsing"
    BINDINGS = [
        ("ctrl+r", "search", "Search"),
        ("ctrl+d", "download", "Download"),
        ("o", "toggle_optionals_only", "Optionals only"),
        ("ctrl+q", "quit", "Quit"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }

    #search-row {
        height: 1;
        margin: 1 1 0 1;
    }

    #game-query {
        width: 1fr;
        height: 1;
    }

    #search-button {
        width: auto;
        min-width: 0;
        height: 1;
        margin-left: 1;
    }

    #include-addons {
        width: 18;
        height: 1;
        padding: 0 1;
    }

    #status {
        height: auto;
        min-height: 1;
        max-height: 2;
        margin: 0 1;
        padding: 0 1;
        background: $surface-lighten-1;
    }

    #busy {
        height: 1;
        display: none;
        margin: 0 2;
    }

    #workspace {
        height: 1fr;
        margin: 0 1;
    }

    #choices {
        width: 2fr;
        height: 1fr;
    }

    .choice-pane {
        height: 1fr;
        min-height: 7;
        border: round $surface-lighten-2;
        padding: 0 1;
        margin-bottom: 1;
    }

    .pane-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }

    SelectionList {
        height: 1fr;
        border: none;
        padding: 0;
    }

    #right-pane {
        width: 3fr;
        height: 1fr;
        margin-left: 1;
    }

    #plan-preview {
        height: 3fr;
        border: round $accent;
        padding: 0 1;
    }

    #activity {
        height: 2fr;
        border: round $surface-lighten-2;
        margin-top: 1;
        padding: 0 1;
    }

    #actions {
        height: 1;
        margin: 0 1 1 1;
        align-horizontal: right;
    }

    #key-help {
        width: 1fr;
        height: 1;
        color: $text-muted;
        content-align: left middle;
    }

    #download-button {
        margin-left: 1;
        min-width: 0;
        width: auto;
        height: 1;
    }

    ModalScreen {
        align: center middle;
    }

    .dialog {
        width: 78;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    .dialog-title {
        height: 2;
        text-style: bold;
        color: $accent;
    }

    .dialog-copy {
        height: auto;
        max-height: 12;
        margin-bottom: 1;
    }

    #confirm-summary {
        height: auto;
        max-height: 18;
        margin-bottom: 1;
    }

    #confirm-summary .dialog-copy {
        max-height: 100;
        margin-bottom: 0;
    }

    #optionals-only-mode {
        width: auto;
        height: 1;
        padding: 0 1;
    }

    .dialog-buttons {
        height: 1;
        align-horizontal: right;
        margin-top: 1;
    }

    .dialog-buttons Button {
        margin-left: 1;
    }

    #captcha-error {
        height: auto;
        min-height: 1;
        color: $error;
    }
    """

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        destination: Path | None = None,
        backend: FetchBackend | None = None,
        activity_log: Path | None = None,
    ) -> None:
        super().__init__()
        self._backend = backend or VNMasterFetchBackend(
            config_path=config_path,
            destination=destination,
            reporter=self._report_from_worker,
        )
        self._snapshot: FetchSnapshot | None = None
        self._candidate_plan: DownloadPlan | None = None
        self._selected_plan: DownloadPlan | None = None
        self._execution_plan: DownloadPlan | None = None
        self._supplied_urls: dict[int, str] = {}
        self._busy = False
        self._activity_log = activity_log

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="search-row"):
            yield Input(
                placeholder="Game title, F95 thread ID, or thread URL",
                id="game-query",
                compact=True,
            )
            yield Checkbox(
                "Find add-ons",
                value=True,
                id="include-addons",
                compact=True,
            )
            yield Button(
                "Search",
                id="search-button",
                variant="primary",
                compact=True,
            )
        yield Static(
            f"Destination: {self._backend.destination}",
            id="status",
            markup=False,
        )
        yield LoadingIndicator(id="busy")
        with Horizontal(id="workspace"):
            with VerticalScroll(id="choices"):
                with Vertical(classes="choice-pane"):
                    yield Label("Game parts", classes="pane-title")
                    yield PaneSelectionList(id="parts", disabled=True)
                with Vertical(classes="choice-pane"):
                    yield Label("Optional downloads", classes="pane-title")
                    yield PaneSelectionList(id="addons", disabled=True)
                with Vertical(classes="choice-pane"):
                    yield Label("Download providers", classes="pane-title")
                    yield PaneSelectionList(id="providers", disabled=True)
            with Vertical(id="right-pane"):
                plan_preview = RichLog(
                    id="plan-preview",
                    wrap=True,
                    highlight=False,
                    markup=False,
                )
                plan_preview.border_title = "Selected plan"
                yield plan_preview
                activity = RichLog(
                    id="activity",
                    wrap=True,
                    highlight=False,
                    markup=False,
                )
                activity.border_title = "Activity"
                yield activity
        with Horizontal(id="actions"):
            yield Static(
                "↑/↓ move · Space toggle · ←/→ panes · O optionals",
                id="key-help",
                markup=False,
            )
            yield Checkbox(
                "Optionals only",
                id="optionals-only-mode",
                compact=True,
            )
            yield Button(
                "Download…",
                id="download-button",
                variant="success",
                disabled=True,
                compact=True,
            )
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#plan-preview", RichLog).write(
            Text("Search for a game to build a download plan.", style="dim")
        )
        activity = "Network and LLM work will appear here."
        if self._activity_log is not None:
            activity += f" Log: {self._activity_log}"
        self.query_one("#activity", RichLog).write(_activity_text(activity))
        self.query_one("#game-query", Input).focus()

    def on_unmount(self) -> None:
        self._backend.close()

    def action_search(self) -> None:
        if not self._busy:
            self._start_search()

    def action_download(self) -> None:
        if not self._busy:
            self._confirm_download()

    def action_toggle_optionals_only(self) -> None:
        if self._busy or self._snapshot is None:
            return
        toggle = self.query_one("#optionals-only-mode", Checkbox)
        toggle.value = not toggle.value

    @on(Button.Pressed, "#search-button")
    def search_pressed(self) -> None:
        self.action_search()

    @on(Input.Submitted, "#game-query")
    def search_submitted(self) -> None:
        self.action_search()

    @on(Button.Pressed, "#download-button")
    def download_pressed(self) -> None:
        self._confirm_download()

    @on(SelectionList.SelectedChanged, "#parts")
    def parts_changed(self) -> None:
        if self._snapshot is None:
            return
        if self.query_one("#parts", SelectionList).selected:
            self._build_candidate_plan()
        else:
            self._clear_candidate_plan()

    @on(SelectionList.SelectedChanged, "#addons")
    @on(SelectionList.SelectedChanged, "#providers")
    def plan_selection_changed(self) -> None:
        self._refresh_preview()

    @on(Checkbox.Changed, "#optionals-only-mode")
    def optional_only_changed(self) -> None:
        self._refresh_preview()

    @on(PaneSelectionList.MovePane)
    def move_choice_pane(self, message: PaneSelectionList.MovePane) -> None:
        message.stop()
        pane_ids = ("parts", "addons", "providers")
        if message.source_id not in pane_ids:
            return
        current = pane_ids.index(message.source_id)
        for offset in range(1, len(pane_ids) + 1):
            target_id = pane_ids[(current + message.direction * offset) % len(pane_ids)]
            target = self.query_one(f"#{target_id}", PaneSelectionList)
            if not target.disabled:
                target.focus()
                return

    @on(PaneSelectionList.Continue)
    def continue_from_choice_pane(self, message: PaneSelectionList.Continue) -> None:
        message.stop()
        if message.source_id == "providers":
            download = self.query_one("#download-button", Button)
            if not download.disabled:
                download.focus()
            return
        pane_ids = ("parts", "addons", "providers")
        if message.source_id not in pane_ids:
            return
        current = pane_ids.index(message.source_id)
        for target_id in pane_ids[current + 1 :]:
            target = self.query_one(f"#{target_id}", PaneSelectionList)
            if not target.disabled:
                target.focus()
                return
        download = self.query_one("#download-button", Button)
        if not download.disabled:
            download.focus()

    def _start_search(self, query_override: str | None = None) -> None:
        query = query_override or self.query_one("#game-query", Input).value.strip()
        if not query:
            self._set_status("Enter a game title, thread ID, or thread URL.", error=True)
            return
        self._reset_plan_state()
        self._set_busy(True, f"Discovering {query!r}...")
        include_addons = self.query_one("#include-addons", Checkbox).value
        self._discover(query, include_addons)

    @work(
        thread=True,
        exclusive=True,
        group="discovery",
        exit_on_error=False,
    )
    def _discover(self, query: str, include_addons: bool) -> None:
        try:
            snapshot = self._backend.discover(query, include_addons=include_addons)
        except AmbiguousGameError as exc:
            self.call_from_thread(self._show_game_choices, exc.hits)
        except Exception as exc:
            self.call_from_thread(self._fail, "Discovery failed", exc)
        else:
            self.call_from_thread(self._accept_snapshot, snapshot)

    def _show_game_choices(self, hits: list[F95SearchHit]) -> None:
        self._set_busy(False)
        self.push_screen(GameChoiceScreen(hits), self._game_choice_complete)

    def _game_choice_complete(self, thread_id: int | None) -> None:
        if thread_id is None:
            self._set_status("Search cancelled.")
            return
        self._start_search(str(thread_id))

    def _accept_snapshot(self, snapshot: FetchSnapshot) -> None:
        self._snapshot = snapshot
        self._set_busy(False)
        game = snapshot.discovery.game
        parser = f" · parser {snapshot.parser_summary}" if snapshot.parser_summary else ""
        self._set_status(
            f"Resolved: {game.title} · {game.version or 'unknown version'} · "
            f"thread #{game.thread_id}{parser}"
        )
        for note in snapshot.notes:
            self._append_log(f"Note: {note}")

        parts = self.query_one("#parts", SelectionList)
        parts.clear_options()
        if snapshot.detection.is_multipart:
            choices = []
            for part in snapshot.detection.parts:
                installed = snapshot.installed_parts.get(part.number)
                label = part.label + (f" · installed {installed}" if installed is not None else "")
                choices.append((label, part.number, False))
            parts.add_options(choices)
            parts.disabled = False
            parts.highlighted = 0
            self._write_plan_lines(
                "Multipart thread detected.",
                "Select one or more independent games; the plan updates automatically.",
            )
            parts.focus()
        else:
            parts.disabled = True
            self._build_candidate_plan(move_focus=True)

    def _build_candidate_plan(self, *, move_focus: bool = False) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        selected_parts: tuple[int, ...] | None = None
        if snapshot.detection.is_multipart:
            selected_parts = tuple(sorted(self.query_one("#parts", SelectionList).selected))
            if not selected_parts:
                self._clear_candidate_plan()
                return

        previous_candidate = self._candidate_plan
        addons = self.query_one("#addons", SelectionList)
        previous_addon_highlight = addons.highlighted
        previous_optional = set()
        if previous_candidate is not None:
            old_optional = [item for item in previous_candidate.artifacts if item.kind == "addon"]
            selected_optional = set(addons.selected)
            previous_optional = {
                self._artifact_key(artifact)
                for number, artifact in enumerate(old_optional, start=1)
                if number in selected_optional
            }

        providers = self.query_one("#providers", SelectionList)
        previous_provider_highlight = providers.highlighted
        previous_provider_values = (
            {value.casefold() for value in available_providers(previous_candidate)}
            if previous_candidate is not None
            else set()
        )
        previously_enabled = {str(value).casefold() for value in providers.selected}
        try:
            candidate = self._backend.build_plan(snapshot, selected_parts)
        except Exception as exc:
            self._fail("Could not build the download plan", exc)
            return
        self._candidate_plan = candidate
        self._supplied_urls.clear()

        addons.clear_options()
        optional = [item for item in candidate.artifacts if item.kind == "addon"]
        addons.add_options(
            (
                self._artifact_label(artifact),
                number,
                self._artifact_key(artifact) in previous_optional,
            )
            for number, artifact in enumerate(optional, start=1)
        )
        addons.disabled = not bool(optional)
        if optional:
            addons.highlighted = min(previous_addon_highlight or 0, len(optional) - 1)

        providers.clear_options()
        excluded = {value.casefold() for value in self._backend.excluded_hosts}
        provider_values = available_providers(candidate)
        providers.add_options(
            (
                provider,
                provider,
                (
                    provider.casefold() in previously_enabled
                    if provider.casefold() in previous_provider_values
                    else provider.casefold() not in excluded
                ),
            )
            for provider in provider_values
        )
        providers.disabled = not bool(provider_values)
        if provider_values:
            providers.highlighted = min(previous_provider_highlight or 0, len(provider_values) - 1)
        self._refresh_preview()
        if move_focus:
            self._focus_first_plan_selector()

    def _clear_candidate_plan(self) -> None:
        self._candidate_plan = None
        self._selected_plan = None
        self._execution_plan = None
        self._supplied_urls.clear()
        for selector in ("#addons", "#providers"):
            widget = self.query_one(selector, SelectionList)
            widget.clear_options()
            widget.disabled = True
        self.query_one("#download-button", Button).disabled = True
        self._write_plan_lines(
            "Multipart thread detected.",
            "Select one or more independent games; the plan updates automatically.",
        )

    def _focus_first_plan_selector(self) -> None:
        for selector in ("#addons", "#providers"):
            widget = self.query_one(selector, PaneSelectionList)
            if not widget.disabled:
                widget.focus()
                return
        download = self.query_one("#download-button", Button)
        if not download.disabled:
            download.focus()

    def _refresh_preview(self) -> None:
        candidate = self._candidate_plan
        if candidate is None:
            return
        self._execution_plan = None
        selected_optional = tuple(sorted(self.query_one("#addons", SelectionList).selected))
        enabled_providers = tuple(self.query_one("#providers", SelectionList).selected)
        plan = select_optional_artifacts(candidate, selected_optional)
        if self.query_one("#optionals-only-mode", Checkbox).value:
            optionals = tuple(artifact for artifact in plan.artifacts if artifact.kind == "addon")
            if not optionals:
                self._selected_plan = None
                self.query_one("#download-button", Button).disabled = True
                self._write_plan_lines(
                    "Optional-only mode.",
                    "Select at least one optional download.",
                )
                return
            plan = replace(plan, artifacts=optionals)
        try:
            plan = apply_provider_policy(plan, enabled_providers)
        except ProviderPolicyError as exc:
            self._selected_plan = None
            self.query_one("#download-button", Button).disabled = True
            self._write_plan_lines(
                "Plan is not ready.",
                str(exc),
                "Enable another provider or deselect an optional download that depends on it.",
            )
            return
        self._selected_plan = plan
        self.query_one("#download-button", Button).disabled = self._busy
        self._render_plan(plan)

    def _render_plan(self, plan: DownloadPlan) -> None:
        title = Text(plan.game.title, style="bold")
        title.append("  ·  ", style="dim")
        title.append(plan.game.version or "unknown version", style="bold cyan")
        destination = Text("Destination  ", style="dim")
        destination.append(str(self._backend.destination))
        lines: list[str | Text] = [title, destination]
        if plan.artifacts and all(artifact.kind == "addon" for artifact in plan.artifacts):
            lines.append(
                Text(
                    "OPTIONALS ONLY  ·  game skipped  ·  files kept separate",
                    style="bold magenta",
                )
            )
        lines.append("")
        for number, artifact in enumerate(plan.artifacts, start=1):
            artifact_line = Text(f"{number:>2}. ", style="dim")
            kind_style = "bold green" if artifact.kind == "game" else "bold magenta"
            artifact_line.append(f"{artifact.kind.upper():<5}", style=kind_style)
            artifact_line.append(f"  {artifact.title}", style="bold")
            if artifact.part:
                artifact_line.append(f"  ·  {artifact.part}", style="cyan")
            artifact_line.append(f"  ·  {artifact.host}", style="bright_blue")
            fallback_count = len(artifact.alternate_mirrors)
            if fallback_count:
                artifact_line.append(
                    f"  + {fallback_count} fallback{'s' if fallback_count != 1 else ''}",
                    style="dim",
                )
            if artifact.install_action == "merge":
                artifact_line.append("  ·  installs into game", style="yellow")
            elif artifact.install_action == "separate":
                artifact_line.append("  ·  kept separate", style="green")
            lines.append(artifact_line)
            if artifact.warning:
                lines.append(Text(f"     ⚠ {artifact.warning}", style="yellow"))
        if plan.skipped:
            lines.extend(("", Text("Unavailable / manual items", style="bold yellow")))
            for item in plan.skipped:
                skipped = Text("  ⚠ ", style="bold yellow")
                skipped.append(f"{item.title}: ", style="yellow")
                skipped.append(item.reason, style="dim")
                lines.append(skipped)
        self._write_plan_lines(*lines)

    def _confirm_download(self) -> None:
        plan = self._selected_plan
        if plan is None or self._busy:
            return
        game_count = sum(item.kind == "game" for item in plan.artifacts)
        addon_count = len(plan.artifacts) - game_count
        optionals_only = game_count == 0 and addon_count > 0
        warned_artifacts = [item for item in plan.artifacts if item.warning]
        summary = (
            f"{game_count} game{'s' if game_count != 1 else ''}, "
            f"{addon_count} optional download{'s' if addon_count != 1 else ''}\n"
            f"Destination: {self._backend.destination}"
        )
        if optionals_only:
            summary += "\nGame download skipped; optionals will be kept separate."
        elif game_count and addon_count:
            summary += "\nThe game will still be kept if an optional download fails."
        if warned_artifacts:
            summary += f"\n\nCompatibility warnings ({len(warned_artifacts)}):"
            for artifact in warned_artifacts:
                part = f" [{artifact.part}]" if artifact.part else ""
                summary += f"\n- {artifact.title}{part}: {artifact.warning}"
        self.push_screen(
            ConfirmDownloadScreen(summary, optionals_only=optionals_only),
            self._confirmation_complete,
        )

    def _confirmation_complete(self, decision: ConfirmDecision | None) -> None:
        if decision is None or not decision.confirmed:
            self._set_status("Download cancelled.")
            return
        plan = self._selected_plan
        if plan is None:
            return
        if decision.optionals_only:
            self._append_log(
                f"Optional-only mode: skipping the game and downloading "
                f"{len(plan.artifacts)} selected optional item(s)."
            )
        self._execution_plan = plan
        self._begin_resolution()

    def _begin_resolution(self) -> None:
        plan = self._execution_plan
        if plan is None:
            return
        self._set_busy(True, "Resolving enabled download providers...")
        self._resolve(plan, dict(self._supplied_urls))

    @work(thread=True, exclusive=True, group="resolution", exit_on_error=False)
    def _resolve(self, plan: DownloadPlan, supplied_urls: dict[int, str]) -> None:
        try:
            result = self._backend.resolve_plan(plan, supplied_urls)
        except Exception as exc:
            self.call_from_thread(self._fail, "Link resolution failed", exc)
        else:
            self.call_from_thread(self._accept_resolution, plan, result)

    def _accept_resolution(self, plan: DownloadPlan, resolution: ResolutionResult) -> None:
        if resolution.errors:
            self._set_busy(False)
            self._set_status(resolution.errors[0], error=True)
            for error in resolution.errors:
                self._append_log(f"Error: {error}")
            return
        if resolution.protected:
            self._set_busy(False)
            protected = resolution.protected[0]
            self.push_screen(
                CaptchaScreen(protected),
                lambda value: self._captcha_complete(protected, value),
            )
            return
        if not resolution.ready:
            self._set_busy(False)
            self._set_status("The download plan could not be fully resolved.", error=True)
            return
        self._set_busy(True, "Downloading, extracting, and verifying...")
        self._execute(plan, resolution)

    def _captcha_complete(self, protected: ProtectedDownload, value: str | None) -> None:
        if value is None:
            self._set_status("Browser download step cancelled.")
            return
        self._supplied_urls[protected.artifact_index] = value
        self._begin_resolution()

    @work(thread=True, exclusive=True, group="execution", exit_on_error=False)
    def _execute(self, plan: DownloadPlan, resolution: ResolutionResult) -> None:
        try:
            result = self._backend.execute(plan, resolution.downloads)
        except Exception as exc:
            self.call_from_thread(self._fail, "Download failed", exc)
        else:
            self.call_from_thread(self._accept_execution, result)

    def _accept_execution(self, result: FetchRunResult) -> None:
        self._set_busy(False)
        for final_dir in result.final_dirs:
            self._append_log(f"Ready: {final_dir}", level="success")
        for install_id in result.install_ids:
            self._append_log(f"Recorded install state: #{install_id}", level="success")
        for failure in result.failures:
            self._append_log(f"Failed: {failure}", level="error")
        if result.failures:
            if result.mode == "optionals":
                summary = (
                    f"Optional downloads: {len(result.final_dirs)} ready, "
                    f"{len(result.failures)} failed."
                )
            else:
                noun = "part" if len(result.final_dirs) == 1 else "parts"
                summary = (
                    f"Download finished: {len(result.final_dirs)} game {noun} kept; "
                    f"{len(result.failures)} selected item(s) failed."
                )
            self._set_status(
                summary,
                level="warning" if result.final_dirs else "error",
            )
        elif result.mode == "optionals":
            self._set_status(
                f"Optional downloads complete: {len(result.final_dirs)} ready.",
                level="success",
            )
        else:
            self._set_status(
                f"Download complete: {len(result.final_dirs)} game "
                f"{'part' if len(result.final_dirs) == 1 else 'parts'} ready.",
                level="success",
            )

    def _reset_plan_state(self) -> None:
        self._snapshot = None
        self._candidate_plan = None
        self._selected_plan = None
        self._execution_plan = None
        self._supplied_urls.clear()
        self.query_one("#optionals-only-mode", Checkbox).value = False
        for selector in ("#parts", "#addons", "#providers"):
            widget = self.query_one(selector, SelectionList)
            widget.clear_options()
            widget.disabled = True
        self.query_one("#download-button", Button).disabled = True
        self._write_plan_lines("Searching...")

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        self.query_one("#busy", LoadingIndicator).display = busy
        self.query_one("#search-button", Button).disabled = busy
        self.query_one("#game-query", Input).disabled = busy
        if busy:
            self.query_one("#download-button", Button).disabled = True
        elif self._snapshot is not None:
            self.query_one("#download-button", Button).disabled = self._selected_plan is None
        if status is not None:
            self._set_status(status)
            self._append_log(status)

    def _set_status(
        self,
        value: str,
        *,
        level: ActivityLevel = "info",
        error: bool = False,
    ) -> None:
        if error:
            level = "error"
        status = self.query_one("#status", Static)
        status.update(_status_text(value, level))

    def _fail(self, label: str, exc: BaseException) -> None:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        self._set_busy(False)
        self._set_status(f"{label}: {detail}", error=True)
        self._append_log(f"{label}: {detail}")

    def _report_from_worker(self, message: str) -> None:
        try:
            self.call_from_thread(self._append_log, message)
        except RuntimeError:
            pass

    def _append_log(self, message: str, *, level: ActivityLevel | None = None) -> None:
        if self._activity_log is not None:
            log.info("%s", message)
        self.query_one("#activity", RichLog).write(
            _activity_text(message, level),
            scroll_end=True,
        )

    def _write_plan_lines(self, *lines: str | Text) -> None:
        log = self.query_one("#plan-preview", RichLog)
        log.clear()
        for line in lines:
            log.write(line)

    @staticmethod
    def _artifact_label(artifact: PlannedArtifact) -> Text:
        details = [artifact.title]
        if artifact.part:
            details.append(artifact.part)
        details.append(artifact.host)
        if artifact.install_action == "merge":
            details.append("install")
        elif artifact.install_action == "separate":
            details.append("keep separate")
        label = Text(" · ".join(details))
        if artifact.warning:
            label.append(" ⚠", style="yellow")
        return label

    @staticmethod
    def _artifact_key(artifact: PlannedArtifact) -> tuple[object, ...]:
        return (
            artifact.kind,
            artifact.thread_id,
            artifact.title.casefold(),
            artifact.part,
            artifact.version,
            artifact.install_action,
        )


def run_tui(*, config_path: Path | None = None, destination: Path | None = None) -> None:
    log_path = VNMasterPaths.defaults_for_macos().log_dir / "tui.log"
    configure_logging(log_path, include_stderr=False)
    log.info("TUI session started (pid=%d)", os.getpid())
    try:
        VNMasterApp(
            config_path=config_path,
            destination=destination,
            activity_log=log_path,
        ).run()
    except Exception:
        log.exception("TUI session crashed")
        raise
    finally:
        log.info("TUI session ended (pid=%d)", os.getpid())
