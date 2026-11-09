"""Shared pytest fixtures for plot generators.

Tests pin the ``_prepare`` step (DataFrame schema + values on
synthetic baselines) and confirm ``generate`` writes a non-empty
file. We do NOT compare PDF bytes — matplotlib's PDF output is not
byte-deterministic across versions (font subsetting, timestamps).
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
