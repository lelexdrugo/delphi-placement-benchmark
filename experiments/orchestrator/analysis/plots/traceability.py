"""Score-map heatmap for representative jobs — RQ-traceability figure.

Reads ``traceability.csv`` from a single run-dir (typically the
DELPHI-Full Phase-B run) and shows the per-cluster scores for a
small set of representative jobs — one per intent profile so the
visual makes the intent → placement reasoning legible.

Columns are the four candidate clusters in a fixed order
(on-prem, public-cloud, edge-1, edge-2). Cell value is the score
DELPHI computed; the selected cluster is highlighted with a thick
border so the reader can read off the placement at a glance.

Synthetic-preview path renders the same shape against any of the
fixtures under ``tests/fixtures/synthetic-*``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

from . import _figures, _loaders, _style


CLUSTER_COL_ORDER: tuple[str, ...] = ("on-prem", "public-cloud", "edge-1", "edge-2")
INTENT_PROFILES: tuple[str, ...] = (
    "balanced", "cost-aware", "latency-sensitive", "locality-aware",
)


def _parse_score_blob(blob: str) -> dict[str, float]:
    """Parse 'on-prem=0.90;edge-1=0.70;edge-2=0.70;public-cloud=0.30'."""
    if not isinstance(blob, str) or not blob.strip():
        return {}
    out: dict[str, float] = {}
    for part in blob.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        try:
            out[k.strip()] = float(v.strip())
        except ValueError:
            continue
    return out


def _prepare(run_dirs: Iterable[Path]) -> pd.DataFrame:
    """Pick one representative job per intent_profile from the first
    non-empty traceability.csv in ``run_dirs``.

    Returns a DataFrame indexed by ``intent_profile`` with one row per
    profile and one column per cluster (``score_<cluster>``), plus a
    ``selected_cluster`` column carrying the placement decision.
    """
    bundles = _loaders.load_bundles(list(run_dirs))
    # Prefer DELPHI-Full when present — it's the baseline with varied
    # score maps; the other baselines' traceability tables degenerate
    # (Static-Karmada picks the same cluster for everything).
    # Fall back to the first non-empty bundle otherwise.
    src: pd.DataFrame | None = None
    delphi_match = next(
        (b for b in bundles
         if b.baseline_key == "delphi-full" and not b.traceability.empty),
        None,
    )
    if delphi_match is not None:
        src = delphi_match.traceability
    else:
        for bundle in bundles:
            if not bundle.traceability.empty:
                src = bundle.traceability
                break
    if src is None:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    for profile in INTENT_PROFILES:
        cand = src[src["intent_profile"] == profile]
        if cand.empty:
            continue
        # Deterministically pick the first job (sorted by job_name) so
        # the figure is stable across re-runs.
        chosen = cand.sort_values("job_name").iloc[0]
        score_map = _parse_score_blob(chosen.get("clusters_score", ""))
        row: dict[str, object] = {
            "intent_profile": profile,
            "job_name": chosen["job_name"],
            "selected_cluster": chosen.get("selected_cluster", ""),
        }
        for cluster in CLUSTER_COL_ORDER:
            row[f"score_{cluster}"] = float(score_map.get(cluster, 0.0))
        rows.append(row)
    return pd.DataFrame(rows)


def _render(df: pd.DataFrame, ax: plt.Axes) -> None:  # type: ignore[name-defined]
    if df.empty:
        ax.text(
            0.5, 0.5,
            "No traceability rows. Provide a run-dir with a non-empty traceability.csv.",
            ha="center", va="center", transform=ax.transAxes,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return

    matrix = df[[f"score_{c}" for c in CLUSTER_COL_ORDER]].to_numpy(dtype=float)
    profiles = df["intent_profile"].tolist()
    selected = df["selected_cluster"].tolist()

    im = ax.imshow(matrix, aspect="auto", cmap="viridis", vmin=0.0, vmax=1.1)
    ax.set_xticks(range(len(CLUSTER_COL_ORDER)))
    ax.set_xticklabels(CLUSTER_COL_ORDER, rotation=20, ha="right")
    ax.set_yticks(range(len(profiles)))
    ax.set_yticklabels([p.capitalize() for p in profiles])
    ax.set_title("Cluster scores per intent profile")

    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            ax.text(
                j, i, f"{matrix[i, j]:.2f}",
                ha="center", va="center",
                fontsize=8,
                color="white" if matrix[i, j] < 0.6 else "black",
            )

    # Outline the selected cluster column for each row.
    for i, sel in enumerate(selected):
        if sel in CLUSTER_COL_ORDER:
            j = CLUSTER_COL_ORDER.index(sel)
            ax.add_patch(
                _selection_rect(j, i)
            )

    cbar = ax.figure.colorbar(im, ax=ax, shrink=0.85)
    cbar.set_label("Score")


def _selection_rect(j: int, i: int):  # type: ignore[no-untyped-def]
    """Thick black border around the selected-cluster cell."""
    from matplotlib.patches import Rectangle
    return Rectangle(
        (j - 0.5, i - 0.5), 1.0, 1.0,
        fill=False,
        edgecolor="red",
        linewidth=2.0,
        zorder=10,
    )


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
