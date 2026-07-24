from pathlib import Path

from click.testing import CliRunner
from sqlalchemy import select

from vnmaster.cli import (
    _game_choice_label,
    _guard_incompatible_addons,
    _optional_choice_label,
    _parse_optional_selection,
    _prompt_game_resolution,
    _prompt_optional_selection_menu,
    main,
)
from vnmaster.db.engine import create_engine_for, session_scope
from vnmaster.db.models import Base, Pairing
from vnmaster.downloads.models import DownloadPlan, PlannedArtifact, ThreadInfo
from vnmaster.f95_search import F95SearchHit


CONFIG_FIXTURE = Path(__file__).parent.parent / "fixtures" / "configs" / "valid.toml"


def test_cli_has_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "fetch" in result.output
    assert "rebuild" in result.output
    assert "installs" in result.output
    for sub in ["digest", "bot", "init", "pair", "status"]:
        assert sub in result.output


def test_optional_download_selection_parser() -> None:
    assert _parse_optional_selection("", 4) == ()
    assert _parse_optional_selection("none", 4) == ()
    assert _parse_optional_selection("all", 3) == (1, 2, 3)
    assert _parse_optional_selection("1, 3-4", 4) == (1, 3, 4)


def test_optional_download_selection_rejects_bad_number() -> None:
    import pytest

    with pytest.raises(ValueError, match="outside"):
        _parse_optional_selection("5", 4)


def _optional_artifact(title: str, *, warning: str | None = None) -> PlannedArtifact:
    return PlannedArtifact(
        kind="addon",
        title=title,
        version="v1.2",
        thread_id=2,
        thread_url="https://f95zone.to/threads/.2/",
        group_name="Patch",
        platform=None,
        host="MEGA",
        locator="https://f95zone.to/masked/mega.nz/x",
        warning=warning,
    )


def test_optional_choice_label_includes_host_version_and_warning() -> None:
    label = _optional_choice_label(_optional_artifact("A Patch", warning="uncertain"))
    assert label == "A Patch · MEGA · v1.2 · install ⚠"


def test_optional_selection_menu_returns_sorted_choice_values(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeQuestion:
        def ask(self):
            return [2, 1]

    def fake_checkbox(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return FakeQuestion()

    monkeypatch.setattr("vnmaster.cli.questionary.checkbox", fake_checkbox)
    selected = _prompt_optional_selection_menu(
        (_optional_artifact("Patch"), _optional_artifact("Walkthrough"))
    )
    assert selected == (1, 2)
    assert captured["message"] == "Select optional downloads"
    assert "Space toggle" in str(captured["instruction"])


def test_guard_incompatible_addons_requires_explicit_force() -> None:
    game = ThreadInfo(1, "A Game", "v2", None, "thread", ())
    required = PlannedArtifact(
        kind="game",
        title="A Game",
        version="v2",
        thread_id=1,
        thread_url="thread",
        group_name="Mac",
        platform="mac",
        host="MEGA",
        locator="game",
    )
    addon = _optional_artifact(
        "A Game Multi-Mod",
        warning="reported add-on version v1 may not match game v2",
    )
    plan = DownloadPlan(game, (required, addon))

    import pytest
    import click

    with pytest.raises(click.ClickException, match="--force-incompatible-addons"):
        _guard_incompatible_addons(plan, force=False, assume_yes=True)
    assert _guard_incompatible_addons(plan, force=True, assume_yes=True) is plan


def test_game_resolution_menu_shows_creator_version_and_thread(monkeypatch) -> None:
    hits = [
        F95SearchHit(
            "Artemis",
            77680,
            "https://f95zone.to/threads/77680",
            creator="digi.B",
            version="v0.7.1",
        ),
        F95SearchHit(
            "Artemis",
            47082,
            "https://f95zone.to/threads/47082",
            creator="Volta",
            version="v0.11",
        ),
    ]
    captured: dict[str, object] = {}

    class FakeQuestion:
        def ask(self):
            return 1

    def fake_select(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return FakeQuestion()

    monkeypatch.setattr("vnmaster.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("vnmaster.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("vnmaster.cli.questionary.select", fake_select)

    selected = _prompt_game_resolution(hits)

    assert selected.thread_id == 47082
    assert "digi.B" in _game_choice_label(hits[0])
    assert "v0.7.1" in _game_choice_label(hits[0])
    assert "77680" in _game_choice_label(hits[0])
    assert captured["message"] == "Select the intended F95 game"


def test_digest_subcommand_exists() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["digest", "--help"])
    assert result.exit_code == 0
    assert "Run one digest pipeline now" in result.output


def test_digest_has_daily_flag():
    from vnmaster.cli import digest
    opt = {p.name: p for p in digest.params}["daily"]
    assert opt.is_flag and opt.default is False


def test_no_subcommand_still_says_not_yet_implemented() -> None:
    """Regression: previously bot/pair/status echoed 'not yet implemented'.
    They must now do real work (or fail clearly because config is missing).
    """
    runner = CliRunner()
    for sub in ["bot", "pair", "status"]:
        result = runner.invoke(main, [sub, "--help"])
        assert "not yet implemented" not in result.output, (
            f"{sub} subcommand still has placeholder text"
        )


def test_status_subcommand_wires_to_cmd_status(tmp_path: Path, monkeypatch) -> None:
    """Status command should query the DB and print results, not echo a stub."""
    db = tmp_path / "v.db"
    engine = create_engine_for(db)
    Base.metadata.create_all(engine)

    config_text = CONFIG_FIXTURE.read_text().replace(
        '"~/Library/Application Support/VNMaster/vnmaster.db"',
        f'"{db}"',
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text)

    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, ["status", "--config", str(config_file)])
    assert result.exit_code == 0
    # Empty DB → either "No digest runs yet" or library count of 0
    assert "Library size" in result.output or "No digest runs yet" in result.output


def test_pair_subcommand_writes_pairing(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "v.db"
    engine = create_engine_for(db)
    Base.metadata.create_all(engine)

    config_text = CONFIG_FIXTURE.read_text().replace(
        '"~/Library/Application Support/VNMaster/vnmaster.db"',
        f'"{db}"',
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text)

    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, [
        "pair", "MySaveDir", "https://f95zone.to/threads/foo.12345/",
        "--config", str(config_file),
    ])
    assert result.exit_code == 0
    assert "12345" in result.output

    with session_scope(engine) as s:
        row = s.execute(select(Pairing)).scalar_one()
        assert row.f95_thread_id == 12345
        assert row.save_dir_name == "MySaveDir"


def test_pair_subcommand_rejects_bad_url(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "v.db"
    engine = create_engine_for(db)
    Base.metadata.create_all(engine)

    config_text = CONFIG_FIXTURE.read_text().replace(
        '"~/Library/Application Support/VNMaster/vnmaster.db"',
        f'"{db}"',
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text)

    monkeypatch.setenv("HOME", str(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, [
        "pair", "X", "https://example.com/foo",
        "--config", str(config_file),
    ])
    assert result.exit_code != 0
    assert "does not look like an F95" in result.output


def test_bot_subcommand_has_real_help() -> None:
    """The bot command should describe what it does, not say 'not implemented'."""
    runner = CliRunner()
    result = runner.invoke(main, ["bot", "--help"])
    assert result.exit_code == 0
    assert "Run the Discord reaction bot" in result.output


def _make_test_engine_and_config(tmp_path: Path, monkeypatch):
    """Helper: create a temp DB + config file wired to each other."""
    from vnmaster.db.engine import create_engine_for
    from vnmaster.db.models import Base

    db = tmp_path / "v.db"
    engine = create_engine_for(db)
    Base.metadata.create_all(engine)

    config_text = CONFIG_FIXTURE.read_text().replace(
        '"~/Library/Application Support/VNMaster/vnmaster.db"',
        f'"{db}"',
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text)
    monkeypatch.setenv("HOME", str(tmp_path))
    return engine, config_file


def test_pairings_subcommand_prints_empty_message(tmp_path: Path, monkeypatch) -> None:
    engine, config_file = _make_test_engine_and_config(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["pairings", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "no pairings" in result.output.lower()


def test_pairings_subcommand_lists_rows(tmp_path: Path, monkeypatch) -> None:
    from vnmaster.db.engine import session_scope
    from vnmaster.db.models import Pairing

    engine, config_file = _make_test_engine_and_config(tmp_path, monkeypatch)
    with session_scope(engine) as s:
        s.add(Pairing(f95_thread_id=12345, save_dir_name="MyGame", confidence=1.0, paired_at=100))

    runner = CliRunner()
    result = runner.invoke(main, ["pairings", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "12345" in result.output
    assert "MyGame" in result.output


def test_unpair_subcommand_removes_row(tmp_path: Path, monkeypatch) -> None:
    from sqlalchemy import select

    from vnmaster.db.engine import session_scope
    from vnmaster.db.models import Pairing

    engine, config_file = _make_test_engine_and_config(tmp_path, monkeypatch)
    with session_scope(engine) as s:
        s.add(Pairing(f95_thread_id=99999, save_dir_name="ByeBye", confidence=0.9, paired_at=100))

    runner = CliRunner()
    result = runner.invoke(main, ["unpair", "ByeBye", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "99999" in result.output

    with session_scope(engine) as s:
        row = s.execute(select(Pairing).where(Pairing.f95_thread_id == 99999)).scalar_one_or_none()
    assert row is None


def test_unpair_subcommand_missing_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    _engine_unused, config_file = _make_test_engine_and_config(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(main, ["unpair", "ghost", "--config", str(config_file)])
    assert result.exit_code != 0
    assert "No pairing found for" in result.output
