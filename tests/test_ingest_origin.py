"""Celestron Origin folder ingest (temporary fixtures, never live captures)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from m110 import calibration, catalog, config, ingest, siril
from tests._helpers import add_library, seed_root


TARGET = "Pacman Nebula - NGC 281"


def test_origin_aliases_stay_inside_the_fixture_store(tmp_path, monkeypatch):
    root = seed_root(tmp_path, monkeypatch)
    ingest.add_alias("Pacman test", "NGC 281")
    assert (root / config.INTERNAL_DIRNAME / "ingest_aliases.toml").is_file()


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


@pytest.mark.parametrize("separate_scans", [False, True])
def test_origin_calibration_dedups_by_content_and_remains_in_prep(
        tmp_path, monkeypatch, separate_scans):
    """Repeated masters must not multiply across sessions or disappear from prep."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    first = _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    second = _origin_session(src, folder="Pacman_2026-09-29_01-31-17")
    before = {p: p.read_bytes() for p in src.rglob("*") if p.is_file()}
    if separate_scans:
        ingest.apply_ops(ingest.scan_directory_plan(first))
        ops = ingest.scan_directory_plan(second)
        assert not any(op.kind in ("dark", "flat", "bias") for op in ops)
    else:
        ops = ingest.scan_directory_plan(src)
        assert len([op for op in ops if op.kind in ("dark", "flat", "bias")]) == 3
    ingest.apply_ops(ops)
    assert len(list(config.lights_dir("NGC 281").iterdir())) == 4
    for tier in ("darks", "flats", "biases"):
        assert len(list((config.target_dir("NGC 281") / tier).iterdir())) == 1
    plan = siril.plan_prep("NGC 281")
    assert plan.total_lights == 4
    assert set(plan.calib_kinds) == {"darks", "flats", "biases"}
    assert len(plan.calib_links) == 3
    for stamp in ("20260926-210108", "20260929-013117"):
        stored = config.seestar_stacks_dir("NGC 281") / f"{stamp}_dark0007.json"
        assert stored.read_bytes() == (first / "dark0007.json").read_bytes()
    assert ingest.scan_directory_plan(src) == []
    assert all(p.read_bytes() == data for p, data in before.items())


def test_origin_changed_calibration_with_same_session_name_is_retained(tmp_path, monkeypatch):
    """A filename match must not hide changed bytes, including a re-exported session."""
    seed_root(tmp_path, monkeypatch)
    session = _origin_session(tmp_path, folder="Pacman_2026-09-26_21-01-08")
    ingest.apply_ops(ingest.scan_directory_plan(session))
    with fits.open(session / "dark0007.fits", mode="update") as hdus:
        hdus[0].data[0, 0] = 42
    ops = ingest.scan_directory_plan(session)
    assert [op.kind for op in ops] == ["dark"]
    ingest.apply_ops(ops)
    darks = list(config.darks_dir("NGC 281").iterdir())
    assert len(darks) == 2
    assert {int(fits.getdata(p)[0, 0]) for p in darks} == {0, 42}
    assert ingest.scan_directory_plan(session) == []


def test_origin_calibration_apply_rechecks_independently_scanned_plans(tmp_path, monkeypatch):
    """Two previews made before confirmation must still store a master only once."""
    seed_root(tmp_path, monkeypatch)
    a = _origin_session(tmp_path, folder="Pacman_2026-09-26_21-01-08")
    b = _origin_session(tmp_path, folder="Pacman_2026-09-29_01-31-17")
    plans = ingest.scan_directory_plan(a) + ingest.scan_directory_plan(b)
    result = ingest.apply_ops(plans)
    assert result["skipped"] == 3
    assert len(list(config.darks_dir("NGC 281").iterdir())) == 1
    assert ingest.scan_directory_plan(tmp_path) == []


def test_origin_root_calibration_uses_device_library_and_prep(tmp_path, monkeypatch):
    """Root masters have no target: infer the single sibling telescope, then match ISO."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    session = _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    for name in ("dark0007.fits", "bias0007.fits", "flat.fits"):
        (session / name).unlink()  # Exercise the library-only case.
    for name, kind in (("dark0007.fits", "DARK"), ("bias0007.fits", "BIAS"),
                       ("flat.fits", "FLAT")):
        path = src / "Calibration" / name
        _origin_fits(path, kind)
        with fits.open(path, mode="update") as hdus:
            del hdus[0].header["TELESCOP"]
            hdus[0].header["ISO"] = 200
            hdus[0].header["XBINNING"] = 1
    for path in session.glob("Light*.fits"):
        with fits.open(path, mode="update") as hdus:
            hdus[0].header["ISO"] = 200
            hdus[0].header["XBINNING"] = 1
    ops = ingest.scan_directory_plan(src)
    masters = [op for op in ops if op.kind.startswith("cal-")]
    assert {op.kind for op in masters} == {"cal-dark", "cal-flat", "cal-bias"}
    assert all(op.object == "RASA6F2" and not op.new_object for op in masters)
    assert all(op.action == "copy" for op in masters)
    ingest.apply_ops(ops)
    assert calibration.match_for_target("NGC 281").as_dict() == {
        "darks": config.calibration_dir("RASA6F2", "darks") / "dark0007.fits",
        "flats": config.calibration_dir("RASA6F2", "flats") / "flat.fits",
        "biases": config.calibration_dir("RASA6F2", "biases") / "bias0007.fits",
    }
    assert siril.plan_prep("NGC 281").calib_source == "library"
    assert ingest.scan_directory_plan(src) == []


def test_origin_root_calibration_without_a_device_is_held(tmp_path, monkeypatch):
    """A model name must never be invented for an unidentified calibration export."""
    seed_root(tmp_path, monkeypatch)
    path = tmp_path / "Origin" / "Calibration" / "dark0007.fits"
    _origin_fits(path, "DARK")
    with fits.open(path, mode="update") as hdus:
        del hdus[0].header["TELESCOP"]
    ops = ingest.scan_directory_plan(path.parent.parent)
    assert [op.kind for op in ops] == ["unassigned"]
    assert not any(op.object == "DARK" for op in ops)


def test_origin_root_calibration_ambiguous_device_is_held(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    first = _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    second = _origin_session(src, folder="Pacman_2026-09-29_01-31-17")
    for light in second.glob("Light*.fits"):
        with fits.open(light, mode="update") as hdus:
            hdus[0].header["TELESCOP"] = "Another Origin"
    shared = src / "Calibration" / "dark0007.fits"
    shared.parent.mkdir()
    shared.write_bytes((first / "dark0007.fits").read_bytes())
    with fits.open(shared, mode="update") as hdus:
        del hdus[0].header["TELESCOP"]
    ops = ingest.scan_directory_plan(src)
    assert [op.kind for op in ops if Path(op.src) == shared] == ["unassigned"]
    assert not any(op.kind.startswith("cal-") for op in ops)


def test_origin_shared_masters_are_not_assigned_across_two_cameras_of_one_model(
        tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    second = _origin_session(src, folder="Pacman_2026-09-29_01-31-17")
    for light in second.glob("Light*.fits"):
        with fits.open(light, mode="update") as hdus:
            hdus[0].header["CAMERA"] = "Origin178-another-unit"
    path = src / "Calibration" / "dark0007.fits"
    _origin_fits(path, "DARK")
    with fits.open(path, mode="update") as hdus:
        del hdus[0].header["TELESCOP"]
    ops = ingest.scan_directory_plan(src)
    assert [op.kind for op in ops if Path(op.src) == path] == ["unassigned"]


def test_origin_shared_masters_are_not_assigned_across_cameras_in_one_session(
        tmp_path, monkeypatch):
    """Every sibling light matters when attributing a root master."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    session = _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    with fits.open(session / "Light0002.fits", mode="update") as hdus:
        hdus[0].header["CAMERA"] = "Origin178-another-unit"
    path = src / "Calibration" / "dark0007.fits"
    _origin_fits(path, "DARK")
    with fits.open(path, mode="update") as hdus:
        del hdus[0].header["TELESCOP"]
    ops = ingest.scan_directory_plan(src)
    assert [op.kind for op in ops if Path(op.src) == path] == ["unassigned"]


def test_origin_empty_root_calibration_changes_nothing(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    before = ingest.scan_directory_plan(src)
    (src / "Calibration").mkdir()
    assert ingest.scan_directory_plan(src) == before


def test_origin_shared_master_can_gain_its_sidecar_on_a_later_import(tmp_path, monkeypatch):
    """Content dedup must not discard newly supplied ISO metadata for an existing master."""
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    path = src / "Calibration" / "dark0007.fits"
    _origin_fits(path, "DARK")
    ingest.apply_ops(ingest.scan_directory_plan(src))
    path.with_suffix(".json").write_text(json.dumps({"iso": 200}))
    ops = ingest.scan_directory_plan(src)
    assert [Path(op.dest).name for op in ops] == ["dark0007.json"]
    ingest.apply_ops(ops)
    stored = config.calibration_dir("RASA6F2", "darks") / "dark0007.fits"
    assert calibration.frame_facts(stored)["iso"] == 200
    assert ingest.scan_directory_plan(src) == []


@pytest.mark.parametrize("layout", ["origin", "store"])
@pytest.mark.parametrize("same_master", [False, True])
def test_calibration_json_follows_master_from_independent_previews(
        tmp_path, monkeypatch, layout, same_master):
    """A stale preview must never give one master's ISO to another master."""
    seed_root(tmp_path, monkeypatch)
    suffix = Path("Calibration/RASA6F2/darks" if layout == "store" else "Calibration")
    incoming = tmp_path / "incoming" / suffix / "dark0007.fits"
    retained = tmp_path / "first" / suffix / ("retained.fits" if same_master else "dark0007.fits")
    _origin_fits(incoming, "DARK")
    incoming.with_suffix(".json").write_text('{"iso": 200}')
    _origin_fits(retained, "DARK")
    if not same_master:
        with fits.open(retained, mode="update") as hdus:
            hdus[0].header["EXPTIME"] = 30.0
    pending = ingest.scan_directory_plan(tmp_path / "incoming")
    first = ingest.scan_directory_plan(tmp_path / "first")
    ingest.apply_ops(first)
    ingest.apply_ops(pending)
    directory = config.calibration_dir("RASA6F2", "darks")
    if same_master:
        master = directory / "retained.fits"
        assert sorted(p.name for p in directory.iterdir()) == ["retained.fits", "retained.json"]
    else:
        master = directory / "dark0007_1.fits"
        assert calibration.frame_facts(directory / "dark0007.fits")["iso"] is None
        assert not (directory / "dark0007.json").exists()
        assert calibration.frame_facts(master)["exptime"] == 15.0
    assert calibration.frame_facts(master)["iso"] == 200
    assert incoming.exists()          # device/store Browse imports remain copies
    assert ingest.scan_directory_plan(tmp_path / "incoming") == []


@pytest.mark.parametrize("late_metadata", [False, True])
def test_store_calibration_keeps_paired_json_and_late_metadata(
        tmp_path, monkeypatch, late_metadata):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "foreign" / "Calibration" / "RASA6F2" / "darks" / "master.fits"
    _origin_fits(src, "DARK")
    if late_metadata:
        ingest.apply_ops(ingest.scan_directory_plan(tmp_path / "foreign"))
    src.with_suffix(".JSON").write_text('{"iso": 200}')
    ops = ingest.scan_directory_plan(tmp_path / "foreign")
    assert [Path(op.dest).name for op in ops] == (
        ["master.json"] if late_metadata else ["master.fits", "master.json"])
    ingest.apply_ops(ops)
    stored = config.calibration_dir("RASA6F2", "darks") / "master.fits"
    assert calibration.frame_facts(stored)["iso"] == 200
    assert ingest.scan_directory_plan(tmp_path / "foreign") == []


@pytest.mark.parametrize("layout", ["origin", "store"])
def test_calibration_stale_json_preserves_existing_settings(tmp_path, monkeypatch, layout):
    seed_root(tmp_path, monkeypatch)
    suffix = Path("Calibration/RASA6F2/darks" if layout == "store" else "Calibration")
    src = tmp_path / "source" / suffix / "dark.fits"
    _origin_fits(src, "DARK")
    src.with_suffix(".json").write_text('{"iso": 200}')
    pending = ingest.scan_directory_plan(tmp_path / "source")
    # A separate import wins confirmation first, supplying retained settings.
    ingest.apply_ops([op for op in pending if config.is_fits_file(op.src)])
    stored = config.calibration_dir("RASA6F2", "darks") / "dark.fits"
    stored.with_suffix(".JSON").write_text('{"iso": 400}')
    ingest.apply_ops(pending)
    assert calibration.frame_facts(stored)["iso"] == 400
    assert sorted(p.suffix.lower() for p in stored.parent.iterdir()) == [".fits", ".json"]


def test_store_calibration_move_keeps_json_paired_after_collision(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "foreign" / "Calibration" / "RASA6F2" / "darks" / "master.fits"
    _origin_fits(src, "DARK")
    src.with_suffix(".json").write_text('{"iso": 200}')
    ops = ingest.scan_directory_plan(tmp_path / "foreign", action="move")
    stored = config.calibration_dir("RASA6F2", "darks") / "master.fits"
    _origin_fits(stored, "DARK")
    with fits.open(stored, mode="update") as hdus:
        hdus[0].header["EXPTIME"] = 30.0
    ingest.apply_ops(ops)
    assert not src.exists()
    assert not src.with_suffix(".json").exists()
    assert calibration.frame_facts(stored)["iso"] is None
    assert calibration.frame_facts(stored.with_name("master_1.fits"))["iso"] == 200


def test_store_calibration_move_finishes_metadata_pair_when_cancelled(
        tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "foreign" / "Calibration" / "RASA6F2" / "darks" / "master.fits"
    _origin_fits(src, "DARK")
    src.with_suffix(".json").write_text('{"iso": 200}')
    ops = ingest.scan_directory_plan(tmp_path / "foreign", action="move")
    cancellation_requested = False

    def progress(index, _total):
        nonlocal cancellation_requested
        if index == 1:
            cancellation_requested = True

    result = ingest.apply_ops(
        ops, progress=progress, should_cancel=lambda: cancellation_requested)

    stored = config.calibration_dir("RASA6F2", "darks") / "master.fits"
    assert result == {"moved": 2, "skipped": 0, "cancelled": True}
    assert not src.exists()
    assert not src.with_suffix(".json").exists()
    assert calibration.frame_facts(stored)["iso"] == 200
    assert ingest.scan_directory_plan(tmp_path / "foreign") == []


@pytest.mark.parametrize("stale_preview", [False, True])
@pytest.mark.parametrize("occupied", ["json", "fit", "directory"])
def test_calibration_new_master_does_not_claim_an_occupied_metadata_stem(
        tmp_path, monkeypatch, stale_preview, occupied):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin" / "Calibration" / "master.fits"
    _origin_fits(src, "DARK")
    src.with_suffix(".json").write_text('{"iso": 200}')
    if stale_preview:
        ops = ingest.scan_directory_plan(tmp_path / "Origin")
    directory = config.calibration_dir("RASA6F2", "darks")
    directory.mkdir(parents=True, exist_ok=True)
    if occupied == "json":
        (directory / "master.json").write_text('{"iso": 400}')
    elif occupied == "fit":
        _origin_fits(directory / "master.fit", "DARK")
        with fits.open(directory / "master.fit", mode="update") as hdus:
            hdus[0].header["EXPTIME"] = 30.0
    else:
        (directory / "master.json").mkdir()
    if not stale_preview:
        ops = ingest.scan_directory_plan(tmp_path / "Origin")
    ingest.apply_ops(ops)
    stored = directory / "master_1.fits"
    assert stored.is_file()
    assert calibration.frame_facts(stored)["iso"] == 200
    if occupied == "fit":
        assert calibration.frame_facts(directory / "master.fit")["iso"] is None
    if occupied == "json":
        assert (directory / "master.json").read_text() == '{"iso": 400}'
    assert ingest.scan_directory_plan(tmp_path / "Origin") == []


def test_calibration_json_directory_never_creates_an_orphan_sidecar(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin" / "Calibration" / "master.fits"
    _origin_fits(src, "DARK")
    src.with_suffix(".json").write_text('{"iso": 200}')
    ops = ingest.scan_directory_plan(tmp_path / "Origin")
    ingest.apply_ops([op for op in ops if config.is_fits_file(op.src)])
    directory = config.calibration_dir("RASA6F2", "darks")
    (directory / "master.json").mkdir()
    result = ingest.apply_ops(ops)
    assert result == {"moved": 0, "skipped": 2, "cancelled": False}
    assert not (directory / "master_1.json").exists()
    assert (directory / "master.json").is_dir()


def test_calibration_json_without_a_retained_master_is_not_copied(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin" / "Calibration" / "dark.fits"
    _origin_fits(src, "DARK")
    src.with_suffix(".json").write_text('{"iso": 200}')
    ops = ingest.scan_directory_plan(tmp_path / "Origin")
    sidecars = [op for op in ops if Path(op.src).suffix == ".json"]
    assert len(sidecars) == 1
    result = ingest.apply_ops(sidecars)
    assert result == {"moved": 0, "skipped": 1, "cancelled": False}
    assert not Path(sidecars[0].dest).exists()


def test_origin_calibration_hashing_cancellation_leaves_no_files(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    session = _origin_session(tmp_path, folder="Pacman_2026-09-26_21-01-08")
    op = next(op for op in ingest.scan_directory_plan(session) if op.kind == "dark")
    calls = 0

    def cancel_during_hash():
        nonlocal calls
        calls += 1
        return calls >= 2

    result = ingest.apply_ops([op], should_cancel=cancel_during_hash)
    assert result == {"moved": 0, "skipped": 0, "cancelled": True}
    assert not Path(op.dest).exists()
    with pytest.raises(ingest.IngestCancelled):
        ingest._digest(op.src, should_cancel=lambda: True)


def test_origin_calibration_dedup_is_target_scoped(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    src = tmp_path / "Origin"
    _origin_session(src, folder="Pacman_2026-09-26_21-01-08")
    _origin_session(src, folder="Andromeda_2026-09-29_01-31-17",
                    info_target="Andromeda Galaxy")
    ingest.apply_ops(ingest.scan_directory_plan(src))
    assert len(list(config.darks_dir("NGC 281").iterdir())) == 1
    assert len(list(config.darks_dir("M31").iterdir())) == 1


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
    assert len({op.dest for op in plan}) == 13  # three identical calibration files shared
    assert {Path(op.dest).name for op in plan if op.kind == "light"} == {
        "Light0001_20260926-210108.fits", "Light0002_20260926-210108.fits",
        "Light0001_20260929-013117.fits", "Light0002_20260929-013117.fits",
    }
    assert ingest.apply_ops(plan)["moved"] == 13
    assert ingest.apply_ops(remapped_plan())["moved"] == 0
    assert len(list(config.target_dir("M31").rglob("*.*"))) == 13


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
