"""Per-intent-profile intent_compliance breakdown — paper-critical RQ1 figure.

The aggregate ``intent_compliance.hard_pass_rate`` (0.10 / 0.75 / 0.975
across the iter-4d ladder) hides where each baseline's compliance
budget breaks. This figure surfaces the per-profile structure so
Section 6 can argue concretely about *which* intents Heuristic-Scorer
fails on the latency/cost ordinal and *which* DELPHI-Full recovers.

Reads ``intent-compliance.csv`` from one or more run-dirs (one per
baseline) and emits a grouped bar chart with one group per intent
profile and one bar per baseline inside the group.

Paper claim: monotonic improvement is concentrated on the
region-gated profiles (``latency-sensitive``, ``locality-aware``) and
on the ordinal-budget profile (``cost-aware``); the region=any
``balanced`` profile is dominated by all three baselines and is the
weakest separator.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


# Canonical x-axis order for intent profiles. Keep aligned with the
# `experiments/specs/intent-profiles.yaml` order so the figure reads
# left-to-right "region=any → region-gated → region+budget".
INTENT_PROFILE_ORDER: tuple[str, ...] = (
    "balanced",
    "cost-aware",
    "latency-sensitive",
    "locality-aware",
)


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Group hard_pass by (baseline, intent_profile).

    Returns a tidy DataFrame with columns ``baseline_key`` /
    ``intent_profile`` / ``hard_pass_rate`` / ``n_jobs``. Profiles
    absent from a baseline (rare; only if a spec excluded them) get a
    row with ``hard_pass_rate = NaN`` so the renderer can skip them
    without rebuilding the grid.
    """
    frames: list[pd.DataFrame] = []
    for bundle in _loaders.load_bundles(list(run_dirs)):
        if bundle.intent_compliance.empty:
            continue
        df = bundle.intent_compliance[["intent_profile", "hard_pass"]].copy()
        grouped = (
            df.groupby("intent_profile")
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
            columns=["intent_profile", "hard_pass_rate", "n_jobs", "baseline_key"]
        )

    out = pd.concat(frames, ignore_index=True)
    out["intent_profile"] = pd.Categorical(
        out["intent_profile"],
        categories=[p for p in INTENT_PROFILE_ORDER if p in set(out["intent_profile"])],
        ordered=True,
    )
    out["baseline_key"] = pd.Categorical(
        out["baseline_key"],
        categories=[b for b in _style.BASELINE_ORDER if b in set(out["baseline_key"])],
        ordered=True,
    )
    return out.sort_values(["intent_profile", "baseline_key"]).reset_index(drop=True)


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    if df.empty:
        ax.text(
            0.5, 0.5,
            "No intent-compliance data — pass --run-dirs with intent-compliance.csv files.",
            ha="center", va="center", transform=ax.transAxes,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return

    profiles_present = [
        p for p in INTENT_PROFILE_ORDER if p in set(df["intent_profile"].dropna())
    ]
    baselines_present = [
        b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"].dropna())
    ]
    n_baselines = max(len(baselines_present), 1)
    bar_width = 0.8 / n_baselines

    for i, baseline in enumerate(baselines_present):
        sub = df[df["baseline_key"] == baseline].set_index("intent_profile")
        sub = sub.reindex(profiles_present)
        values = sub["hard_pass_rate"].fillna(0.0).to_numpy()
        x = [j + (i - (n_baselines - 1) / 2) * bar_width for j in range(len(profiles_present))]
        ax.bar(
            x, values,
            width=bar_width,
            color=_style.baseline_color(baseline),
            label=_style.baseline_label(baseline),
            edgecolor="white",
            linewidth=0.4,
        )

    ax.set_xticks(range(len(profiles_present)))
    # Full profile names, rotated so they stay legible without colliding
    # at single-column width.
    ax.set_xticklabels(
        [_pretty_profile(p) for p in profiles_present],
        rotation=20, ha="right", rotation_mode="anchor",
    )
    ax.set_ylim(0.0, 1.16)
    ax.set_ylabel("Intent compliance (hard tier)")
    ax.set_xlabel("Intent profile")
    ax.set_title("Per-intent intent-compliance breakdown")
    # Legend above the axes in one row so it never overlaps the bars,
    # which reach 1.0 across most groups.
    ax.legend(
        loc="lower center", bbox_to_anchor=(0.5, 1.005), ncol=n_baselines,
        frameon=False, fontsize=7, columnspacing=1.0, handletextpad=0.4,
    )


def _pretty_profile(name: str) -> str:
    """Full profile names for the x-axis (rotated in the renderer)."""
    return {
        "balanced": "Balanced",
        "cost-aware": "Cost-aware",
        "latency-sensitive": "Latency-sensitive",
        "locality-aware": "Locality-aware",
    }.get(name, name)


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
    show_title: bool = True,
) -> list[Path]:
    """End-to-end: load, prepare, render, write.

    Set ``synthetic_preview=True`` to add the SYNTHETIC watermark and
    the ``.SYNTHETIC`` filename suffix. iter-4g.1 ships this generator
    against real iter-4d data so the watermark is opt-in.
    """
    formats = formats or ["pdf", "png"]
    _style.apply_style()
    df = _prepare(run_dirs)
    fig, ax = plt.subplots(figsize=(3.6, 2.6), constrained_layout=True)
    _render(df, ax)
    if not show_title:
        _figures.clear_titles(fig)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
