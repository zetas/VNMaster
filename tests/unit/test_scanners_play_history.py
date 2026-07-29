import json
import os
import time
import zipfile
from pathlib import Path

from vnmaster.scanners.play_history import scan_play_history

FIXTURES = Path(__file__).parent.parent / "fixtures" / "renpy_saves"


def _write_renpy_save(path: Path, version: str | None) -> None:
    """Create a minimal Ren'Py-style .save (zip with a json member)."""
    meta = {"_save_name": "1", "_renpy_version": [8, 3, 2, 0]}
    if version is not None:
        meta["_version"] = version
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("json", json.dumps(meta))
        z.writestr("log", b"fake log")


def test_scan_reads_version_from_newest_save(tmp_path: Path) -> None:
    """The game version (_version in the save's json) should come from the
    most-recently-modified save file."""
    sub = tmp_path / "MyGame-1"
    sub.mkdir()
    old = sub / "1-1.save"
    new = sub / "2-1.save"
    _write_renpy_save(old, version="0.5.0")
    _write_renpy_save(new, version="0.7.2")
    now = time.time()
    os.utime(old, (now - 1000, now - 1000))
    os.utime(new, (now - 10, now - 10))

    entry = next(e for e in scan_play_history(tmp_path) if e.save_dir_name == "MyGame-1")
    assert entry.last_played_version == "0.7.2"


def test_scan_version_none_when_save_lacks_version(tmp_path: Path) -> None:
    sub = tmp_path / "NoVer-1"
    sub.mkdir()
    _write_renpy_save(sub / "1-1.save", version=None)
    entry = next(e for e in scan_play_history(tmp_path) if e.save_dir_name == "NoVer-1")
    assert entry.last_played_version is None


def test_scan_version_none_for_corrupt_save(tmp_path: Path) -> None:
    sub = tmp_path / "Corrupt-1"
    sub.mkdir()
    (sub / "1-1.save").write_bytes(b"not a zip file")
    entry = next(e for e in scan_play_history(tmp_path) if e.save_dir_name == "Corrupt-1")
    assert entry.last_played_version is None
    assert entry.save_count == 1  # still counted as a save


def test_scan_finds_all_subdirs() -> None:
    entries = scan_play_history(FIXTURES)
    names = {e.save_dir_name for e in entries}
    assert {"EternumGame-1234567890", "EmptyDir", "WithPersistent"} == names


def test_scan_counts_save_files_correctly() -> None:
    entries = {e.save_dir_name: e for e in scan_play_history(FIXTURES)}
    assert entries["EternumGame-1234567890"].save_count == 2
    assert entries["EmptyDir"].save_count == 0


def test_scan_detects_persistent_flag() -> None:
    entries = {e.save_dir_name: e for e in scan_play_history(FIXTURES)}
    assert entries["EternumGame-1234567890"].persistent_data_present is True
    assert entries["WithPersistent"].persistent_data_present is True
    assert entries["EmptyDir"].persistent_data_present is False


def test_scan_last_played_uses_save_mtime(tmp_path: Path) -> None:
    sub = tmp_path / "MyGame-9"
    sub.mkdir()
    save_a = sub / "1-1.save"
    save_b = sub / "2-1.save"
    save_a.write_text("a")
    save_b.write_text("b")
    now = int(time.time())
    os.utime(save_a, (now - 1000, now - 1000))
    os.utime(save_b, (now - 50, now - 50))
    entries = scan_play_history(tmp_path)
    e = next(x for x in entries if x.save_dir_name == "MyGame-9")
    assert e.last_played_at == now - 50
    assert e.first_played_at == now - 1000


def test_scan_total_size_sums_files(tmp_path: Path) -> None:
    sub = tmp_path / "SizeTest-1"
    sub.mkdir()
    (sub / "1-1.save").write_bytes(b"x" * 100)
    (sub / "2-1.save").write_bytes(b"y" * 200)
    entries = scan_play_history(tmp_path)
    e = next(x for x in entries if x.save_dir_name == "SizeTest-1")
    assert e.total_save_size_bytes == 300


def test_scan_handles_missing_root(tmp_path: Path) -> None:
    entries = scan_play_history(tmp_path / "does_not_exist")
    assert entries == []


def test_scan_finds_nested_save_dir(tmp_path: Path) -> None:
    """A game can set config.save_directory to a path ("Talothral/Sorcerer2"),
    which lands its saves one level below the RenPy root."""
    nested = tmp_path / "Talothral" / "Sorcerer2"
    nested.mkdir(parents=True)
    _write_renpy_save(nested / "1-1.save", version="0.4.0")

    entries = {e.save_dir_name: e for e in scan_play_history(tmp_path)}
    assert "Talothral/Sorcerer2" in entries
    assert entries["Talothral/Sorcerer2"].save_count == 1
    assert entries["Talothral/Sorcerer2"].last_played_version == "0.4.0"


def test_scan_ignores_sync_mirror_dirs(tmp_path: Path) -> None:
    """Ren'Py's cloud-sync dir mirrors the parent's saves — not a separate game.

    Nearly every save folder has one, so treating them as games would roughly
    double the library with duplicates.
    """
    game = tmp_path / "MyGame-1"
    sync = game / "sync"
    sync.mkdir(parents=True)
    _write_renpy_save(game / "1-1.save", version="1.0")
    _write_renpy_save(sync / "1-1.save", version="1.0")

    names = {e.save_dir_name for e in scan_play_history(tmp_path)}
    assert "MyGame-1" in names
    assert "MyGame-1/sync" not in names
    assert not any(n.endswith("sync") for n in names)


def test_scan_skips_nested_dirs_without_saves(tmp_path: Path) -> None:
    """Only surface a nested dir when it actually holds saves."""
    (tmp_path / "Container" / "empty").mkdir(parents=True)
    (tmp_path / "Container" / "images").mkdir(parents=True)
    names = {e.save_dir_name for e in scan_play_history(tmp_path)}
    assert "Container/empty" not in names
    assert "Container/images" not in names


def test_scan_does_not_recurse_below_one_level(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    _write_renpy_save(deep / "1-1.save", version="1.0")
    names = {e.save_dir_name for e in scan_play_history(tmp_path)}
    assert not any("b/c" in n for n in names)
