"""Cordon avoidance — iter-4h-4 figure for paper Section 6 (RQ4).

For each baseline, three grouped bars (pre / during / post cordon) whose
height is the fraction of decisions in that window that targeted the
cordoned cluster. The story the figure tells:

- Static-Karmada: flat across phases (insensitive to cordon — fixed target);
- Heuristic-Scorer: stays high during cordon (registry-blind);
- DELPHI-Full: drops to ~0 during cordon (live-state filter), recovering
  afterwards.

Reads each run-dir's ``summary.json`` ``cordon_response`` aggregate
(written by the gated cordon_response metric), so a run without a cordon
window has no aggregate and is skipped.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style

# Phase order + a fixed palette (independent of the baseline palette so
# the three within-baseline bars are easy to tell apart). Colours are
# Okabe-Ito colour-blind-safe (_style.OKABE_ITO): neutral grey for the
# pre reference, vermillion for the load-bearing "during" bar (high
# contrast + reads as alert, distinct from blue under every CVD type),
# blue for post. Vermillion replaces the old pure red (#d62728), which
# was hard to separate from grey under protanopia/deuteranopia.
_PHASES: tuple[tuple[str, str, str], ...] = (
    ("pre_cordon", "pre", "#999999"),            # neutral grey
    ("during_cordon", "during", _style.OKABE_ITO[4]),  # vermillion — load-bearing
    ("post_cordon", "post", _style.OKABE_ITO[0]),      # blue
)
_PHASE_KEYS: tuple[str, ...] = tuple(p[0] for p in _PHASES)


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Reduce single-baseline run-dirs to per-(baseline, phase) fractions.

    Columns: ``baseline_key`` / ``phase`` / ``fraction`` / ``count``.
    Pure: same inputs → same output. Run-dirs without a cordon_response
    aggregate are skipped.
    """
    rows: list[dict[str, object]] = []
    for run_dir in run_dirs:
        bundle = _loaders.load_bundle(run_dir)
        agg = _loaders.summary_metric(bundle.summary, "cordon_response")
        if not agg:
            continue
        for phase_key in _PHASE_KEYS:
            # The DURING bar uses the strict, placement-faithful fraction —
            # decisions both SUBMITTED and DECIDED inside the cordon, i.e.
            # unambiguously made while the cluster was cordoned (excludes the
            # boundary artifacts: pre-submitted jobs whose PP lands after
            # cordon-onset, and during-submitted jobs decided after uncordon).
            # PRE/POST stay submission-time as the un-cordoned reference rate.
            # (Older summaries without the strict field fall back to
            # submission-time for during too.)
            if phase_key == "during_cordon" and \
                    "during_cordon_strict_count" in agg:
                frac = agg.get("during_cordon_strict_fraction_targeting_cordoned")
                count_key = "during_cordon_strict_count"
            else:
                frac = agg.get(f"{phase_key}_fraction_targeting_cordoned")
                count_key = f"{phase_key}_count"
            rows.append({
                "baseline_key": bundle.baseline_key,
                "phase": phase_key,
                "fraction": 0.0 if frac is None else float(frac),
                "count": int(agg.get(count_key, 0) or 0),
            })
    if not rows:
        return pd.DataFrame(columns=["baseline_key", "phase", "fraction", "count"])
    return pd.DataFrame(rows)


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    baselines_present = [
        b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"])
    ]
    n_phases = len(_PHASES)
    bar_width = 0.8 / n_phases
    lookup = {
        (str(b), str(p)): float(f)
        for b, p, f in zip(df["baseline_key"], df["phase"], df["fraction"])
    }

    for i, (phase_key, phase_label, color) in enumerate(_PHASES):
        heights = [lookup.get((baseline, phase_key), 0.0) for baseline in baselines_present]
        x = [j + (i - (n_phases - 1) / 2) * bar_width for j in range(len(baselines_present))]
        ax.bar(
            x, heights,
            width=bar_width,
            color=color,
            label=phase_label,
            edgecolor="white",
            linewidth=0.4,
        )
        # Value labels above every bar. Making the number explicit is
        # what carries the figure's headline: a DELPHI during-cordon bar
        # of 0.0 is invisible otherwise and reads as "missing data"
        # rather than "avoided the cordoned cluster".
        for xi, h in zip(x, heights):
            ax.text(xi, h + 0.02, f"{h:.2f}", ha="center", va="bottom",
                    fontsize=6.5, rotation=90)

    ax.set_xticks(range(len(baselines_present)))
    ax.set_xticklabels([_style.baseline_label(b) for b in baselines_present])
    ax.set_ylim(0.0, 1.15)  # headroom for the rotated value labels
    ax.set_ylabel("Fraction targeting cordoned cluster")
    ax.set_xlabel("Baseline")
    ax.set_title("Cordon avoidance: fraction of decisions targeting the cordoned cluster\n"
                 "(during = decisions made while cordoned; pre/post = submission-time reference)")
    ax.legend(title="Window", loc="upper right", frameon=True)


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
    show_title: bool = True,
) -> list[Path]:
    """End-to-end: load, prepare, render, write PDF/PNG (both by default)."""
    formats = formats or ["pdf", "png"]
    _style.apply_style()
    df = _prepare(run_dirs)
    fig, ax = plt.subplots(figsize=(3.6, 3.0), constrained_layout=True)
    _render(df, ax)
    if not show_title:
        _figures.clear_titles(fig)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
