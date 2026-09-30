"""Tests for placement_effectiveness — D3 strategy.

Verify ``_prepare`` returns the expected per-(workload_class,
baseline) hard-pass rate and that ``generate`` writes a non-empty
file. PDF bytes are not compared.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import placement_effectiveness


def test_prepare_shape(synthetic_run_dirs: list[Path]) -> None:
    df = placement_effectiveness._prepare(synthetic_run_dirs)
    # 3 baselines × 3 workload classes.
    assert df.shape == (9, 4)
    assert set(df.columns) == {"workload_class", "hard_pass_rate", "n_jobs", "baseline_key"}


def test_prepare_baseline_ordering(synthetic_run_dirs: list[Path]) -> None:
    """Rows must be sorted by (workload_class, baseline_key) so the
    renderer can iterate without re-sorting."""
    df = placement_effectiveness._prepare(synthetic_run_dirs)
    # First three rows: cpu × (static, heuristic, delphi-full).
    first_classes = df["workload_class"].head(3).tolist()
    assert first_classes == ["cpu"] * 3
    first_baselines = df["baseline_key"].head(3).tolist()
    assert first_baselines == ["static-karmada", "heuristic-scorer", "delphi-full"]


def test_static_karmada_fails_latency_sensitive(synthetic_run_dirs: list[Path]) -> None:
    """Static-Karmada pins to public-cloud, so latency-sensitive jobs
    with region=edge must fail the hard gate. Synthetic fixture's
    intent_compliance.csv encodes hard_pass=0 for those rows; the
    per-class mean must reflect the failure."""
    df = placement_effectiveness._prepare(synthetic_run_dirs)
    static = df[df["baseline_key"] == "static-karmada"].set_index("workload_class")
    # 4 profiles per class: balanced, cost-aware, latency-sensitive, locality-aware.
    # Region-gated profiles: latency-sensitive (region=edge), locality-aware
    # (region=on-premises). Static picks public-cloud, so 2/4 fail per class.
    for cls in ("cpu", "memory", "network"):
        rate = float(static.loc[cls, "hard_pass_rate"])
        assert rate == pytest.approx(0.5, abs=0.01), f"{cls}: got {rate}"


def test_heuristic_passes_intent(synthetic_run_dirs: list[Path]) -> None:
    """Heuristic routes by region — every job should pass the hard gate."""
    df = placement_effectiveness._prepare(synthetic_run_dirs)
    heuristic = df[df["baseline_key"] == "heuristic-scorer"]
    assert (heuristic["hard_pass_rate"] == 1.0).all()


def test_generate_writes_files(synthetic_run_dirs: list[Path], tmp_path: Path) -> None:
    written = placement_effectiveness.generate(
        synthetic_run_dirs,
        tmp_path / "pe",
        formats=["pdf", "png"],
        synthetic_preview=True,
    )
    assert len(written) == 2
    for p in written:
        assert p.exists()
        assert p.stat().st_size > 1024  # arbitrary non-empty threshold
        assert ".SYNTHETIC." in p.name


def test_generate_no_watermark_when_not_preview(
    synthetic_run_dirs: list[Path], tmp_path: Path,
) -> None:
    written = placement_effectiveness.generate(
        synthetic_run_dirs,
        tmp_path / "pe",
        formats=["pdf"],
        synthetic_preview=False,
    )
    assert len(written) == 1
    assert ".SYNTHETIC." not in written[0].name


def test_prepare_handles_empty_run_dirs(tmp_path: Path) -> None:
    """Empty run-dir list must produce an empty DataFrame, not raise."""
    df = placement_effectiveness._prepare([])
    assert df.empty
    assert set(df.columns) == {"workload_class", "hard_pass_rate", "n_jobs", "baseline_key"}
