"""Celestron Origin folder ingest (temporary fixtures, never live captures)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from m110 import catalog, config, ingest
from tests._helpers import add_library, seed_root


TARGET = "Pacman Nebula - NGC 281"


def _origin_fits(path: Path, imagetyp: str, *, obj: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(np.zeros((2, 2), dtype="uint16"))
    hdr = hdu.header
    hdr["CREATOR"] = "Origin 1.5.6240"
    hdr["TELESCOP"] = "RASA6F2"
    hdr["MOUNT"] = "Origin"
    hdr["CAMERA"] = "Origin178-test"
    hdr["IMAGETYP"] = imagetyp
    hdr["OBJECT"] = obj or (TARGET if imagetyp == "LIGHT" else imagetyp)
    hdr["DATE-OBS"] = "2026-09-26T21:01:08"
    hdr["TIMEZONE"] = "-0400"
    hdr["EXPTIME"] = 15.0
    hdr["FILTER"] = "Nebula"
    hdr["BAYERPAT"] = "RGGB"
    hdu.writeto(path)


def _origin_session(src: Path, *, folder: str, include_info: bool = True,
                    info_target: str | None = TARGET,
                    fits_target: str | None = None) -> Path:
    session = src / folder
    light_target = fits_target if fits_target is not None else info_target
    _origin_fits(session / "Light0001.fits", "LIGHT", obj=light_target)
    _origin_fits(session / "Light0002.fits", "LIGHT", obj=light_target)
    _origin_fits(session / "dark0007.fits", "DARK")
    _origin_fits(session / "bias0007.fits", "BIAS")
    _origin_fits(session / "flat.fits", "FLAT")
    (session / "dark0007.json").write_text(json.dumps({"cameraType": "Origin178"}))
    (session / "FinalStackedMaster.tiff").write_bytes(b"small TIFF fixture")
    if include_info:
        (session / "info.json").write_text(json.dumps({
            "StackedInfo": {
                "objectName": info_target,
                "dateTime": "2026-09-26T21:01:08-04:00",
                "filter": "Nebula",
                "stackedDepth": 2,
            },
        }))
    return session


def _by_kind(ops):
    grouped = {}
    for op in ops:
        grouped.setdefault(op.kind, []).append(op)
    return grouped


def _relative_names(root: Path, ops):
    return {Path(op.dest).relative_to(root).as_posix() for op in ops}


def test_origin_session_routes_stamped_frames_stacks_and_sidecars(tmp_path, monkeypatch):
    """Without Origin routing, reused light names collide and session files hit Inbox."""
    root = seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder="Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08")

    ops = ingest.scan_directory_plan(src)
    by_kind = _by_kind(ops)
    names = _relative_names(root, ops)

    assert {op.layout for op in ops} == {"origin"}
    assert "unassigned" not in by_kind
    assert {
        "Images/NGC 281/lights/Light0001_20260926-210108.fits",
        "Images/NGC 281/lights/Light0002_20260926-210108.fits",
        "Images/NGC 281/darks/dark0007_20260926-210108.fits",
        "Images/NGC 281/biases/bias0007_20260926-210108.fits",
        "Images/NGC 281/flats/flat_20260926-210108.fits",
        "Images/NGC 281/seestar-stacks/20260926-210108_FinalStackedMaster.tiff",
        "Images/NGC 281/seestar-stacks/20260926-210108_info.json",
        "Images/NGC 281/seestar-stacks/20260926-210108_dark0007.json",
    } <= names
    assert all(op.action == "copy" for op in ops)


def test_origin_sessions_with_reused_light_names_import_once_each(tmp_path, monkeypatch):
    """A second Origin session must add its subs, while reimporting one adds none."""
    root = seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder="Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08")
    _origin_session(src, folder="Pacman_Nebula_-_NGC_281_2026-09-29_01-31-17")

    first = ingest.scan_directory_plan(src)
    light_names = {
        Path(op.dest).name for op in first if op.kind == "light"
    }
    assert light_names == {
        "Light0001_20260926-210108.fits",
        "Light0002_20260926-210108.fits",
        "Light0001_20260929-013117.fits",
        "Light0002_20260929-013117.fits",
    }

    result = ingest.apply_ops(first)
    assert result == {"moved": len(first), "skipped": 0, "cancelled": False}
    assert ingest.scan_directory_plan(src) == []
    assert sorted(p.name for p in config.lights_dir("NGC 281").iterdir()) == sorted(light_names)


def test_origin_creator_header_recognizes_a_session_without_info_json(tmp_path, monkeypatch):
    """Current Origin FITS headers are a supported fallback when info.json is absent."""
    root = seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(
        src,
        folder="Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08",
        include_info=False,
    )

    ops = ingest.scan_directory_plan(src)
    assert ops
    assert {op.layout for op in ops} == {"origin"}
    assert all(op.object == "NGC 281" for op in ops)
    assert "unassigned" not in _by_kind(ops)


@pytest.mark.parametrize(
    ("folder", "include_info", "info_target", "fits_target", "expected"),
    [
        ("Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08", True,
         "Pacman Nebula - NGC 281", "Andromeda Galaxy", "NGC 281"),
        ("Heart_Nebula_-_IC_1805_2026-09-26_21-01-08", True,
         "Heart Nebula - IC 1805", "Andromeda Galaxy", "Sh2-190"),
        ("Andromeda_Galaxy_2026-09-26_21-01-08", False,
         None, "Andromeda Galaxy", "M31"),
        ("Andromeda_Galaxy_2026-09-26_21-01-08", False,
         None, "Unknown", "M31"),
        ("Mystery_Field_2026-09-26_21-01-08", True,
         "Mystery Field", "Andromeda Galaxy", "Mystery Field"),
        ("Cave_Nebula_-_Sh2-155_2026-09-26_21-01-08", True,
         "Cave Nebula - Sh2-155", "Unknown", "Sh2-155"),
    ],
)
def test_origin_resolves_json_fits_and_folder_target_sources(
        tmp_path, monkeypatch, folder, include_info, info_target, fits_target, expected):
    """Origin JSON, FITS, then folder labels map to one catalog target."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder=folder, include_info=include_info,
                    info_target=info_target, fits_target=fits_target)

    ops = ingest.scan_directory_plan(src)

    assert ops
    assert {op.object for op in ops} == {expected}


@pytest.mark.parametrize(
    ("folder", "target", "slug", "object_id", "object_type", "object_name"),
    [
        ("Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08",
         "Pacman Nebula - NGC 281", "ngc-281", "NGC 281", "emission",
         "Pacman Nebula"),
        ("Heart_Nebula_-_IC_1805_2026-09-26_21-01-08",
         "Heart Nebula - IC 1805", "sh2-190", "Sh2-190", "emission",
         "Heart Nebula"),
        ("Andromeda_Galaxy_2026-09-26_21-01-08",
         "Andromeda Galaxy", "m31", "M31", "galaxy", "Andromeda Galaxy"),
    ],
)
def test_origin_canonical_target_promotes_the_seeded_catalog_details(
        tmp_path, monkeypatch, folder, target, slug, object_id, object_type, object_name):
    """Origin adds catalog facts and fills a blank Library name from its JSON label."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder=folder, info_target=target)

    ops = ingest.scan_directory_plan(src)
    ingest.apply_ops(ops)
    assert catalog.add_captured_objects(resolve_coords=False) == [slug]

    entry = catalog.load_library()[slug]
    assert entry["id"] == object_id
    assert entry["type"] == object_type
    assert entry.get("name") == object_name


def test_origin_json_name_does_not_replace_a_curated_catalog_name(tmp_path, monkeypatch):
    """A source label only fills the blank catalog-name field."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(
        src,
        folder="Andromeda_Galaxy_2026-09-26_21-01-08",
        info_target="Camera Label - M 31",
    )

    ingest.apply_ops(ingest.scan_directory_plan(src))
    assert catalog.add_captured_objects(resolve_coords=False) == ["m31"]
    assert catalog.load_library()["m31"]["name"] == "Andromeda Galaxy"


@pytest.mark.parametrize("name", ["", "   ", "My curated Pacman"])
def test_origin_refresh_backfills_only_missing_existing_name(tmp_path, monkeypatch, name):
    root = seed_root(tmp_path, monkeypatch)
    add_library(root, {"ngc-281": {"id": "NGC 281", "name": name,
                                  "custom": "preserve me"}})
    src = tmp_path / "Origin"
    _origin_session(src, folder="Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08")
    ingest.apply_ops(ingest.scan_directory_plan(src))

    assert catalog.add_captured_objects(resolve_coords=False) == []
    entry = catalog.load_library()["ngc-281"]
    assert entry["name"] == (name if name.strip() else "Pacman Nebula")
    assert entry["custom"] == "preserve me"
    assert catalog.add_captured_objects(resolve_coords=False) == []


def test_remapped_origin_sessions_keep_stamps_and_reimport_once(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    for date in ("2026-09-26_21-01-08", "2026-09-29_01-31-17"):
        _origin_session(src, folder=f"Pacman_Nebula_-_NGC_281_{date}")

    def remapped_plan():
        return [op for group in ingest.group_ops(ingest.scan_directory_plan(src))
                for op in ingest.retarget(group, "M31").ops]

    plan = remapped_plan()
    assert len({op.dest for op in plan}) == 16
    assert {Path(op.dest).name for op in plan if op.kind == "light"} == {
        "Light0001_20260926-210108.fits", "Light0002_20260926-210108.fits",
        "Light0001_20260929-013117.fits", "Light0002_20260929-013117.fits",
    }
    assert ingest.apply_ops(plan)["moved"] == 16
    assert ingest.apply_ops(remapped_plan())["moved"] == 0
    assert len(list(config.target_dir("M31").rglob("*.*"))) == 16


def test_origin_remembered_folder_alias_wins_on_rescan(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    session = _origin_session(
        src, folder="Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08")
    ingest.add_alias(session.name, "M31")
    plan = ingest.scan_directory_plan(src)
    assert {op.object for op in plan} == {"M31"}
    ingest.apply_ops(plan)
    assert ingest.scan_directory_plan(src) == []


def test_origin_backfill_preserves_added_slugs_and_matches_designation(tmp_path, monkeypatch):
    root = seed_root(tmp_path, monkeypatch)
    add_library(root, {"sh2-155": {"id": "Sh2-155", "name": ""},
                       "ngc-281": {"id": "NGC 281", "name": ""}})
    src = tmp_path / "Origin"
    _origin_session(src, folder="Cave_Nebula_-_Sh2-155_2026-09-26_21-01-08",
                    info_target="Cave Nebula - Sh2-155")
    _origin_session(src, folder="Andromeda_Galaxy_2026-09-26_21-01-08",
                    info_target="Andromeda Galaxy")
    ingest.apply_ops(ingest.scan_directory_plan(src))
    stacks = config.target_dir("NGC 281") / "seestar-stacks"
    stacks.mkdir(parents=True)
    (stacks / "info.json").write_text(json.dumps({
        "StackedInfo": {"objectName": "Wrong name - M31"},
    }))

    assert catalog.add_captured_objects(resolve_coords=False) == ["m31"]
    library = catalog.load_library()
    assert library["sh2-155"]["name"] == "Cave Nebula"
    assert library["ngc-281"]["name"] == ""
    assert library["m31"]["name"] == "Andromeda Galaxy"


def test_loose_fits_layout_probe_can_cancel_between_headers(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "loose"
    src.mkdir()
    for number in range(3):
        fits.PrimaryHDU().writeto(src / f"frame{number}.fits")
    original = ingest.frame_info
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(ingest, "frame_info", read)
    with pytest.raises(ingest.IngestCancelled):
        ingest.scan_directory_plan(src, should_cancel=lambda: bool(reads))
    assert len(reads) == 1


def test_loose_fits_logging_does_not_repeat_layout_probe(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "loose"
    src.mkdir()
    fits.PrimaryHDU().writeto(src / "frame.fits")
    original = ingest.frame_info
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(ingest, "frame_info", read)
    assert ingest.scan_directory_plan(src)
    assert len(reads) == 2  # one layout probe, one classifier read
