"""Sensitivity to cluster-state staleness and reasoning delay — RQ4 figure.

Two-panel line plot:

- **Left**  staleness of cluster snapshot vs intent-compliance hard-pass rate.
- **Right** injected reasoning delay vs end-to-end decision latency.

This generator is P3 today: iter-4f has not landed the sweep, so the
curves come from a small parametric model baked into the module
rather than from measured data. The watermark + ``.SYNTHETIC``
filename suffix make it unmistakable, and the footer states the
provenance.

When iter-4f delivers per-staleness summary.json files, ``_prepare``
swaps in to read them from the run-dirs and the watermark is
dropped. The function signature stays the same.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import _figures, _loaders, _style


# Parametric curves driving the SYNTHETIC sensitivity plot.
# baseline -> (staleness_decay_per_sec, reasoning_floor_ms,
#              reasoning_slope_per_ms_delay).
_SYNTHETIC_MODEL: dict[str, tuple[float, float, float]] = {
    "static-karmada":   (0.0000,  1.0,    1.0),    # state-blind
    "heuristic-scorer": (0.0005, 15.0,    1.05),   # mild degradation with staleness
    "delphi-full":      (0.0014, 28000.0, 1.15),   # most affected by stale history
}

_STALENESS_S: tuple[float, ...] = (0.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0)
_DELAY_MS:    tuple[float, ...] = (0.0, 50.0, 100.0, 200.0, 500.0, 1000.0)


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Materialise the parametric curves, restricted to baselines present.

    Returns a long-format DataFrame with columns
    ``baseline_key`` / ``panel`` / ``x`` / ``y``.

    ``panel`` ∈ {"staleness", "delay"}. ``x`` is seconds (staleness)
    or milliseconds (delay). ``y`` is hard_pass_rate (staleness) or
    decision_latency_ms (delay).
    """
    bundles = _loaders.load_bundles(list(run_dirs))
    present = [b.baseline_key for b in bundles]
    if not present:
        present = list(_SYNTHETIC_MODEL.keys())

    rows: list[dict[str, object]] = []
    for key in present:
        decay, floor_ms, slope = _SYNTHETIC_MODEL.get(
            key, (0.005, 100.0, 1.05),
        )
        # Staleness panel: hard_pass_rate decays from 1.0.
        for x in _STALENESS_S:
            y = max(0.45, 1.0 - decay * x)
            rows.append({
                "baseline_key": key,
                "panel": "staleness",
                "x": float(x),
                "y": float(y),
            })
        # Delay panel: decision_latency grows with injected delay.
        for x in _DELAY_MS:
            y = floor_ms + slope * x
            rows.append({
                "baseline_key": key,
                "panel": "delay",
                "x": float(x),
                "y": float(y),
            })
    df = pd.DataFrame(rows)
    df["baseline_key"] = pd.Categorical(
        df["baseline_key"],
        categories=[b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"])],
        ordered=True,
    )
    return df


def _render(df: pd.DataFrame, axes: list[plt.Axes]) -> None:  # type: ignore[name-defined]
    ax_stale, ax_delay = axes
    baselines_present = [
        b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"].dropna())
    ]
    for baseline in baselines_present:
        sub = df[df["baseline_key"] == baseline]
        stale = sub[sub["panel"] == "staleness"]
        delay = sub[sub["panel"] == "delay"]
        ax_stale.plot(
            stale["x"], stale["y"],
            marker="o", markersize=4, linewidth=1.4,
            color=_style.baseline_color(baseline),
            label=_style.baseline_label(baseline),
        )
        ax_delay.plot(
            delay["x"], delay["y"],
            marker="s", markersize=4, linewidth=1.4,
            color=_style.baseline_color(baseline),
            label=_style.baseline_label(baseline),
        )

    ax_stale.set_xlabel("Cluster snapshot staleness (s)")
    ax_stale.set_ylabel("Intent compliance (hard tier)")
    ax_stale.set_title("Effect of state staleness")
    ax_stale.set_ylim(0.5, 1.05)
    ax_stale.legend(loc="lower left", frameon=True)

    ax_delay.set_xlabel("Injected reasoning delay (ms)")
    ax_delay.set_ylabel("End-to-end decision latency (ms)")
    ax_delay.set_title("Effect of reasoning delay")
    ax_delay.set_yscale("symlog", linthresh=100)


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
    fig, axes_arr = plt.subplots(1, 2, figsize=(9.0, 3.8))
    axes_list = list(np.atleast_1d(axes_arr).ravel())
    _render(df, axes_list)
    fig.suptitle("Sensitivity to controlled axes", y=1.02, fontsize=12)
    fig.tight_layout()
    if synthetic_preview:
        _figures.add_synthetic_watermark(
            fig,
            footer="SYNTHETIC — curves are parametric, not measured. Real data lands in iter-4f.",
        )
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
