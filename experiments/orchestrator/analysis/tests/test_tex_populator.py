r"""tex_template populator — covers both canonical-06 and proposal-06 keys."""
from __future__ import annotations

import json
from pathlib import Path

from experiments.orchestrator.analysis.tex_template import populate


_CANONICAL_FRAGMENT = r"""
Static-Karmada placement success: \texttt{[[success_static]]}.
Static-Karmada median completion: \texttt{[[median_static]]}.
DELPHI-Full success: \texttt{[[success_full]]}.
Heuristic intent: \texttt{[[intent_heuristic]]}.
"""

_PROPOSAL_FRAGMENT = r"""
Static-Karmada completion: \texttt{[[comp_static]]}.
Static-Karmada decision: \texttt{[[dec_static]]}.
Static-Karmada movement: \texttt{[[move_static]]}.
Most frequent class: \texttt{[[class_static]]}.
Overhead admission median: \texttt{[[adm_med]]}.
Ablation No-History success: \texttt{[[success_nh]]}.
"""


def _build_summary() -> dict:
    return {
        "run_id": "test-run",
        "baseline": "static-karmada",
        "no_pollution_ok": True,
        "join_coverage_ok": True,
        "submitted_at": None,
        "finished_at": None,
        "baselines": {
            "static-karmada": {
                "placement_success": {"value": 0.875, "n_applied": 8, "n_completed": 7},
                "completion_time": {"median_s": 42.1, "p95_s": 88.4, "max_s": 102.7, "n": 7},
                "decision_latency": {"median_ms": 1.2, "p95_ms": 3.0, "max_ms": 6.0, "n": 7},
                "intent_compliance": {"hard_pass_rate": 0.75, "soft_score_mean": 0.50, "n": 6},
                "cost_proxy": {"total": 245.3},
                "data_movement": {"mean_score": 0.8, "n_scored": 5},
                "admission_overhead": {"median_ms": None, "p95_ms": None,
                                       "status": "pending paired-run protocol"},
                "telemetry_overhead": {"median_ms": None, "p95_ms": None},
                "most_frequent_selected_cluster": "edge-1",
            },
            "heuristic-scorer": {},
            "delphi-full": {},
        },
        "notes": [],
    }


def test_canonical_keys_populate(tmp_path: Path):
    src = tmp_path / "06_canonical.tex"
    src.write_text(_CANONICAL_FRAGMENT, encoding="utf-8")
    sj = tmp_path / "summary.json"
    sj.write_text(json.dumps(_build_summary()), encoding="utf-8")

    out = populate(src, sj)
    text = out.read_text(encoding="utf-8")

    # Static cells are filled.
    assert "87.5\\%" in text, "success_static should render as percentage"
    assert "42.1\\,s" in text, "median_static should render with unit"
    # Unfilled cells render as \textit{pending}.
    assert r"\textit{pending}" in text, "success_full empty → pending"
    assert text.count(r"\textit{pending}") >= 2, \
        "success_full and intent_heuristic both empty"


def test_proposal_keys_populate(tmp_path: Path):
    src = tmp_path / "06_proposal.tex"
    src.write_text(_PROPOSAL_FRAGMENT, encoding="utf-8")
    sj = tmp_path / "summary.json"
    sj.write_text(json.dumps(_build_summary()), encoding="utf-8")

    out = populate(src, sj)
    text = out.read_text(encoding="utf-8")

    assert "med=42.1\\,s" in text, "comp_static (completion formatted)"
    assert "med=1.2\\,ms" in text, "dec_static (decision-latency formatted)"
    assert "0.80" in text, "move_static (movement.mean_score)"
    assert "edge-1" in text, "class_static (most_frequent_selected_cluster)"
    # Pending cells.
    assert r"\textit{pending}" in text


def test_source_not_modified(tmp_path: Path):
    """The source .tex must never be overwritten."""
    src = tmp_path / "06_source.tex"
    original = _CANONICAL_FRAGMENT
    src.write_text(original, encoding="utf-8")
    sj = tmp_path / "summary.json"
    sj.write_text(json.dumps(_build_summary()), encoding="utf-8")

    populate(src, sj)
    assert src.read_text(encoding="utf-8") == original


def test_output_path_naming(tmp_path: Path):
    src = tmp_path / "06_foo.tex"
    src.write_text(_CANONICAL_FRAGMENT, encoding="utf-8")
    sj = tmp_path / "summary.json"
    sj.write_text(json.dumps(_build_summary()), encoding="utf-8")

    out = populate(src, sj)
    assert out.name == "06_foo.populated.tex"
    assert out.parent == src.parent


# iter-4a.1 regression: canonical paper sources escape underscores as \_
# inside \texttt{} blocks (raw _ would be parsed as math-subscript). The
# original iter-4a fixtures only exercised the unescaped form, so the
# populator's regex silently failed against the real 06_EvaluationResults.tex.
_ESCAPED_FRAGMENT = r"""
Static-Karmada success: \texttt{[[success\_static]]}.
Static-Karmada median: \texttt{[[median\_static]]}.
"""


def test_escaped_underscore_placeholders_populate(tmp_path: Path):
    """The populator must match the paper-style escaped \\_ form."""
    src = tmp_path / "06_escaped.tex"
    src.write_text(_ESCAPED_FRAGMENT, encoding="utf-8")
    sj = tmp_path / "summary.json"
    sj.write_text(json.dumps(_build_summary()), encoding="utf-8")

    out = populate(src, sj)
    text = out.read_text(encoding="utf-8")

    assert "87.5\\%" in text, "escaped success_static must substitute"
    assert "42.1\\,s" in text, "escaped median_static must substitute"
    # No raw placeholder should survive after substitution.
    assert "[[success" not in text and "[[median" not in text
