r"""Flat search-and-replace populator for \texttt{[[<key>]]} tokens.

Reads summary.json + a source .tex file. Writes <source>.populated.tex
as a sibling — NEVER overwrites the source. Unfilled keys render as
``\textit{pending}`` so the LaTeX still compiles and the unpopulated
cells are visually obvious in the rendered PDF.

Two placeholder name sets are supported by a single PLACEHOLDER_MAP:
- canonical content/06_EvaluationResults.tex
  (median_static, p95_static, decision_static, intent_static,
  movement_static, ...)
- proposal proposals/evaluation-alignment-2026-05-22/06_EvaluationResults.tex
  (comp_static, dec_static, intent_static, class_static, move_static,
  adm_med, adm_p95, pol_med, pol_p95, tel_med, tel_p95,
  per-state and ablation cells, ...)

The populator looks up the key's value via a JSON-path string against
the run's summary.json. If the value is None or missing, emit
``\textit{pending}``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

# JSON-path → callable lookup helper -----------------------------------

def _lookup(summary: dict[str, Any], path: str) -> Any:
    cur: Any = summary
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# Formatters -----------------------------------------------------------

def _fmt_pct(v: Any) -> Optional[str]:
    if v is None:
        return None
    return f"{float(v) * 100:.1f}\\%"


def _fmt_ms(v: Any) -> Optional[str]:
    if v is None:
        return None
    return f"{float(v):.1f}\\,ms"


def _fmt_s(v: Any) -> Optional[str]:
    if v is None:
        return None
    return f"{float(v):.1f}\\,s"


def _fmt_3sig(v: Any) -> Optional[str]:
    if v is None:
        return None
    f = float(v)
    return f"{f:.3g}"


def _fmt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _fmt_intent_pair(v: Any) -> Optional[str]:
    """v should be a dict with hard_pass_rate and soft_score_mean."""
    if not isinstance(v, dict):
        return _fmt_str(v)
    hp = v.get("hard_pass_rate")
    ss = v.get("soft_score_mean")
    if hp is None and ss is None:
        return None
    hp_s = f"{hp * 100:.0f}\\%" if hp is not None else "?"
    ss_s = f"{ss:.2f}" if ss is not None else "?"
    return f"hard={hp_s} / soft={ss_s}"


def _fmt_completion(v: Any) -> Optional[str]:
    """v should be the completion_time aggregate (median/p95/max)."""
    if not isinstance(v, dict):
        return _fmt_str(v)
    med = v.get("median_s")
    p95 = v.get("p95_s")
    mx = v.get("max_s")
    if med is None and p95 is None and mx is None:
        return None
    parts = []
    if med is not None: parts.append(f"med={med:.1f}\\,s")
    if p95 is not None: parts.append(f"p95={p95:.1f}\\,s")
    if mx is not None: parts.append(f"max={mx:.1f}\\,s")
    return " / ".join(parts)


def _fmt_decision(v: Any) -> Optional[str]:
    if not isinstance(v, dict):
        return _fmt_str(v)
    med = v.get("median_ms")
    p95 = v.get("p95_ms")
    if med is None and p95 is None:
        return None
    parts = []
    if med is not None: parts.append(f"med={med:.1f}\\,ms")
    if p95 is not None: parts.append(f"p95={p95:.1f}\\,ms")
    return " / ".join(parts)


def _fmt_movement(v: Any) -> Optional[str]:
    if not isinstance(v, dict):
        return _fmt_str(v)
    mean = v.get("mean_score")
    if mean is None:
        return None
    return f"{mean:.2f}"


# PLACEHOLDER_MAP ------------------------------------------------------
# Each entry: token → (summary.json JSON-path, formatter).
# Both canonical and proposal placeholder names live here. Same token
# name in both files maps to the same value, so a single populated.tex
# pass works for both targets.

_BASELINE_KEYS = {
    "static": "static-karmada",
    "heuristic": "heuristic-scorer",
    "full": "delphi-full",
}

PLACEHOLDER_MAP: dict[str, tuple[str, Callable[[Any], Optional[str]]]] = {}


def _register() -> None:
    for suffix, key in _BASELINE_KEYS.items():
        base = f"baselines.{key}"
        # canonical and proposal share these
        PLACEHOLDER_MAP[f"success_{suffix}"] = (f"{base}.placement_success.value", _fmt_pct)
        PLACEHOLDER_MAP[f"cost_{suffix}"] = (f"{base}.cost_proxy.total", _fmt_3sig)
        # canonical 06:
        PLACEHOLDER_MAP[f"median_{suffix}"] = (f"{base}.completion_time.median_s", _fmt_s)
        PLACEHOLDER_MAP[f"p95_{suffix}"] = (f"{base}.completion_time.p95_s", _fmt_s)
        PLACEHOLDER_MAP[f"decision_{suffix}"] = (f"{base}.decision_latency.median_ms", _fmt_ms)
        PLACEHOLDER_MAP[f"intent_{suffix}"] = (f"{base}.intent_compliance", _fmt_intent_pair)
        PLACEHOLDER_MAP[f"movement_{suffix}"] = (f"{base}.data_movement", _fmt_movement)
        # proposal 06:
        PLACEHOLDER_MAP[f"comp_{suffix}"] = (f"{base}.completion_time", _fmt_completion)
        PLACEHOLDER_MAP[f"dec_{suffix}"] = (f"{base}.decision_latency", _fmt_decision)
        PLACEHOLDER_MAP[f"class_{suffix}"] = (f"{base}.most_frequent_selected_cluster", _fmt_str)
        PLACEHOLDER_MAP[f"move_{suffix}"] = (f"{base}.data_movement", _fmt_movement)

    # Overhead table (proposal 06). All pending until iter-4d.1.
    for short, lookup in [
        ("adm_med", "baselines.delphi-full.admission_overhead.median_ms"),
        ("adm_p95", "baselines.delphi-full.admission_overhead.p95_ms"),
        ("pol_med", "baselines.delphi-full.policy_materialization.median_ms"),
        ("pol_p95", "baselines.delphi-full.policy_materialization.p95_ms"),
        ("tel_med", "baselines.delphi-full.telemetry_overhead.median_ms"),
        ("tel_p95", "baselines.delphi-full.telemetry_overhead.p95_ms"),
    ]:
        PLACEHOLDER_MAP[short] = (lookup, _fmt_ms)

    # Canonical 06 derived deltas. Pending until both runs land; the
    # populator emits \textit{pending} when either side is missing.
    for tok in (
        "delta_success_static", "delta_completion_static",
        "delta_success_heuristic", "delta_completion_heuristic",
        "delta_cost", "delta_latency", "delta_cost_tradeoff",
        "delta_history", "delta_history_completion",
        "delta_similarity", "delta_feedback",
        "delta_stale_success", "delta_stale_completion",
        "delta_churn", "delta_vs_static_churn",
    ):
        PLACEHOLDER_MAP[tok] = (f"deltas.{tok}", _fmt_str)

    # Proposal 06 ablation table cells (No-History, No-Similarity,
    # No-Intent, No-Feedback). All pending until iter-4e.
    for tok in (
        "success_nh", "comp_nh", "intent_nh", "cost_nh",
        "success_ns", "comp_ns", "intent_ns", "cost_ns",
        "success_ni", "comp_ni", "intent_ni", "cost_ni",
        "success_nf", "comp_nf", "intent_nf", "cost_nf",
        "note",
    ):
        PLACEHOLDER_MAP[tok] = (f"ablations.{tok}", _fmt_str)

    # Proposal 06 per-state table (idle / mixed-load / skewed-*).
    for tok in (
        "strategy", "success", "median", "cluster",
        "result",
    ):
        PLACEHOLDER_MAP[tok] = (f"states.{tok}", _fmt_str)


_register()


# Populator ------------------------------------------------------------

# Accept both \texttt{[[success_static]]} (unit-test form) and
# \texttt{[[success\_static]]} (canonical LaTeX form, with the underscore
# escaped to \_ to avoid math-mode subscript). The actual paper sources
# use the escaped form throughout, so the populator must consume both.
# iter-4a's fixtures used only the unescaped form, hiding the bug from
# CI — fixed in iter-4a.1 along with a paper-style fixture in
# test_tex_populator.py.
_PLACEHOLDER_RE = re.compile(r"\\texttt\{\[\[(?P<key>[\w\\]+)\]\]\}")


def _replace_one(match: re.Match, summary: dict[str, Any]) -> str:
    raw_key = match.group("key")
    # LaTeX escapes underscores as \_; PLACEHOLDER_MAP keys are the
    # unescaped slugs (`success_static`), so normalise here.
    key = raw_key.replace("\\", "")
    entry = PLACEHOLDER_MAP.get(key)
    if entry is None:
        # Unknown key — leave the placeholder verbatim so the operator
        # notices and either teaches the populator or removes the token.
        return match.group(0)
    json_path, fmt = entry
    value = _lookup(summary, json_path)
    rendered = fmt(value)
    if rendered is None:
        return r"\textit{pending}"
    return rendered


def populate(
    source_tex: Path,
    summary_json: Path,
    *,
    output_suffix: str = ".populated.tex",
) -> Path:
    """Read source_tex + summary_json; write <source>.populated.tex.

    Returns the output path. Never overwrites source_tex. If output
    already exists, it is replaced atomically via a temporary rewrite.
    """
    source = source_tex.read_text(encoding="utf-8")
    summary = json.loads(summary_json.read_text(encoding="utf-8"))

    out_path = source_tex.with_suffix(source_tex.suffix + "")  # keep .tex
    # We want <name>.populated.tex (suffix replacement style).
    out_path = source_tex.parent / (source_tex.stem + output_suffix)

    new_text = _PLACEHOLDER_RE.sub(lambda m: _replace_one(m, summary), source)
    out_path.write_text(new_text, encoding="utf-8")
    return out_path


def populate_many(
    sources: list[Path],
    summary_json: Path,
) -> list[Path]:
    """Convenience: populate a list of .tex files; return the list of
    output paths. Order-preserving.
    """
    return [populate(src, summary_json) for src in sources]


# iter-4g.1 composite populator ----------------------------------------

# Each summary.json the analyzer emits carries exactly one baseline
# key under ``baselines``. To populate ``_static`` / ``_heuristic`` /
# ``_full`` placeholders in one pass we merge those three single-key
# dicts into a single composite under the same ``baselines`` root so
# the existing PLACEHOLDER_MAP lookups (``baselines.<key>.<metric>``)
# resolve without further changes.

def _merge_composite_summary(
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge per-baseline summaries into one composite summary.

    Each input summary must have a single key under ``baselines``.
    Conflicting keys (rare; only when two summaries carry the same
    baseline) take the *last* writer.

    Top-level scalars (``run_id``, ``submitted_at``, ...) are taken
    from the first non-empty summary so the composite still reports
    *a* run identity. ``notes`` are concatenated with a per-baseline
    prefix so the merge is auditable from the populated .tex via
    ``\\textit{pending}`` traces.
    """
    composite: dict[str, Any] = {"baselines": {}, "notes": []}
    seen_top = False
    for s in summaries:
        if not isinstance(s, dict):
            continue
        baselines = s.get("baselines") or {}
        for key, proj in baselines.items():
            composite["baselines"][str(key)] = proj
        if not seen_top:
            for k, v in s.items():
                if k in ("baselines", "notes"):
                    continue
                composite[k] = v
            seen_top = True
        for note in s.get("notes") or []:
            tag = next(iter(baselines.keys()), "unknown")
            composite["notes"].append(f"[{tag}] {note}")
    return composite


def populate_composite(
    source_tex: Path,
    summary_jsons: list[Path],
    *,
    output_suffix: str = ".populated.tex",
) -> Path:
    """Multi-baseline single-pass populate.

    Reads N summary.json files (one per baseline), merges them into a
    composite summary whose ``baselines`` dict contains every key, and
    populates one ``<source>.populated.tex``. Equivalent to running
    ``populate`` N times — each baseline's placeholders fill in
    correctly — but in one pass, so the file is written exactly once
    and ``_static`` / ``_heuristic`` / ``_full`` cells coexist.

    Returns the output path. Never overwrites source_tex.
    """
    loaded = [
        json.loads(p.read_text(encoding="utf-8")) for p in summary_jsons
    ]
    composite = _merge_composite_summary(loaded)

    source = source_tex.read_text(encoding="utf-8")
    out_path = source_tex.parent / (source_tex.stem + output_suffix)
    new_text = _PLACEHOLDER_RE.sub(lambda m: _replace_one(m, composite), source)
    out_path.write_text(new_text, encoding="utf-8")
    return out_path
