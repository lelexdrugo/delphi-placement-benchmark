"""Tests for traceability heatmap."""
from __future__ import annotations

from pathlib import Path

import pytest

from experiments.orchestrator.analysis.plots import traceability


def test_prepare_one_row_per_profile(synthetic_delphi_only: list[Path]) -> None:
    df = traceability._prepare(synthetic_delphi_only)
    assert df.shape == (4, 7)
    profiles = df["intent_profile"].tolist()
    assert profiles == ["balanced", "cost-aware", "latency-sensitive", "locality-aware"]


def test_prepare_score_columns(synthetic_delphi_only: list[Path]) -> None:
    df = traceability._prepare(synthetic_delphi_only)
    cluster_cols = [c for c in df.columns if c.startswith("score_")]
    assert set(cluster_cols) == {
        "score_on-prem", "score_public-cloud", "score_edge-1", "score_edge-2",
    }
    # Every row's selected_cluster column must carry the max score
    # (or tied for max) — sanity check on the synthetic generator.
    for _, row in df.iterrows():
        sel = row["selected_cluster"]
        sel_score = row[f"score_{sel}"]
        other_scores = [
            row[f"score_{c}"] for c in ("on-prem", "public-cloud", "edge-1", "edge-2")
            if c != sel
        ]
        assert sel_score >= max(other_scores), f"{sel} did not have the max score in row {row['job_name']}"


def test_parse_score_blob_round_trip() -> None:
    blob = "edge-1=1.00;edge-2=1.00;on-prem=0.00;public-cloud=0.00"
    parsed = traceability._parse_score_blob(blob)
    assert parsed == {
        "edge-1": 1.0, "edge-2": 1.0, "on-prem": 0.0, "public-cloud": 0.0,
    }


def test_parse_score_blob_handles_garbage() -> None:
    assert traceability._parse_score_blob("") == {}
    assert traceability._parse_score_blob("not a score") == {}
    # Mixed valid + garbage entries.
    assert traceability._parse_score_blob("edge-1=0.9;bogus;edge-2=foo;on-prem=0.5") == {
        "edge-1": 0.9, "on-prem": 0.5,
    }


def test_generate_writes_files(synthetic_delphi_only: list[Path], tmp_path: Path) -> None:
    written = traceability.generate(
        synthetic_delphi_only,
        tmp_path / "tr",
        formats=["pdf"],
        synthetic_preview=True,
    )
    assert len(written) == 1
    assert written[0].stat().st_size > 1024
    assert ".SYNTHETIC." in written[0].name


def test_generate_against_phase_a(phase_a_run_dir: Path, tmp_path: Path) -> None:
    """Real-data smoke: Phase-A has 144 traceability rows; the
    generator must pick one representative per intent profile and
    write a non-empty file."""
    if not phase_a_run_dir.exists():
        pytest.skip("Phase-A run-dir not present in this checkout")
    written = traceability.generate(
        [phase_a_run_dir],
        tmp_path / "tr-phase-a",
        formats=["pdf"],
        synthetic_preview=False,
    )
    assert written[0].stat().st_size > 1024
