"""Middleware overhead decomposition — RQ2 figure.

Stacked bar per baseline of three overhead components from
``summary.json``:

- ``admission_overhead.median_ms``    (PENDING iter-4d.1 paired runs)
- ``decision_latency.median_ms``      (OK — fully observed)
- ``telemetry_overhead.median_ms``    (PENDING iter-4d.1 paired runs)

Until paired runs land, admission and telemetry overheads come from
synthetic fixtures (status="ok" + note="SYNTHETIC"). The figure
emitter writes the ``.SYNTHETIC`` preview suffix and stamps a
diagonal watermark whenever ``synthetic_preview=True`` so the
fixture-driven render cannot be mistaken for citable data.

Generator pattern: P3 — functioning with synthetic data + watermark.
The user picked P3 over the safer P2 placeholder; the watermark and
filename suffix are the agreed-upon mitigation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


COMPONENT_COLORS: dict[str, str] = {
    "admission_ms": "#ff7f0e",   # tab10 orange
    "decision_ms": "#9467bd",    # tab10 purple (matches DELPHI-Full main colour)
    "telemetry_ms": "#8c564b",   # tab10 brown
}

COMPONENT_LABELS: dict[str, str] = {
    "admission_ms": "Admission webhook",
    "decision_ms": "Decision latency",
    "telemetry_ms": "Telemetry collector",
}


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Pull the three overhead medians per baseline.

    Returns a DataFrame indexed by ``baseline_key`` with columns
    ``admission_ms`` / ``decision_ms`` / ``telemetry_ms``. A missing
    metric (status="pending" or absent) is recorded as ``NaN`` so the
    renderer can drop / mark it explicitly.
    """
    rows: list[dict[str, object]] = []
    for bundle in _loaders.load_bundles(list(run_dirs)):
        baselines = bundle.summary.get("baselines") or {}
        proj = baselines.get(bundle.baseline_key, {})
        rows.append({
            "baseline_key": bundle.baseline_key,
            "admission_ms": _pluck(proj.get("admission_overhead"), "median_ms"),
            "decision_ms": _pluck(proj.get("decision_latency"), "median_ms"),
            "telemetry_ms": _pluck(proj.get("telemetry_overhead"), "median_ms"),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["baseline_key"] = pd.Categorical(
        df["baseline_key"],
        categories=_present_baseline_order(df["baseline_key"]),
        ordered=True,
    )
    return df.sort_values("baseline_key").reset_index(drop=True)


def _present_baseline_order(series: pd.Series) -> list[str]:
    """Return canonical order with any non-canonical baseline appended.

    Allows test fixtures (and future baselines) to flow through without
    triggering a pandas Categorical deprecation warning when a value
    isn't in BASELINE_ORDER.
    """
    present = set(series.dropna())
    canonical = [b for b in _style.BASELINE_ORDER if b in present]
    extras = sorted(b for b in present if b not in _style.BASELINE_ORDER)
    return canonical + extras


def _pluck(obj: object, key: str) -> float | None:
    """Tolerate ``status: pending`` or absent metric block."""
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


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    if df.empty:
        ax.text(
            0.5, 0.5,
            "No overhead data — pass --run-dirs with summary.json files.",
            ha="center", va="center", transform=ax.transAxes,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return

    labels = [_style.baseline_label(b) for b in df["baseline_key"]]
    x = list(range(len(labels)))
    bottom = [0.0] * len(labels)
    for comp in ("admission_ms", "decision_ms", "telemetry_ms"):
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

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Median overhead (ms)")
    ax.set_xlabel("Baseline")
    ax.set_title("Middleware overhead decomposition (median)")
    ax.legend(loc="upper left", frameon=True)
    ax.set_yscale("symlog", linthresh=10)


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
    fig, ax = plt.subplots(figsize=(6.0, 3.8))
    _render(df, ax)
    if synthetic_preview:
        _figures.add_synthetic_watermark(
            fig,
            footer="SYNTHETIC — admission & telemetry components pending iter-4d.1 paired runs.",
        )
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
