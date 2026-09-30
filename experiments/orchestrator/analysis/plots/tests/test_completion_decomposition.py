"""Tests for the completion_decomposition figure (iter-4d.0b)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from experiments.orchestrator.analysis.plots import completion_decomposition as cd


def _run_dir(tmp_path: Path, baseline: str, block: dict | None) -> Path:
    """Hand-craft a minimal run-dir with a completion_decomposition block."""
    run_dir = tmp_path / baseline
    adir = run_dir / "analysis"
    adir.mkdir(parents=True)
    proj: dict = {}
    if block is not None:
        proj["completion_decomposition"] = block
    (adir / "summary.json").write_text(json.dumps({
        "baseline": baseline,
        "baselines": {baseline: proj},
    }))
    return run_dir


def test_prepare_pulls_wait_and_exec(tmp_path: Path) -> None:
    dirs = [
        _run_dir(tmp_path, "heuristic-scorer",
                 {"status": "ok", "median_wait_s": 4.0, "median_exec_s": 60.0}),
        _run_dir(tmp_path, "delphi-full",
                 {"status": "ok", "median_wait_s": 150.0, "median_exec_s": 62.0}),
    ]
    df = cd._prepare(dirs)
    assert set(df.columns) == {"baseline_key", "wait_s", "exec_s"}
    delphi = df[df["baseline_key"] == "delphi-full"].iloc[0]
    heur = df[df["baseline_key"] == "heuristic-scorer"].iloc[0]
    # DELPHI's wait dwarfs its execution; the Heuristic's wait is tiny.
    assert delphi["wait_s"] == 150.0 and delphi["exec_s"] == 62.0
    assert heur["wait_s"] == 4.0
    # Executions are comparable — the figure's point.
    assert abs(delphi["exec_s"] - heur["exec_s"]) < 5.0


def test_pending_block_is_nan(tmp_path: Path) -> None:
    df = cd._prepare([_run_dir(tmp_path, "delphi-full", {"status": "pending"})])
    row = df.iloc[0]
    assert pd.isna(row["wait_s"])
    assert pd.isna(row["exec_s"])


def test_absent_block_is_nan(tmp_path: Path) -> None:
    df = cd._prepare([_run_dir(tmp_path, "static-karmada", None)])
    row = df.iloc[0]
    assert pd.isna(row["wait_s"])


def test_generate_writes_file(tmp_path: Path) -> None:
    dirs = [
        _run_dir(tmp_path, "static-karmada",
                 {"status": "ok", "median_wait_s": 5.0, "median_exec_s": 69.0}),
        _run_dir(tmp_path, "delphi-full",
                 {"status": "ok", "median_wait_s": 150.0, "median_exec_s": 62.0}),
    ]
    written = cd.generate(dirs, tmp_path / "decomp", formats=["pdf"],
                          synthetic_preview=True)
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name


def test_generate_all_pending_renders_message(tmp_path: Path) -> None:
    """When every run-dir's block is pending the figure still writes
    (with a guidance message), never crashes and never invents bars."""
    dirs = [_run_dir(tmp_path, "delphi-full", {"status": "pending"})]
    written = cd.generate(dirs, tmp_path / "empty", formats=["pdf"])
    assert len(written) == 1
    assert written[0].exists()


def test_component_palette_is_colourblind_safe() -> None:
    """T2: wait/exec colours route through the shared Okabe-Ito palette
    (orange wait, blue exec) instead of the old tab10 olive/blue."""
    from experiments.orchestrator.analysis.plots import _style
    assert cd.COMPONENT_COLORS["wait_s"] == _style.OKABE_ITO[1]  # orange
    assert cd.COMPONENT_COLORS["exec_s"] == _style.OKABE_ITO[0]  # blue
    assert cd.COMPONENT_COLORS["wait_s"] != "#bcbd22"            # not old olive
