"""Tests for cluster_state."""
from __future__ import annotations

from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import cluster_state


def test_prepare_shape(synthetic_run_dirs: list[Path]) -> None:
    df = cluster_state._prepare(synthetic_run_dirs)
    # 3 baselines × 48 jobs per baseline = 144 rows.
    assert df.shape == (144, 3)
    assert set(df.columns) == {"state_id", "duration_seconds", "baseline_key"}


def test_prepare_state_order_is_canonical(synthetic_run_dirs: list[Path]) -> None:
    df = cluster_state._prepare(synthetic_run_dirs)
    # The categorical dtype carries the canonical state order.
    categories = list(df["state_id"].cat.categories)
    assert categories == [
        "idle", "mixed-load",
        "skewed-edge-1-saturated", "skewed-on-prem-saturated",
    ]


def test_delphi_full_better_under_edge_skew(synthetic_run_dirs: list[Path]) -> None:
    """DELPHI-Full's median completion time under skewed-edge-1
    must be lower than Heuristic — the fixture's parametric model
    encodes this delta (108s heuristic vs 101s delphi)."""
    df = cluster_state._prepare(synthetic_run_dirs)
    state = "skewed-edge-1-saturated"
    h = df[(df["baseline_key"] == "heuristic-scorer") & (df["state_id"] == state)]
    d = df[(df["baseline_key"] == "delphi-full") & (df["state_id"] == state)]
    assert d["duration_seconds"].median() < h["duration_seconds"].median()


def test_generate_writes_files(synthetic_run_dirs: list[Path], tmp_path: Path) -> None:
    written = cluster_state.generate(
        synthetic_run_dirs,
        tmp_path / "cs",
        formats=["pdf"],
        synthetic_preview=True,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name


def test_generate_against_phase_a_single_baseline(
    phase_a_run_dir: Path, tmp_path: Path,
) -> None:
    """Real-data smoke: Phase-A has only DELPHI-Full, but the
    generator must still produce a non-empty file (single-baseline
    boxplot is degenerate but valid)."""
    if not phase_a_run_dir.exists():
        pytest.skip("Phase-A run-dir not present in this checkout")
    written = cluster_state.generate(
        [phase_a_run_dir],
        tmp_path / "cs-phase-a",
        formats=["pdf"],
        synthetic_preview=False,
    )
    assert written[0].stat().st_size > 1024


def test_prepare_falls_back_to_idle_when_state_id_empty(tmp_path: Path) -> None:
    """iter-4g.1 fallback: single-state campaigns (iter-4d) leave
    ``state_id`` empty across every row because the field has no
    per-row variation. ``_prepare`` must coerce those rows to the
    canonical ``"idle"`` label so the boxplot still renders instead
    of returning an empty positions list and crashing matplotlib."""
    import csv
    import json

    run_dir = tmp_path / "single-state-run"
    adir = run_dir / "analysis"
    adir.mkdir(parents=True)
    (adir / "summary.json").write_text(
        json.dumps({"baseline": "delphi-full", "baselines": {"delphi-full": {}}}),
        encoding="utf-8",
    )
    with (adir / "completion-time.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["job_name", "state_id", "workload_class", "intent_profile",
                    "applied_at_utc", "k8s_completion_time", "selected_cluster",
                    "duration_seconds"])
        for i in range(6):
            w.writerow([f"j{i}", "", "cpu", "balanced", "", "", "edge-1", 90.0 + i])

    df = cluster_state._prepare([run_dir])
    states = set(df["state_id"].dropna())
    assert states == {"idle"}, f"expected idle fallback, got {states}"
    assert len(df) == 6
