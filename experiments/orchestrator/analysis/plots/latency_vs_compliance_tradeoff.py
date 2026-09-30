"""Decision-latency vs intent-compliance tradeoff — paper-critical RQ1/RQ2 figure.

Single-panel scatter with one labelled point per baseline:

- x = ``decision_latency.median_ms`` (log scale; spans ~4 orders of
  magnitude across the iter-4d ladder),
- y = ``intent_compliance.hard_pass_rate``.

The figure visualises the systems-level tradeoff that Section 6 must
make explicit: closing the intent-compliance gap (0.10 → 0.75 →
0.975) by adding signal sources (region gate → evidence-grounded
synthesis) costs ~4 orders of magnitude in decision-loop latency. The
DELPHI claim is *not* that this cost is small — it is that the
asynchronous controller / Karmada-policy contract tolerates that
cost without blocking workload admission or reconciliation.

A dashed connector from Heuristic-Scorer to DELPHI-Full highlights
the relative move ("the second 0.225 of compliance costs 4 OoM more
than the first 0.65"), which is the qualitative argument the paper
needs to make.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Pull the two summary.json scalars per baseline.

    Returns a DataFrame with columns ``baseline_key`` /
    ``decision_latency_median_ms`` / ``hard_pass_rate``. Missing
    values are returned as NaN so the renderer can skip the point
    rather than crash.
    """
    rows: list[dict[str, object]] = []
    for bundle in _loaders.load_bundles(list(run_dirs)):
        baselines = bundle.summary.get("baselines") or {}
        proj = baselines.get(bundle.baseline_key, {})
        dl = proj.get("decision_latency") or {}
        ic = proj.get("intent_compliance") or {}
        rows.append({
            "baseline_key": bundle.baseline_key,
            "decision_latency_median_ms": _as_float(dl.get("median_ms")),
            "hard_pass_rate": _as_float(ic.get("hard_pass_rate")),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["baseline_key"] = pd.Categorical(
        df["baseline_key"],
        categories=[b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"])],
        ordered=True,
    )
    return df.sort_values("baseline_key").reset_index(drop=True)


def _as_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    if df.empty:
        ax.text(
            0.5, 0.5,
            "No summary.json data — pass --run-dirs with one summary.json each.",
            ha="center", va="center", transform=ax.transAxes,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return

    # Plot one point per baseline. Use a slightly larger marker so the
    # baseline colour is legible at paper-print sizes.
    for _, row in df.iterrows():
        x = row["decision_latency_median_ms"]
        y = row["hard_pass_rate"]
        if x is None or y is None or pd.isna(x) or pd.isna(y):
            continue
        baseline = str(row["baseline_key"])
        ax.scatter(
            [x], [y],
            s=140,
            color=_style.baseline_color(baseline),
            edgecolor="black",
            linewidths=0.7,
            zorder=5,
            label=_style.baseline_label(baseline),
        )

    # Optional dashed connector Heuristic → DELPHI to emphasise the
    # "second 0.225 costs 4 OoM more" move.
    heur = df[df["baseline_key"] == "heuristic-scorer"]
    delphi = df[df["baseline_key"] == "delphi-full"]
    if not heur.empty and not delphi.empty:
        h = heur.iloc[0]
        d = delphi.iloc[0]
        if all(pd.notna(v) for v in (h["decision_latency_median_ms"], h["hard_pass_rate"], d["decision_latency_median_ms"], d["hard_pass_rate"])):
            ax.plot(
                [h["decision_latency_median_ms"], d["decision_latency_median_ms"]],
                [h["hard_pass_rate"], d["hard_pass_rate"]],
                linestyle="--",
                color="#666666",
                linewidth=0.8,
                zorder=1,
            )

    ax.set_xscale("log")
    ax.set_xlabel("Decision latency, median (ms; log scale)")
    ax.set_ylabel("Intent compliance (hard tier)")
    ax.set_ylim(-0.05, 1.1)
    ax.set_title("Decision-latency vs intent-compliance tradeoff")
    ax.grid(True, which="both", axis="x", alpha=0.25, linestyle=":")
    ax.legend(loc="lower right", frameon=True)


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
    show_title: bool = True,
) -> list[Path]:
    formats = formats or ["pdf", "png"]
    _style.apply_style()
    df = _prepare(run_dirs)
    fig, ax = plt.subplots(figsize=(3.5, 2.8), constrained_layout=True)
    _render(df, ax)
    if not show_title:
        _figures.clear_titles(fig)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
