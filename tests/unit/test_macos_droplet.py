from pathlib import Path
import shutil

import pytest

from vnmaster.macos_droplet import _droplet_script, install_patch_droplet


def test_droplet_forwards_each_dropped_path_to_installed_vnmaster() -> None:
    script = _droplet_script(Path("/Applications/VNMaster's Tools/vnmaster"))

    assert "on open droppedItems" in script
    assert "install-local" in script
    assert "quoted form of itemPath" in script
    assert "VNMaster Patch Installer" in script
    assert "VNMaster'\\\"'\\\"'s Tools" in script


@pytest.mark.skipif(shutil.which("osacompile") is None, reason="macOS only")
def test_installs_compiled_droplet(tmp_path: Path) -> None:
    executable = tmp_path / "vnmaster"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    destination = tmp_path / "VNMaster Patch Installer.app"

    installed = install_patch_droplet(destination, executable=executable)

    assert installed == destination
    assert (destination / "Contents" / "Info.plist").is_file()
    assert any((destination / "Contents" / "MacOS").iterdir())
