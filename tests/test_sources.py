"""Read-only device discovery against scratch mounts, never live telescope data."""
import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from m110 import config, ingest, sources


def _session(root, name="Pacman_2026-09-26_21-01-08", payload=None):
    session = root / name
    session.mkdir(parents=True)
    (session / "info.json").write_text(
        json.dumps({"StackedInfo": {}} if payload is None else payload), encoding="utf-8")
    return session


def test_origin_roots_use_session_metadata_not_volume_labels(tmp_path, monkeypatch):
    first = tmp_path / "UNTITLED"
    second = tmp_path / "Backup disk"
    _session(first)
    _session(second)
    (first / "Calibration").mkdir()
    (tmp_path / "Origin").mkdir()  # The name alone is not evidence.
    monkeypatch.setattr(sources, "mounted_roots", lambda: [first, second, first,
                                                          tmp_path / "Origin"])
    assert sources.find_origin_roots() == [first, second]


@pytest.mark.parametrize("payload", [{}, [], {"StackedInfo": None},
                                    {"StackedInfo": []}, {"Other": {}}])
def test_origin_roots_reject_unrelated_json(tmp_path, payload):
    _session(tmp_path, payload=payload)
    assert sources.find_origin_roots([tmp_path]) == []


@pytest.mark.parametrize("content", [b"not JSON", b"\xff", b"[" * 10000,
                                    b" " * (1024 * 1024) + b'{"StackedInfo": {}}'],
                         ids=["invalid-json", "invalid-utf8", "deep-json", "oversized-json"])
def test_origin_roots_skip_corrupt_or_oversized_metadata(tmp_path, content):
    session = _session(tmp_path)
    (session / "info.json").write_bytes(content)
    assert sources.find_origin_roots([tmp_path]) == []


def test_origin_discovery_does_not_recurse_or_probe_fits(tmp_path, monkeypatch):
    _session(tmp_path / "archive")
    _session(tmp_path, name=".hidden")

    def no_fits(*args, **kwargs):
        pytest.fail("Source discovery must not open FITS files")

    monkeypatch.setattr(ingest, "frame_info", no_fits)
    assert sources.find_origin_roots([tmp_path]) == []
    assert sources.find_origin_roots([tmp_path / "archive"]) == [tmp_path / "archive"]


def test_origin_discovery_continues_after_an_unreadable_root(tmp_path, monkeypatch):
    broken = tmp_path / "offline"
    broken.mkdir()
    valid = tmp_path / "USB"
    _session(valid)
    original = Path.iterdir

    def iterdir(path):
        if path == broken:
            raise PermissionError("unavailable volume")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert sources.find_origin_roots([broken, valid, tmp_path / "missing"]) == [valid]


def test_origin_discovery_skips_a_child_with_an_unreadable_type(tmp_path, monkeypatch):
    """A failed child stat must not discard all accessible siblings."""
    broken = tmp_path / "a-offline"
    broken.mkdir()
    _session(tmp_path, name="b-valid")
    original = Path.is_dir

    def is_dir(path):
        if path == broken:
            raise PermissionError("unavailable child")
        return original(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert sources.find_origin_roots([tmp_path]) == [tmp_path]


def test_origin_discovery_skips_an_unreadable_session(tmp_path, monkeypatch):
    broken = _session(tmp_path, name="a-offline")
    _session(tmp_path, name="b-valid")
    original = os.scandir

    def scandir(path):
        if Path(path) == broken:
            raise PermissionError("unavailable session")
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert ingest._origin_info(broken) is None
    assert sources.find_origin_roots([tmp_path]) == [tmp_path]


def test_origin_info_directory_does_not_hide_a_case_variant_file(tmp_path, monkeypatch):
    """Case-sensitive mounts may contain both an INFO.JSON dir and info.json file."""
    session = _session(tmp_path, payload={"StackedInfo": {"objectName": "NGC 281"}})
    original_scan = os.scandir
    original_is_file = Path.is_file
    entries = [SimpleNamespace(name="INFO.JSON", is_file=lambda: False),
               SimpleNamespace(name="info.json", is_file=lambda: True)]

    def scandir(path):
        if Path(path) == session:
            return nullcontext(iter(entries))
        return original_scan(path)

    def is_file(path):
        if path.parent == session and path.name == "INFO.JSON":
            return False
        return original_is_file(path)

    # Simulate a case-sensitive listing on Windows too; the JSON read is real.
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(Path, "is_file", is_file)
    assert ingest._origin_info(session) == {"objectName": "NGC 281"}
    assert sources.find_origin_roots([tmp_path]) == [tmp_path]


def test_origin_discovery_can_cancel_between_sessions(tmp_path, monkeypatch):
    _session(tmp_path, name="a-unrelated", payload={})
    _session(tmp_path, name="b-origin")
    reads = []
    original = ingest._origin_info

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(ingest, "_origin_info", read)
    with pytest.raises(ingest.IngestCancelled):
        sources.find_origin_roots([tmp_path], should_cancel=lambda: bool(reads))


def test_macos_mount_candidates_use_configured_volumes(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(config, "VOLUMES_DIR", tmp_path)
    (tmp_path / "USB").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "not-a-volume").write_text("x")
    assert sources.mounted_roots() == [tmp_path / "USB"]


def test_linux_mount_candidates_cover_user_and_direct_mounts(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(config, "VOLUMES_DIR", tmp_path / "missing")
    user_mounts = (tmp_path / "media", tmp_path / "run-media")
    direct_mounts = tmp_path / "mnt"
    monkeypatch.setattr(sources, "_LINUX_USER_MOUNTS", user_mounts)
    monkeypatch.setattr(sources, "_LINUX_MOUNTS", direct_mounts)
    for path in (user_mounts[0] / "rob" / "USB", user_mounts[1] / "rob" / "Origin",
                 direct_mounts / "scope"):
        path.mkdir(parents=True)
    assert sources.mounted_roots() == [user_mounts[0] / "rob" / "USB",
                                      user_mounts[1] / "rob" / "Origin",
                                      direct_mounts / "scope"]


def test_windows_mount_candidates_use_logical_drives(monkeypatch):
    import ctypes
    monkeypatch.setattr(sources, "sys", SimpleNamespace(platform="win32"))
    drives = SimpleNamespace(GetLogicalDrives=lambda: (1 << 2) | (1 << 23))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=drives), raising=False)
    assert sources.mounted_roots() == [Path("C:/"), Path("X:/")]
