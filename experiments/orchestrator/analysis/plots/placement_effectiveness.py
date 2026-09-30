"""Placement effectiveness — RQ1 figure for paper Section 6.

Renders a grouped bar chart: workload class on the x-axis, one bar
per baseline. Y-axis is intent-compliance hard-pass rate (region as
hard gate, per the harness's measurement boundary).
Hard-pass is the operational definition of "placement
effectiveness" once every baseline trivially completes the Job;
placement_success.value is 1.0 across the synthetic fixtures and is
expected to stay close to 1.0 across the real campaign too.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Reduce a list of single-baseline run-dirs to per-(class, baseline) rates.

    Returns a tidy DataFrame so plot rendering is a one-call group:
    columns ``baseline_key`` / ``workload_class`` / ``hard_pass_rate``
    / ``n_jobs``. The function is pure: same input list, same output.
    """
    bundles = _loaders.load_bundles(list(run_dirs))
    frames: list[pd.DataFrame] = []
    for bundle in bundles:
        if bundle.intent_compliance.empty:
            # No intent-compliance metric — skip this baseline rather
            # than crash; the caller will see a missing column at
            # render time and decide what to do.
            continue
        df = bundle.intent_compliance.copy()
        # intent-compliance.csv has the per-job intent profile but not
        # the workload class; pull it from completion-time.csv which
        # carries both. Join on job_name.
        if bundle.completion_time.empty:
            continue
        ct = bundle.completion_time[["job_name", "workload_class"]]
        df = df.merge(ct, on="job_name", how="left")
        grouped = (
            df.groupby("workload_class")
            .agg(
                hard_pass_rate=("hard_pass", "mean"),
                n_jobs=("hard_pass", "size"),
            )
            .reset_index()
        )
        grouped["baseline_key"] = bundle.baseline_key
        frames.append(grouped)

    if not frames:
        return pd.DataFrame(
            columns=["workload_class", "hard_pass_rate", "n_jobs", "baseline_key"]
        )

    out = pd.concat(frames, ignore_index=True)
    out["workload_class"] = pd.Categorical(
        out["workload_class"],
        categories=list(_style.WORKLOAD_CLASS_ORDER),
        ordered=True,
    )
    out["baseline_key"] = pd.Categorical(
        out["baseline_key"],
        categories=[b for b in _style.BASELINE_ORDER if b in set(out["baseline_key"])],
        ordered=True,
    )
    out = out.sort_values(["workload_class", "baseline_key"]).reset_index(drop=True)
    return out


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    """Render the grouped bar chart from a prepared DataFrame."""
    classes = list(_style.WORKLOAD_CLASS_ORDER)
    baselines_present = [
        b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"])
    ]
    n_baselines = len(baselines_present)
    bar_width = 0.8 / max(n_baselines, 1)

    for i, baseline in enumerate(baselines_present):
        sub = df[df["baseline_key"] == baseline].set_index("workload_class")
        # Reindex to the canonical order so missing classes show as 0.
        sub = sub.reindex(classes)
        values = sub["hard_pass_rate"].fillna(0.0).to_numpy()
        x = [j + (i - (n_baselines - 1) / 2) * bar_width for j in range(len(classes))]
        ax.bar(
            x, values,
            width=bar_width,
            color=_style.baseline_color(baseline),
            label=_style.baseline_label(baseline),
            edgecolor="white",
            linewidth=0.4,
        )

    ax.set_xticks(range(len(classes)))
    ax.set_xticklabels([c.capitalize() for c in classes])
    ax.set_ylim(0.0, 1.05)
    ax.set_ylabel("Intent compliance (hard tier)")
    ax.set_xlabel("Workload class")
    ax.set_title("Placement effectiveness per workload class")
    ax.legend(loc="lower right", frameon=True)


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
) -> list[Path]:
    """End-to-end: load, prepare, render, write.

    ``out_path`` is interpreted as a stem — the extension is appended
    by ``_figures.save_figure`` per format requested.
    """
    formats = formats or ["pdf", "png"]
    _style.apply_style()
    df = _prepare(run_dirs)
    fig, ax = plt.subplots(figsize=(6.0, 3.6))
    _render(df, ax)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
