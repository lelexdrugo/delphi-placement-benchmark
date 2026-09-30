"""Shared matplotlib style for Section 6 figures.

One module, one source of truth for palette + rcParams so every
generator agrees on baseline colours, font sizes, and figure
proportions. Generators must call ``apply_style()`` before
``plt.subplots`` so the rcParams overrides take effect.
"""
from __future__ import annotations

from typing import Mapping

import matplotlib as mpl

# Force the non-interactive Agg backend before pyplot is imported.
# Plot generators only write to disk; without this matplotlib tries
# to spin up a GUI (Tk on Windows) and fails on headless / CI hosts.
# Idempotent: matplotlib.use() with force=False is a no-op if the
# user has already set a backend.
mpl.use("Agg", force=False)

import matplotlib.pyplot as plt  # noqa: E402  (must follow mpl.use)

# Canonical baseline keys as written into summary.json by the analyzer
# runner. Order matters: it controls left-to-right column order in
# grouped bar plots.
BASELINE_ORDER: tuple[str, ...] = (
    "static-karmada",
    "heuristic-scorer",
    "delphi-full",
)

BASELINE_DISPLAY: Mapping[str, str] = {
    "static-karmada": "Static-Karmada",
    "heuristic-scorer": "Heuristic-Scorer",
    "delphi-full": "DELPHI-Full",
}

# Tab10 indices 0/2/4 are visually well-separated (blue / green /
# purple) and survive black-and-white print better than adjacent
# indices.
BASELINE_COLORS: Mapping[str, str] = {
    "static-karmada": "#1f77b4",   # tab10[0]  blue
    "heuristic-scorer": "#2ca02c",  # tab10[2]  green
    "delphi-full": "#9467bd",       # tab10[4]  purple
}

# Okabe-Ito colour-blind-safe categorical palette (Okabe & Ito 2008).
# These eight hues stay mutually distinguishable under deuteranopia,
# protanopia, and tritanopia, and also separate in luminance so a
# greyscale print remains legible. Use these — not raw tab10 — for any
# *new* categorical encoding (overhead components, cordon phases) so
# Section 6 figures are reviewer-safe. The baseline palette above is
# kept as-is (blue / green / purple already test colour-blind-OK and
# changing it would churn every landed baseline figure).
OKABE_ITO: tuple[str, ...] = (
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#D55E00",  # vermillion
    "#56B4E9",  # sky blue
    "#F0E442",  # yellow
    "#000000",  # black
)


def okabe_ito(n: int) -> list[str]:
    """Return the first ``n`` Okabe-Ito colour-blind-safe hues.

    Cycles if ``n`` exceeds the palette length (8). Use for categorical
    encodings that are NOT baselines (components, phases) so a single
    colour-blind-safe source of truth backs every Section 6 figure.
    """
    if n <= 0:
        return []
    return [OKABE_ITO[i % len(OKABE_ITO)] for i in range(n)]

# Cluster-state ordering — drives x-axis order on cluster_state plots
# so panels read left-to-right "no load -> increasing skew".
CLUSTER_STATE_ORDER: tuple[str, ...] = (
    "idle",
    "mixed-load",
    "skewed-edge-1-saturated",
    "skewed-on-prem-saturated",
)

# Workload-class ordering for the placement-effectiveness facet.
WORKLOAD_CLASS_ORDER: tuple[str, ...] = ("cpu", "memory", "network")

_RC_PARAMS: dict[str, object] = {
    "font.family": "DejaVu Sans",
    "axes.titlesize": 11,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "figure.titlesize": 12,
    "axes.grid": True,
    "grid.alpha": 0.35,
    "grid.linestyle": "--",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "savefig.bbox": "tight",
    "savefig.dpi": 300,  # publication raster resolution
    "pdf.fonttype": 42,  # TrueType, smaller PDFs and ACM-compatible.
}


def apply_style() -> None:
    """Apply seaborn-v0_8-paper + shared rcParams.

    Idempotent. Call once per ``generate`` invocation, before any
    ``plt.subplots`` so the rcParams take effect on the new Figure.
    """
    # The style sheet ships with matplotlib >=3.6 under the
    # ``seaborn-v0_8-paper`` name (renamed in 3.6 from
    # ``seaborn-paper``). Fall back silently when older.
    style = "seaborn-v0_8-paper" if "seaborn-v0_8-paper" in plt.style.available else "default"
    plt.style.use(style)
    mpl.rcParams.update(_RC_PARAMS)


def baseline_color(key: str) -> str:
    """Return the canonical colour for a baseline key.

    Tolerates the display-cased form (DELPHI-Full) by lower-casing.
    """
    norm = key.lower().replace("delphi-full", "delphi-full")
    return BASELINE_COLORS.get(norm, "#7f7f7f")  # neutral grey fallback


def baseline_label(key: str) -> str:
    """Return the human-readable label for a baseline key."""
    return BASELINE_DISPLAY.get(key.lower(), key)
