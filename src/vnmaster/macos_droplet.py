"""Build a Finder drag-and-drop launcher for local add-on installation."""
from __future__ import annotations

import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path


class DropletInstallError(RuntimeError):
    pass


def install_patch_droplet(
    destination: Path,
    *,
    executable: Path,
    replace: bool = False,
) -> Path:
    """Compile and install a macOS app that forwards dropped paths to VNMaster."""
    destination = destination.expanduser().resolve()
    executable = executable.expanduser().resolve()
    compiler = shutil.which("osacompile")
    if compiler is None:
        raise DropletInstallError("The VNMaster patch droplet requires macOS osacompile")
    if not executable.is_file():
        raise DropletInstallError(f"VNMaster executable does not exist: {executable}")
    if destination.exists() and not replace:
        raise DropletInstallError(
            f"Droplet already exists: {destination} (use --replace to update it)"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    script = _droplet_script(executable)
    with tempfile.TemporaryDirectory(
        prefix=".vnmaster-droplet-",
        dir=destination.parent,
    ) as temporary:
        temporary_root = Path(temporary)
        source = temporary_root / "droplet.applescript"
        compiled = temporary_root / destination.name
        source.write_text(script, encoding="utf-8")
        result = subprocess.run(  # nosec B603
            [compiler, "-o", str(compiled), str(source)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise DropletInstallError(f"Could not compile patch droplet: {detail}")

        previous = temporary_root / "previous.app"
        replaced = False
        try:
            if destination.exists():
                destination.replace(previous)
                replaced = True
            compiled.replace(destination)
        except OSError as exc:
            if replaced and previous.exists() and not destination.exists():
                previous.replace(destination)
            raise DropletInstallError(f"Could not install patch droplet: {exc}") from exc
    return destination


def _droplet_script(executable: Path) -> str:
    command_prefix = _applescript_string(f"{_shell_quote(str(executable))} install-local ")
    return f'''on open droppedItems
    repeat with droppedItem in droppedItems
        set itemPath to POSIX path of droppedItem
        set commandText to {command_prefix} & quoted form of itemPath
        tell application "Terminal"
            activate
            do script commandText
        end tell
    end repeat
end open

on run
    display dialog "Drag a patch file, archive, or mod folder onto this app." buttons {{"OK"}} default button "OK" with title "VNMaster Patch Installer"
end run
'''


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
