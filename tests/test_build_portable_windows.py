"""Windows portable-package contract; the real PyInstaller build stays opt-in."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows packaging only")


@pytest.mark.parametrize("explicit_root", [False, True])
def test_package_only_creates_isolated_portable_zip(tmp_path: Path, explicit_root) -> None:
    """A missing app copy or isolation launcher would make the ZIP unsafe to test."""
    repo = Path(__file__).resolve().parents[1]
    script = repo / "packaging" / "windows" / "build_portable.ps1"
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    assert pwsh is not None

    app = tmp_path / "built-app"
    (app / "_internal").mkdir(parents=True)
    (app / "M110.exe").write_bytes(b"fake executable")
    (app / "_internal" / "marker.txt").write_text("bundled", encoding="utf-8")
    output = tmp_path / "artifacts"

    dev_root = tmp_path / "dev"
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PackageOnly",
            "-AppDirectory",
            str(app),
            "-OutputDirectory",
            str(output),
            "-ArtifactSuffix",
            "test123",
            *(["-DevRoot", str(dev_root)] if explicit_root else []),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    archive = output / "M110-portable-test123.zip"
    assert archive.is_file()
    with zipfile.ZipFile(archive) as package:
        assert package.read("M110/M110.exe") == b"fake executable"
        assert package.read("M110/_internal/marker.txt") == b"bundled"
        launcher = package.read("run-dev.cmd").decode("ascii")
    default_root = str(dev_root) if explicit_root else r"%LOCALAPPDATA%\M110-dev"
    assert f'if "%M110_DEV_ROOT%"=="" set "M110_DEV_ROOT={default_root}"' in launcher
    assert 'set "USERPROFILE=%M110_DEV_ROOT%\\home"' in launcher
    assert 'set "M110_DATA_ROOT=%M110_DEV_ROOT%\\data"' in launcher
    assert 'start "" "%~dp0M110\\M110.exe"' in launcher
    assert (app / "M110.exe").read_bytes() == b"fake executable"
    # Exercise the recipient-side expansion, replacing only the executable
    # launch with an environment report (the fixture is not a real executable).
    runnable = tmp_path / "run-test.cmd"
    runnable.write_text(launcher.replace('start "" "%~dp0M110\\M110.exe"',
                                       "echo ROOT=%M110_DATA_ROOT%"), encoding="ascii")
    for override in (None, tmp_path / "runtime override"):
        env = dict(os.environ, LOCALAPPDATA=str(tmp_path / "recipient"))
        env.pop("M110_DEV_ROOT", None)
        if override:
            env["M110_DEV_ROOT"] = str(override)
        expected = override or (dev_root if explicit_root else tmp_path / "recipient/M110-dev")
        launched = subprocess.run(["cmd", "/d", "/c", str(runnable)], env=env,
                                  capture_output=True, text=True)
        assert launched.returncode == 0, launched.stdout + launched.stderr
        assert f"ROOT={expected / 'data'}" in launched.stdout
        assert (expected / "home").is_dir()
        assert (expected / "data").is_dir()


def test_packaging_failure_preserves_callers_environment(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "packaging/windows/build_portable.ps1"
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    # Invoke in the SAME PowerShell process: child processes cannot reveal leaked
    # environment changes in an interactive caller.
    command = """
$env:USERPROFILE = 'sentinel-home'
$env:M110_DATA_ROOT = 'sentinel-data'
try {
    & $args[0] -PackageOnly -AppDirectory $args[1] -DevRoot $args[2]
    exit 2
} catch {
    if ($env:USERPROFILE -ne 'sentinel-home' -or
        $env:M110_DATA_ROOT -ne 'sentinel-data') { exit 3 }
}
"""
    harness = tmp_path / "failure.ps1"
    harness.write_text(command, encoding="utf-8")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
         str(script), str(tmp_path / "missing-app"), str(blocked / "dev")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
