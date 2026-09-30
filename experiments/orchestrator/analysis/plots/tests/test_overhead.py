"""Tests for overhead."""
from __future__ import annotations

from pathlib import Path

from experiments.orchestrator.analysis.plots import overhead


def test_prepare_shape(synthetic_run_dirs: list[Path]) -> None:
    df = overhead._prepare(synthetic_run_dirs)
    assert df.shape == (3, 4)
    assert set(df.columns) == {"baseline_key", "admission_ms", "decision_ms", "telemetry_ms"}


def test_decision_latency_dominates_for_delphi(synthetic_run_dirs: list[Path]) -> None:
    """DELPHI-Full's decision_ms must be orders of magnitude larger
    than its admission_ms (LLM round-trip vs admission webhook).
    This is the qualitative story the figure tells."""
    df = overhead._prepare(synthetic_run_dirs)
    delphi = df[df["baseline_key"] == "delphi-full"].iloc[0]
    assert delphi["decision_ms"] > 100 * delphi["admission_ms"]


def test_pending_status_yields_nan(tmp_path: Path) -> None:
    """A baseline whose admission_overhead has status=pending must
    appear as NaN in admission_ms (the renderer drops it visually)."""
    import json
    # Hand-craft a minimal run-dir with pending status.
    run_dir = tmp_path / "pending-baseline"
    adir = run_dir / "analysis"
    adir.mkdir(parents=True)
    (adir / "summary.json").write_text(json.dumps({
        "baseline": "test-baseline",
        "baselines": {
            "test-baseline": {
                "admission_overhead": {"status": "pending"},
                "decision_latency": {"status": "ok", "median_ms": 100.0},
                "telemetry_overhead": {"status": "pending"},
            }
        }
    }))
    df = overhead._prepare([run_dir])
    row = df.iloc[0]
    assert pd_isna(row["admission_ms"])
    assert pd_isna(row["telemetry_ms"])
    assert row["decision_ms"] == 100.0


def pd_isna(v: object) -> bool:
    import pandas as pd
    return bool(pd.isna(v))


def test_generate_writes_files(synthetic_run_dirs: list[Path], tmp_path: Path) -> None:
    written = overhead.generate(
        synthetic_run_dirs,
        tmp_path / "oh",
        formats=["pdf"],
        synthetic_preview=True,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name
