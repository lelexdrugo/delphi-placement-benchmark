"""Completion-time decomposition — wait vs execution (iter-4d.0b figure).

Stacked bar per baseline of the two components of `completion_time`,
read from each run-dir's ``summary.json`` ``completion_decomposition``
block:

- ``median_wait_s``  (``time_to_pod_spawn_s``) — the asynchronous wait
  the controller absorbs (decision loop + PP materialization + pod
  schedule). For DELPHI-Full this is dominated by the agent loop.
- ``median_exec_s``  (``execution_runtime_s``) — the time the workload
  actually ran on the member; the placement-quality signal under
  task-bound stressors.

The two stack to the completion-time median by construction. This is the
honest representation of the exec-vs-decision split: it shows that
DELPHI-Full's *tall* completion bar is mostly decision wait, while its
*execution* portion is competitive — so a reader of the cluster-state
completion figure does not read the decision cost twice (the
methodological note motivated in iter-4g.2 / the iter-4d.0b plan).

A baseline whose ``completion_decomposition`` block is absent or
``status='pending'`` (e.g. a run analyzed before this metric landed, or
with kube enrichment off so no ``Job.status.startTime``) renders as an
empty bar — the figure never invents numbers. When *every* run-dir is
pending, the renderer prints a guidance message instead of bars.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


# Okabe-Ito colour-blind-safe pair (orange = absorbed wait, blue = real
# execution). Routed through _style.OKABE_ITO so the whole figure suite
# shares one colour-blind-safe source of truth.
COMPONENT_COLORS: dict[str, str] = {
    "wait_s": _style.OKABE_ITO[1],   # orange — the absorbed async wait
    "exec_s": _style.OKABE_ITO[0],   # blue — the real execution
}

COMPONENT_LABELS: dict[str, str] = {
    "wait_s": "Decision wait (time-to-pod-spawn)",
    "exec_s": "Execution runtime",
}


def _pluck(obj: object, key: str) -> float | None:
    """Tolerate ``status: pending`` / absent ``completion_decomposition``."""
    if not isinstance(obj, dict):
        return None
    if obj.get("status") == "pending":
        return None
    v = obj.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Pull median wait + median execution per baseline.

    Returns a DataFrame indexed by ``baseline_key`` with columns
    ``wait_s`` / ``exec_s``. A pending/absent decomposition block is
    recorded as ``NaN`` so the renderer drops it visually.
    """
    rows: list[dict[str, object]] = []
    for bundle in _loaders.load_bundles(list(run_dirs)):
        baselines = bundle.summary.get("baselines") or {}
        proj = baselines.get(bundle.baseline_key, {})
        block = proj.get("completion_decomposition")
        rows.append({
            "baseline_key": bundle.baseline_key,
            "wait_s": _pluck(block, "median_wait_s"),
            "exec_s": _pluck(block, "median_exec_s"),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["baseline_key"] = pd.Categorical(
        df["baseline_key"],
        categories=_present_baseline_order(df["baseline_key"].tolist()),
        ordered=True,
    )
    return df.sort_values("baseline_key").reset_index(drop=True)


def _present_baseline_order(values: list[object]) -> list[str]:
    present = {str(v) for v in values if v is not None}
    canonical = [b for b in _style.BASELINE_ORDER if b in present]
    extras = sorted(b for b in present if b not in _style.BASELINE_ORDER)
    return canonical + extras


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    if df.empty or bool(df[["wait_s", "exec_s"]].isna().to_numpy().all()):
        ax.text(
            0.5, 0.5,
            "No completion_decomposition data — run the analyzer with kube "
            "enrichment on a run whose Jobs carry Job.status.startTime.",
            ha="center", va="center", transform=ax.transAxes, wrap=True,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return

    labels = [_style.baseline_label(b) for b in df["baseline_key"]]
    x = list(range(len(labels)))
    bottom = [0.0] * len(labels)
    for comp in ("wait_s", "exec_s"):
        vals = df[comp].fillna(0.0).to_numpy(dtype=float)
        ax.bar(
            x, vals,
            width=0.55,
            bottom=bottom,
            color=COMPONENT_COLORS[comp],
            label=COMPONENT_LABELS[comp],
            edgecolor="white",
            linewidth=0.5,
        )
        bottom = [b + v for b, v in zip(bottom, vals)]

    # Annotate each stack's total (= completion-time median by construction).
    for xi, total in zip(x, bottom):
        if total > 0:
            ax.text(xi, total, f"{total:.0f}s", ha="center", va="bottom",
                    fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Median time (s)")
    ax.set_xlabel("Baseline")
    ax.set_title("Completion time decomposed: decision wait vs execution (median)")
    ax.legend(loc="upper left", frameon=True)


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
    fig, ax = plt.subplots(figsize=(6.0, 3.8))
    _render(df, ax)
    if not show_title:
        _figures.clear_titles(fig)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
