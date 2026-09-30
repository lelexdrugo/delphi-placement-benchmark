"""Shared-style palette tests (T2 colour-blind-safe pass).

Pins the Okabe-Ito categorical palette + helper added so every Section
6 figure can route non-baseline encodings through one colour-blind-safe
source of truth.
"""
from __future__ import annotations

import re

from experiments.orchestrator.analysis.plots import _style

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_okabe_ito_is_canonical_eight() -> None:
    # The canonical Okabe & Ito (2008) palette has eight entries, every
    # one a 6-digit hex; all distinct.
    assert len(_style.OKABE_ITO) == 8
    assert all(_HEX.match(c) for c in _style.OKABE_ITO)
    assert len(set(_style.OKABE_ITO)) == 8


def test_okabe_ito_helper_returns_prefix() -> None:
    assert _style.okabe_ito(0) == []
    assert _style.okabe_ito(3) == list(_style.OKABE_ITO[:3])
    assert _style.okabe_ito(8) == list(_style.OKABE_ITO)


def test_okabe_ito_helper_cycles_past_eight() -> None:
    # Cycling keeps callers crash-free if a future figure needs >8.
    out = _style.okabe_ito(10)
    assert len(out) == 10
    assert out[8] == _style.OKABE_ITO[0]
    assert out[9] == _style.OKABE_ITO[1]


def test_baseline_palette_unchanged() -> None:
    # The baseline palette is deliberately NOT migrated (already
    # colour-blind-OK; migrating would churn landed figures). Guard
    # against an accidental edit.
    assert _style.BASELINE_COLORS["static-karmada"] == "#1f77b4"
    assert _style.BASELINE_COLORS["heuristic-scorer"] == "#2ca02c"
    assert _style.BASELINE_COLORS["delphi-full"] == "#9467bd"
