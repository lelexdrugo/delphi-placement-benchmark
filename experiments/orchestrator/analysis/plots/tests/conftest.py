"""Shared pytest fixtures for plot generators.

Tests pin the ``_prepare`` step (DataFrame schema + values on
synthetic baselines) and confirm ``generate`` writes a non-empty
file. We do NOT compare PDF bytes — matplotlib's PDF output is not
byte-deterministic across versions (font subsetting, timestamps).
That is the D3 strategy chosen at iter-4g blocker time.
"""
from __future__ import annotations

from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def synthetic_run_dirs() -> list[Path]:
    """Three synthetic baseline run-dirs in canonical order."""
    return [
        FIXTURES_DIR / "synthetic-static-karmada",
        FIXTURES_DIR / "synthetic-heuristic-scorer",
        FIXTURES_DIR / "synthetic-delphi-full",
    ]


@pytest.fixture
def synthetic_delphi_only() -> list[Path]:
    """Single-baseline DELPHI-Full synthetic run-dir for tests that
    don't need the cross-baseline shape (e.g. traceability)."""
    return [FIXTURES_DIR / "synthetic-delphi-full"]


@pytest.fixture
def phase_a_run_dir() -> Path:
    """Real Phase-A run-dir, present on `main` after iter-4c.

    Tests that need real data skip themselves when this path is
    absent (e.g. a partial checkout).
    """
    return Path(__file__).resolve().parents[5] / "experiments" / "runs" / "phase-a-20260524T225655Z"
