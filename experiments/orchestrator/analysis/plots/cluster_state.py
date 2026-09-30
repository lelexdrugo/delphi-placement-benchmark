"""Cluster-state completion-time distribution — RQ1/RQ4 figure.

Boxplot grouped by (cluster state, baseline). One panel showing four
states left-to-right (idle, mixed-load, skewed-edge-1-saturated,
skewed-on-prem-saturated) with three boxes per state (one per
baseline). Reveals whether a baseline's completion-time distribution
degrades under controlled cluster-state variation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Patch

from . import _figures, _loaders, _style


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Concatenate per-baseline completion-time CSVs.

    Returns a long-format DataFrame with columns
    ``baseline_key`` / ``state_id`` / ``duration_seconds``.

    iter-4g.1 fallback: the bootstrap layout (Phase-A) writes a
    per-row ``state_id`` because each run spans multiple cluster
    states. The simple layout used by single-state campaigns
    (iter-4d, ``cluster_state: idle``) leaves ``state_id`` empty
    because the field has no per-row variation. When a bundle's
    ``state_id`` column is entirely NaN/empty, fall back to the
    canonical "idle" label so the boxplot still renders. Multi-state
    campaigns (iter-4d.2+) will populate ``state_id`` explicitly and
    this fallback becomes a no-op.
    """
    frames: list[pd.DataFrame] = []
    for bundle in _loaders.load_bundles(list(run_dirs)):
        if bundle.completion_time.empty:
            continue
        ct = bundle.completion_time[["state_id", "duration_seconds"]].copy()
        # Treat NaN and blank string the same: pandas reads an empty
        # CSV cell as NaN under default dtype inference but as "" when
        # forced to str. Replace both with the canonical "idle" label.
        ct["state_id"] = (
            ct["state_id"].astype("object")
            .where(ct["state_id"].notna() & (ct["state_id"].astype(str) != ""), "idle")
        )
        ct["baseline_key"] = bundle.baseline_key
        frames.append(ct)
    if not frames:
        return pd.DataFrame(columns=["state_id", "duration_seconds", "baseline_key"])
    out = pd.concat(frames, ignore_index=True)
    out["state_id"] = pd.Categorical(
        out["state_id"],
        categories=list(_style.CLUSTER_STATE_ORDER),
        ordered=True,
    )
    out["baseline_key"] = pd.Categorical(
        out["baseline_key"],
        categories=[b for b in _style.BASELINE_ORDER if b in set(out["baseline_key"])],
        ordered=True,
    )
    return out.sort_values(["state_id", "baseline_key"]).reset_index(drop=True)


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    states_present = [
        s for s in _style.CLUSTER_STATE_ORDER if s in set(df["state_id"].dropna())
    ]
    baselines_present = [
        b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"].dropna())
    ]
    n_baselines = max(len(baselines_present), 1)
    width = 0.8 / n_baselines

    for i, baseline in enumerate(baselines_present):
        data = []
        for state in states_present:
            sub = df[(df["state_id"] == state) & (df["baseline_key"] == baseline)]
            data.append(sub["duration_seconds"].to_numpy())
        positions = [
            j + (i - (n_baselines - 1) / 2) * width
            for j in range(len(states_present))
        ]
        bp = ax.boxplot(
            data,
            positions=positions,
            widths=width * 0.85,
            patch_artist=True,
            manage_ticks=False,
            showfliers=False,
        )
        color = _style.baseline_color(baseline)
        for patch in bp["boxes"]:
            patch.set_facecolor(color)
            patch.set_alpha(0.55)
            patch.set_edgecolor(color)
        for median in bp["medians"]:
            median.set_color("black")
            median.set_linewidth(1.2)
        for element in ("whiskers", "caps"):
            for line in bp[element]:
                line.set_color(color)
    # Legend via explicit Patch artists so each swatch carries its
    # baseline's colour cleanly (dummy ax.bar([], []) sometimes
    # resolves the wrong handle colour at legend time).
    legend_handles = [
        Patch(
            facecolor=_style.baseline_color(b),
            alpha=0.55,
            edgecolor=_style.baseline_color(b),
            label=_style.baseline_label(b),
        )
        for b in baselines_present
    ]
    ax.legend(handles=legend_handles, loc="upper left", frameon=True)

    ax.set_xticks(range(len(states_present)))
    ax.set_xticklabels(
        [_pretty_state(s) for s in states_present],
        rotation=12, ha="right",
    )
    ax.set_ylabel("Completion time (s)")
    ax.set_xlabel("Cluster state")
    ax.set_title("Completion time per cluster state and baseline")


def _pretty_state(state: str) -> str:
    """Map raw state id to a short label for the x-axis."""
    return {
        "idle": "Idle",
        "mixed-load": "Mixed load",
        "skewed-edge-1-saturated": "Skewed (edge-1)",
        "skewed-on-prem-saturated": "Skewed (on-prem)",
    }.get(state, state)


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
) -> list[Path]:
    formats = formats or ["pdf", "png"]
    _style.apply_style()
    df = _prepare(run_dirs)
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    _render(df, ax)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
