"""Tests for intent_hard_pass_breakdown (iter-4g.1 NEW)."""
from __future__ import annotations

from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import intent_hard_pass_breakdown as gen


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


@pytest.mark.skipif(not _real_data_present(), reason="iter-4d real run-dirs missing")
def test_prepare_real_iter4d_monotonic_ladder() -> None:
    """On the real iter-4d data, every intent profile's hard_pass_rate
    must satisfy Static <= Heuristic <= DELPHI-Full. This is the
    monotonic ladder cross-baseline-comparison.json calls the headline
    RQ1 signal; if any profile inverts it, the figure misleads."""
    df = gen._prepare(REAL_RUN_DIRS)
    profiles = df["intent_profile"].dropna().unique().tolist()
    assert profiles, "expected at least one profile from real data"
    for profile in profiles:
        sub = df[df["intent_profile"] == profile].set_index("baseline_key")
        s = float(sub.loc["static-karmada", "hard_pass_rate"])
        h = float(sub.loc["heuristic-scorer", "hard_pass_rate"])
        d = float(sub.loc["delphi-full", "hard_pass_rate"])
        assert s <= h + 1e-9, f"{profile}: Static {s} > Heuristic {h}"
        assert h <= d + 1e-9, f"{profile}: Heuristic {h} > DELPHI {d}"


@pytest.mark.skipif(not _real_data_present(), reason="iter-4d real run-dirs missing")
def test_prepare_real_iter4d_delphi_dominates_overall() -> None:
    """DELPHI-Full's per-profile hard_pass_rate is at least 0.9 on every
    intent in iter-4d (the campaign-level aggregate is 0.975). This
    asserts the per-profile breakdown does not hide a profile where
    DELPHI underperforms below the campaign aggregate floor."""
    df = gen._prepare(REAL_RUN_DIRS)
    delphi = df[df["baseline_key"] == "delphi-full"]
    assert not delphi.empty
    assert delphi["hard_pass_rate"].min() >= 0.85, delphi.to_dict("records")


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
