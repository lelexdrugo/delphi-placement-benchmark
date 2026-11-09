"""Tests for intent_hard_pass_breakdown."""
from __future__ import annotations

from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import intent_hard_pass_breakdown as gen


def test_prepare_shape_synthetic(synthetic_run_dirs: list[Path]) -> None:
    df = gen._prepare(synthetic_run_dirs)
    # 3 baselines × 4 intent profiles = 12 rows.
    assert df.shape == (12, 4)
    assert set(df.columns) == {"intent_profile", "hard_pass_rate", "n_jobs", "baseline_key"}


def test_prepare_profile_ordering(synthetic_run_dirs: list[Path]) -> None:
    """Rows must be ordered (intent_profile, baseline_key) so the
    renderer can iterate without re-sorting."""
    df = gen._prepare(synthetic_run_dirs)
    # First three rows: balanced × (static, heuristic, delphi-full).
    first_profiles = df["intent_profile"].head(3).tolist()
    assert first_profiles == ["balanced"] * 3
    first_baselines = df["baseline_key"].head(3).tolist()
    assert first_baselines == ["static-karmada", "heuristic-scorer", "delphi-full"]


def test_synthetic_static_fails_latency_sensitive(
    synthetic_run_dirs: list[Path],
) -> None:
    """The synthetic fixture has Static pin → public-cloud failing the
    region=edge gate for latency-sensitive intent. The aggregated
    per-profile hard_pass_rate must be 0.0 for that intent on Static."""
    df = gen._prepare(synthetic_run_dirs)
    static_ls = df[
        (df["baseline_key"] == "static-karmada")
        & (df["intent_profile"] == "latency-sensitive")
    ]
    assert float(static_ls["hard_pass_rate"].iloc[0]) == pytest.approx(0.0)


def test_generate_writes_files(synthetic_run_dirs: list[Path], tmp_path: Path) -> None:
    written = gen.generate(
        synthetic_run_dirs,
        tmp_path / "ihpb",
        formats=["pdf"],
        synthetic_preview=False,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024


def test_prepare_handles_empty_run_dirs() -> None:
    df = gen._prepare([])
    assert df.empty
    assert set(df.columns) == {"intent_profile", "hard_pass_rate", "n_jobs", "baseline_key"}
