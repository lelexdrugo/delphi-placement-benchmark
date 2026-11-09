"""Tests for latency_vs_compliance_tradeoff."""
from __future__ import annotations

from pathlib import Path

from experiments.orchestrator.analysis.plots import (
    latency_vs_compliance_tradeoff as gen,
)


def test_prepare_shape_synthetic(synthetic_run_dirs: list[Path]) -> None:
    df = gen._prepare(synthetic_run_dirs)
    assert df.shape == (3, 3)
    assert set(df.columns) == {
        "baseline_key", "decision_latency_median_ms", "hard_pass_rate",
    }


def test_prepare_baseline_order(synthetic_run_dirs: list[Path]) -> None:
    df = gen._prepare(synthetic_run_dirs)
    assert df["baseline_key"].tolist() == [
        "static-karmada", "heuristic-scorer", "delphi-full",
    ]


def test_generate_writes_files(synthetic_run_dirs: list[Path], tmp_path: Path) -> None:
    written = gen.generate(
        synthetic_run_dirs,
        tmp_path / "lvc",
        formats=["pdf"],
        synthetic_preview=False,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024


def test_prepare_handles_empty_run_dirs() -> None:
    df = gen._prepare([])
    assert df.empty
