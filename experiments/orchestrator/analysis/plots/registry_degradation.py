"""Registry-degradation tolerance — iter-4h-3 figure.

Three stacked panels against the registry-variant x-axis (degradation
order full -> missing -> region-vocabulary-drift -> compound-drift):

  1. intent_compliance.hard_pass_rate   (primary)
  2. completion_time.median_s            (end-to-end completion time;
                                          this is NOT the execution-runtime
                                          placement-quality signal — that
                                          decomposition lives in
                                          completion_decomposition.py)
  3. cost_proxy.total                    (un-confounded cost discriminator)

One line per baseline. The Heuristic-Scorer collapses as *consumed*
labels drift; Static-Karmada (fixed target) and DELPHI-Full (does not
read the static registry) are flat — DELPHI is registry-invariant *by
construction*, so any small wiggle in its line is N=1 crew
nondeterminism, not a registry effect. Compliance and cost_proxy are
scored against the GROUND-TRUTH registry (iter-4h-3 plan §§ 1.3, 9).
Panels 2/3 can diverge from panel 1 because corruption may route a
workload to a faster-but-non-compliant cluster (cost<->speed
orthogonality, § 9.5).

The paper RQ3 figure is the **compliance panel alone** — see
``registry_degradation_compliance``; the completion/cost panels are kept
here for the repo/appendix only (the cost panel in particular is
deliberately out of the paper per the iter-4i operator decision).

Each run-dir supplied is one (baseline, variant) cell; the variant is
read from summary.json's baselines.<baseline>.registry_diff.variant_id.
"""
from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


# Registry-variant x-axis order + display labels.
VARIANT_ORDER: tuple[str, ...] = ("r0", "r1", "r2", "r3", "r4")
VARIANT_LABEL: dict[str, str] = {
    "r0": "full",
    "full": "full",
    "r1": "missing",
    "r2": "region-drift",
    "r3": "compound-drift",
    "r4": "outdated",
}


def _variant_of(summary: dict[str, Any], baseline_key: str) -> Optional[str]:
    proj = (summary.get("baselines") or {}).get(baseline_key) or {}
    vid = (proj.get("registry_diff") or {}).get("variant_id")
    if not vid:
        return None
    return "r0" if vid == "full" else str(vid)


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """One row per RUN, pulled from summary.json. A (variant, baseline)
    cell may hold several runs; _render reduces them to median and
    min-max, never to the first run."""
    rows: list[dict[str, Any]] = []
    for bundle in _loaders.load_bundles(list(run_dirs)):
        bk = bundle.baseline_key
        proj = (bundle.summary.get("baselines") or {}).get(bk) or {}
        variant = _variant_of(bundle.summary, bk)
        if variant is None:
            continue
        rows.append({
            "variant": variant,
            "baseline_key": bk,
            "hard_pass_rate": (proj.get("intent_compliance") or {}).get("hard_pass_rate"),
            "hard_pass_rate_completion_aware": (proj.get("intent_compliance") or {}).get(
                "hard_pass_rate_completion_aware"),
            "completion_median_s": (proj.get("completion_time") or {}).get("median_s"),
            "cost_proxy_total": (proj.get("cost_proxy") or {}).get("total"),
        })
    df = pd.DataFrame(rows, columns=[
        "variant", "baseline_key", "hard_pass_rate", "hard_pass_rate_completion_aware",
        "completion_median_s", "cost_proxy_total",
    ])
    if df.empty:
        return df
    present = [v for v in VARIANT_ORDER if v in set(df["variant"])]
    df["variant"] = pd.Categorical(df["variant"], categories=present, ordered=True)
    df["baseline_key"] = pd.Categorical(
        df["baseline_key"],
        categories=[b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"])],
        ordered=True,
    )
    return df.sort_values(["variant", "baseline_key"]).reset_index(drop=True)


_PANELS: tuple[tuple[str, str, Optional[tuple[float, float]]], ...] = (
    ("hard_pass_rate", "Intent compliance\n(hard_pass_rate)", (0.0, 1.05)),
    ("completion_median_s", "Completion time\nmedian (s)", None),
    ("cost_proxy_total", "Cost proxy\ntotal", None),
    # Completion-aware compliance: the value the paper reports.
    ("hard_pass_rate_completion_aware", "Hard-tier\ncompliance", (0.0, 1.05)),
)
# The repo/appendix three-panel figure keeps its original panels.
_DEFAULT_PANELS = ("hard_pass_rate", "completion_median_s", "cost_proxy_total")


def _select_panels(
    panels: Optional[Iterable[str]],
) -> list[tuple[str, str, Optional[tuple[float, float]]]]:
    """Resolve a requested subset of panel columns to panel specs,
    preserving the canonical top-to-bottom order. ``None`` -> all panels."""
    by_col = {p[0]: p for p in _PANELS}
    if panels is None:
        return [by_col[c] for c in _DEFAULT_PANELS]
    return [by_col[c] for c in panels]


def _render(df: pd.DataFrame, axes, panels_spec, labels: Optional[dict[str, str]] = None) -> None:  # type: ignore[no-untyped-def]
    variants = [v for v in VARIANT_ORDER if v in set(df["variant"].dropna())]
    baselines = [b for b in _style.BASELINE_ORDER if b in set(df["baseline_key"].dropna())]
    x = list(range(len(variants)))

    for ax, (col, ylabel, ylim) in zip(axes, panels_spec):
        for b in baselines:
            ys: list[float] = []
            lo: list[float] = []
            hi: list[float] = []
            for v in variants:
                sub = df[(df["variant"] == v) & (df["baseline_key"] == b)]
                vals = [float(x) if x is not None and pd.notna(x) else None for x in sub[col]]
                # Median over the runs of the cell with min-max bars. A run
                # without a value leaves the point undrawn rather than
                # reporting k-1 runs as k (same rule as reduce_cells).
                if not vals or any(x is None for x in vals):
                    ys.append(float("nan")); lo.append(0.0); hi.append(0.0)
                    continue
                m = float(median(vals))
                ys.append(m); lo.append(m - min(vals)); hi.append(max(vals) - m)
            ax.errorbar(x, ys, yerr=[lo, hi], marker="o", markersize=3.5, linewidth=1.4,
                        capsize=2.5, elinewidth=0.9,
                        color=_style.baseline_color(b),
                        label=(labels or {}).get(b, _style.baseline_label(b)))
        ax.set_ylabel(ylabel)
        if ylim:
            ax.set_ylim(*ylim)

    axes[0].set_title("Registry-degradation tolerance")
    if len(axes) == 1:
        # Compliance-only paper panel: park the legend above the axes in a
        # single row so it never crosses the lines and fits a narrow
        # single-column figure. constrained_layout reserves the strip.
        axes[0].legend(loc="lower center", bbox_to_anchor=(0.5, 1.005),
                       ncol=3, frameon=False, fontsize=7,
                       columnspacing=1.0, handletextpad=0.4)
    else:
        axes[0].legend(loc="lower left", frameon=True)
    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels([VARIANT_LABEL.get(v, v) for v in variants],
                             rotation=12, ha="right")
    # The variants differ in kind (missing labels, region drift, both), so the axis
    # names them without implying an order of severity (Mid4CC blind review
    # 2026-09-28, F13).
    axes[-1].set_xlabel("Registry variant")


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
    panels: Iterable[str] | None = None,
    show_title: bool = True,
    height_in: float | None = None,
    labels: Optional[dict[str, str]] = None,
) -> list[Path]:
    """Render the registry-degradation figure.

    ``panels`` selects which metric panels to draw (by summary column
    name); ``None`` draws all three. Pass ``("hard_pass_rate",)`` for the
    compliance-only paper figure (see ``registry_degradation_compliance``).
    """
    formats = formats or ["pdf", "png"]
    _style.apply_style()
    df = _prepare(run_dirs)
    panels_spec = _select_panels(panels)
    n = len(panels_spec)
    # Single-panel = the paper's compliance figure: size it for one ACM
    # column so it renders crisp at width=\linewidth. Multi-panel stays
    # the wider repo/appendix artifact.
    height = height_in or (7.2 if n >= 3 else 2.9)
    width = 3.5 if n == 1 else 6.5
    fig, axes = plt.subplots(n, 1, figsize=(width, height), sharex=True,
                             squeeze=False, constrained_layout=True)
    _render(df, list(axes[:, 0]), panels_spec, labels)
    if not show_title:
        _figures.clear_titles(fig)
    if synthetic_preview:
        _figures.add_synthetic_watermark(fig)
    return _figures.save_figure(
        fig, out_path, formats=formats, synthetic_preview=synthetic_preview,
    )
