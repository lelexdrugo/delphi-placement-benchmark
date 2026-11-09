"""Registry-degradation — compliance-only single panel.

Same data and same x-axis as ``registry_degradation``, but renders ONLY
the completion-aware hard-tier compliance panel, one line per baseline
across the registry variants. Heuristic-Scorer reads the cluster
registry; DELPHI-Full (it grounds on live state + history + intent) and
Static-Karmada (fixed target) do not. The leftmost point ("full") is the
correct-registry condition.

The three-panel version (``registry_degradation``) additionally plots
completion time and the cost proxy.
"""
from __future__ import annotations

from pathlib import Path

from . import registry_degradation


def generate(
    run_dirs: list[Path],
    out_path: Path,
    *,
    formats: list[str] | None = None,
    synthetic_preview: bool = False,
    show_title: bool = True,
    height_in: float | None = None,
) -> list[Path]:
    # Display names follow the paper's names for the three policies, not
    # the baseline keys.
    labels = {"static-karmada": "Static", "heuristic-scorer": "Registry heuristic",
              "delphi-full": "Agentic"}
    # Completion-aware compliance, drawn as the median of the runs of each
    # cell with min-max bars.
    return registry_degradation.generate(
        run_dirs,
        out_path,
        formats=formats,
        synthetic_preview=synthetic_preview,
        panels=("hard_pass_rate_completion_aware",),
        show_title=show_title,
        height_in=height_in,
        labels=labels,
    )
