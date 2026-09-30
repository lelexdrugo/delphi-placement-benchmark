"""Tests for latency_vs_compliance_tradeoff (iter-4g.1 NEW)."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import (
    latency_vs_compliance_tradeoff as gen,
)


REAL_DATA_ROOT = (
    Path(__file__).resolve().parents[5] / "experiments" / "runs"
)
REAL_RUN_DIRS = [
    REAL_DATA_ROOT / "effectiveness-static-karmada-20260525T040458Z",
    REAL_DATA_ROOT / "effectiveness-heuristic-20260525T042209Z",
    REAL_DATA_ROOT / "effectiveness-delphi-full-20260525T044028Z",
]


def _real_data_present() -> bool:
    return all((p / "analysis" / "summary.json").exists() for p in REAL_RUN_DIRS)


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


@pytest.mark.skipif(not _real_data_present(), reason="iter-4d real run-dirs missing")
def test_prepare_real_iter4d_orders_of_magnitude() -> None:
    """On real iter-4d data, DELPHI's decision_latency is at least
    1000× Static's. This is the qualitative claim the figure
    visualises; if the gap collapses, the figure mis-renders the
    systems argument."""
    df = gen._prepare(REAL_RUN_DIRS)
    static = float(df[df["baseline_key"] == "static-karmada"]
                   ["decision_latency_median_ms"].iloc[0])
    delphi = float(df[df["baseline_key"] == "delphi-full"]
                   ["decision_latency_median_ms"].iloc[0])
    assert math.log10(delphi / static) >= 3.0, (
        f"expected ≥3 OoM, got static={static} delphi={delphi}"
    )


@pytest.mark.skipif(not _real_data_present(), reason="iter-4d real run-dirs missing")
def test_prepare_real_iter4d_compliance_ladder() -> None:
    df = gen._prepare(REAL_RUN_DIRS)
    rates = {
        row["baseline_key"]: float(row["hard_pass_rate"])
        for _, row in df.iterrows()
    }
    assert rates["static-karmada"] < rates["heuristic-scorer"]
    assert rates["heuristic-scorer"] < rates["delphi-full"]


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
