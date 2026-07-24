from pathlib import Path

from vnmaster.scanners.disk import scan_disk

FIXTURES = Path(__file__).parent.parent / "fixtures" / "renpy_games"


def test_scan_finds_renpy_games_only() -> None:
    games = scan_disk(FIXTURES)
    names = {g.folder_name for g in games}
    assert "EternumGame-0.5.2-pc" in names
    assert "WeirdName" in names
    assert "NotARenpyGame" not in names


def test_scan_extracts_version_from_folder_name() -> None:
    games = {g.folder_name: g for g in scan_disk(FIXTURES)}
    assert games["EternumGame-0.5.2-pc"].installed_version == "0.5.2"


def test_scan_falls_back_to_options_rpy_for_version() -> None:
    games = {g.folder_name: g for g in scan_disk(FIXTURES)}
    assert games["WeirdName"].installed_version == "1.0.0a"


def test_scan_extracts_save_dir_hint_from_options_rpy() -> None:
    games = {g.folder_name: g for g in scan_disk(FIXTURES)}
    assert games["EternumGame-0.5.2-pc"].save_dir_hint == "Eternum-1234567890"


def test_scan_marks_unknown_version_when_no_signals(tmp_path: Path) -> None:
    sub = tmp_path / "Mystery"
    (sub / "renpy").mkdir(parents=True)
    (sub / "game").mkdir()
    (sub / "Mystery.sh").touch()
    games = scan_disk(tmp_path)
    g = next(x for x in games if x.folder_name == "Mystery")
    assert g.installed_version == "unknown"


def test_scan_records_launcher_name() -> None:
    games = {g.folder_name: g for g in scan_disk(FIXTURES)}
    assert games["EternumGame-0.5.2-pc"].launcher_name == "MyGame.app"
    assert games["WeirdName"].launcher_name == "run.sh"


def test_scan_handles_missing_root(tmp_path: Path) -> None:
    assert scan_disk(tmp_path / "nope") == []
