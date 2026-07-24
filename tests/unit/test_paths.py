from pathlib import Path

from vnmaster.paths import VNMasterPaths


def test_default_paths_on_macos(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = VNMasterPaths.defaults_for_macos()
    assert paths.games_root == tmp_path / "Games"
    assert paths.renpy_saves_root == tmp_path / "Library" / "RenPy"
    # F95Checker actually creates its directory in lowercase on disk.
    assert paths.f95checker_db == (
        tmp_path / "Library" / "Application Support" / "f95checker" / "db.sqlite3"
    )
    assert paths.vnmaster_db == (
        tmp_path / "Library" / "Application Support" / "VNMaster" / "vnmaster.db"
    )
    assert paths.config_dir == tmp_path / ".config" / "vnmaster"
    assert paths.log_dir == tmp_path / "Library" / "Logs" / "VNMaster"


def test_expand_user_handles_tildes(tmp_path: Path) -> None:
    p = VNMasterPaths.expand_user(Path("~/foo/bar"), home=tmp_path)
    assert p == tmp_path / "foo" / "bar"
