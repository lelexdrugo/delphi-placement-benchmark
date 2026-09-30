"""Tests for sensitivity (P3 — fully synthetic curves)."""
from __future__ import annotations

from pathlib import Path

from experiments.orchestrator.analysis.plots import sensitivity


def test_prepare_shape(synthetic_run_dirs: list[Path]) -> None:
    df = sensitivity._prepare(synthetic_run_dirs)
    # 3 baselines × (7 staleness pts + 6 delay pts) = 39 rows.
    assert df.shape == (39, 4)
    assert set(df.columns) == {"baseline_key", "panel", "x", "y"}
    assert set(df["panel"]) == {"staleness", "delay"}


def test_static_karmada_is_state_blind(synthetic_run_dirs: list[Path]) -> None:
    """Static-Karmada's staleness curve must be flat at 1.0 — the
    baseline ignores cluster state entirely so staleness can't
    degrade it."""
    df = sensitivity._prepare(synthetic_run_dirs)
    static_stale = df[
        (df["baseline_key"] == "static-karmada") & (df["panel"] == "staleness")
    ]
    assert (static_stale["y"] == 1.0).all()


def test_delphi_full_most_sensitive_to_staleness(
    synthetic_run_dirs: list[Path],
) -> None:
    """DELPHI-Full reasons over history, so it's the most affected
    when the cluster snapshot is stale — its curve must drop fastest."""
    df = sensitivity._prepare(synthetic_run_dirs)
    delphi = df[(df["baseline_key"] == "delphi-full") & (df["panel"] == "staleness")]
    heuristic = df[(df["baseline_key"] == "heuristic-scorer") & (df["panel"] == "staleness")]
    # Compare at x=300s — the largest staleness sample.
    d_300 = float(delphi[delphi["x"] == 300.0]["y"].iloc[0])
    h_300 = float(heuristic[heuristic["x"] == 300.0]["y"].iloc[0])
    assert d_300 < h_300


def test_generate_writes_files(synthetic_run_dirs: list[Path], tmp_path: Path) -> None:
    written = sensitivity.generate(
        synthetic_run_dirs,
        tmp_path / "sn",
        formats=["pdf"],
        synthetic_preview=True,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name
