"""Opt-in real-sample copy/import checks; never modify the original captures.

Set M110_SAMPLE_ROOT to the Astrophotography directory to run these gates.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import types

import pytest

from m110 import calibration, config, ingest, siril, sources
from tests._helpers import seed_root


pytestmark = pytest.mark.skipif(not os.environ.get("M110_SAMPLE_ROOT"),
                                reason="opt-in real telescope samples")


def _sample():
    return (Path(os.environ["M110_SAMPLE_ROOT"]) / "Origin Source Files" / "2026-09-27"
            / "Pacman_Nebula_-_NGC_281_2026-09-26_21-01-08")


def _hashes(folder):
    out = {}
    for p in folder.iterdir():
        if p.is_file():
            with p.open("rb") as stream:
                out[p.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return out


def test_real_origin_sessions_dedup_without_changing_sources(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    sample = _sample()
    before = _hashes(sample)
    src = tmp_path / "source"
    shutil.copytree(sample, src / sample.name)
    shutil.copytree(sample, src / "Pacman_Nebula_-_NGC_281_2026-09-29_01-31-17")
    assert sources.find_origin_roots([src]) == [src]
    plan = ingest.scan_directory_plan(src)
    assert not any(op.kind == "unassigned" for op in plan)
    assert len([op for op in plan if op.kind in ("dark", "flat", "bias")]) == 3
    result = ingest.apply_ops(plan)
    assert result["moved"] == len(plan)
    assert len(list(config.lights_dir("NGC 281").iterdir())) == 20
    prep = siril.plan_prep("NGC 281")
    assert prep.total_lights == 20 and len(prep.calib_links) == 3
    assert ingest.scan_directory_plan(src) == []
    assert _hashes(sample) == before


def test_real_origin_library_only_dark_is_matched_with_json_iso(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    sample = _sample()
    src = tmp_path / "source"
    session = shutil.copytree(sample, src / sample.name)
    shared = src / "Calibration"
    shared.mkdir()
    for name in ("dark0007.fits", "dark0007.json", "bias0007.fits", "flat.fits"):
        shutil.move(str(session / name), str(shared / name))
    assert sources.find_origin_roots([src]) == [src]
    ops = ingest.scan_directory_plan(src)
    assert not any(op.kind == "unassigned" for op in ops)
    ingest.apply_ops(ops)
    match = calibration.match_for_target("NGC 281")
    assert match.darks == config.calibration_dir("RASA6F2", "darks") / "dark0007.fits"
    assert calibration.frame_facts(match.darks)["iso"] == 200
    # The real bias/flat lack ISO/GAIN evidence. Retain them, do not guess a match.
    assert match.biases is None and match.flats is None
    assert siril.plan_prep("NGC 281").calib_source == "library"
    assert ingest.scan_directory_plan(src) == []


def test_real_seestar_plan_matches_accepted_main(tmp_path, monkeypatch):
    seed_root(tmp_path, monkeypatch)
    sample = Path(os.environ["M110_SAMPLE_ROOT"]) / "SeeStar Source Files" / "2026-09-26"
    source = shutil.copytree(sample, tmp_path / "seestar")
    result = subprocess.run(["git", "show", "81c68a1:m110/ingest.py"],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    baseline = types.ModuleType("m110._origin_baseline_ingest")
    baseline.__package__ = "m110"
    monkeypatch.setitem(sys.modules, baseline.__name__, baseline)
    exec(compile(result.stdout, "<accepted-main-ingest>", "exec"), baseline.__dict__)

    def snapshot(ops):
        rows = []
        for op in ops:
            row = asdict(op)
            row.setdefault("master_src", "")  # additive pairing field; Seestar stays unpaired
            for key in ("src", "dest", "dest_rel"):
                row[key] = Path(row[key]).as_posix()
            rows.append(row)
        return rows

    expected = snapshot(baseline.scan_directory_plan(source))
    assert expected
    assert snapshot(ingest.scan_directory_plan(source)) == expected
