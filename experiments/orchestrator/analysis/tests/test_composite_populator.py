"""Tests for tex_template.populate_composite (iter-4g.1)."""
from __future__ import annotations

import json
from pathlib import Path

from experiments.orchestrator.analysis import tex_template as tex


def _write_summary(path: Path, baseline_key: str, *, median_s: float,
                   decision_ms: float, hard_pass: float) -> None:
    summary = {
        "baseline": baseline_key,
        "baselines": {
            baseline_key: {
                "placement_success": {"value": 1.0, "n_applied": 40,
                                       "n_completed": 40, "status": "ok"},
                "completion_time": {"median_s": median_s, "p95_s": median_s + 5,
                                     "max_s": median_s + 8, "n": 40, "status": "ok"},
                "decision_latency": {"median_ms": decision_ms,
                                      "p95_ms": decision_ms * 1.2,
                                      "max_ms": decision_ms * 1.3,
                                      "n": 40, "status": "ok"},
                "intent_compliance": {"hard_pass_rate": hard_pass,
                                       "soft_score_mean": 0.5,
                                       "n": 40, "status": "ok"},
                "data_movement": {"mean_score": 0.5, "n_scored": 20,
                                   "n": 20, "status": "ok"},
                "cost_proxy": {"total": 1000.0, "n": 40, "status": "ok"},
                "most_frequent_selected_cluster": "edge-1",
            },
        },
        "no_pollution_ok": True,
        "join_coverage_ok": True,
        "run_id": f"test-{baseline_key}",
        "notes": [f"synthetic summary for {baseline_key}"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _write_tex(path: Path) -> None:
    """Three-baseline placeholder table with both _static / _heuristic / _full."""
    path.write_text(
        r"""\begin{tabular}{lll}
Static-Karmada & \texttt{[[median\_static]]} & \texttt{[[intent\_static]]} \\
Heuristic-Scorer & \texttt{[[median\_heuristic]]} & \texttt{[[intent\_heuristic]]} \\
DELPHI-Full & \texttt{[[median\_full]]} & \texttt{[[intent\_full]]} \\
\end{tabular}
""",
        encoding="utf-8",
    )


def test_populate_composite_fills_all_three_baselines(tmp_path: Path) -> None:
    """Single pass of populate_composite must fill _static / _heuristic
    / _full from three separate summary.json inputs in one
    populated.tex."""
    s_static = tmp_path / "static.json"
    s_heur = tmp_path / "heuristic.json"
    s_delphi = tmp_path / "delphi.json"
    _write_summary(s_static, "static-karmada", median_s=82.0,
                   decision_ms=1.7, hard_pass=0.10)
    _write_summary(s_heur, "heuristic-scorer", median_s=71.7,
                   decision_ms=2.2, hard_pass=0.75)
    _write_summary(s_delphi, "delphi-full", median_s=100.9,
                   decision_ms=30839.7, hard_pass=0.975)

    tex_src = tmp_path / "table.tex"
    _write_tex(tex_src)
    out_path = tex.populate_composite(tex_src, [s_static, s_heur, s_delphi])

    populated = out_path.read_text(encoding="utf-8")
    # Each baseline's cells filled with its own numbers.
    assert "82.0" in populated and "71.7" in populated and "100.9" in populated
    assert "hard=10\\%" in populated
    assert "hard=75\\%" in populated
    assert "hard=98\\%" in populated  # 0.975 -> 98%
    # No pending placeholders for the three baselines we provided.
    assert "median\\_static]]" not in populated
    assert "median\\_heuristic]]" not in populated
    assert "median\\_full]]" not in populated
    # Source untouched.
    assert "[[median\\_static]]" in tex_src.read_text(encoding="utf-8")


def test_populate_composite_leaves_pending_for_missing_baselines(
    tmp_path: Path,
) -> None:
    """If only one baseline summary is passed, the other two columns'
    cells must render as \textit{pending}."""
    s_delphi = tmp_path / "delphi.json"
    _write_summary(s_delphi, "delphi-full", median_s=100.9,
                   decision_ms=30839.7, hard_pass=0.975)
    tex_src = tmp_path / "table.tex"
    _write_tex(tex_src)
    out_path = tex.populate_composite(tex_src, [s_delphi])
    populated = out_path.read_text(encoding="utf-8")
    assert populated.count(r"\textit{pending}") >= 4  # 2 cells × 2 missing baselines
    assert "100.9" in populated


def test_merge_composite_summary_preserves_notes(tmp_path: Path) -> None:
    """notes from each baseline must be preserved + tagged with the
    baseline key so a populated.tex audit trail is recoverable."""
    s1 = {"baselines": {"a": {}}, "notes": ["note-from-a"]}
    s2 = {"baselines": {"b": {}}, "notes": ["note-from-b", "another-b"]}
    merged = tex._merge_composite_summary([s1, s2])
    assert merged["notes"] == [
        "[a] note-from-a", "[b] note-from-b", "[b] another-b",
    ]
    assert set(merged["baselines"].keys()) == {"a", "b"}
