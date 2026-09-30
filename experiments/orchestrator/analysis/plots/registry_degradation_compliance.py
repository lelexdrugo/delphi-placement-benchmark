"""Registry-degradation — compliance-only single panel (paper RQ3 figure).

Same data and same x-axis as ``registry_degradation``, but renders ONLY
the intent-compliance panel. This is the figure the paper cites for RQ3:
as the cluster registry drifts (full -> missing -> region-drift ->
compound-drift) the registry-reading **Heuristic-Scorer's** compliance
collapses (0.75 -> 0.65 -> 0.40), while **DELPHI-Full** (registry-blind
by construction; it grounds on live state + history + intent) and
**Static-Karmada** (fixed target) stay flat. The leftmost point ("full")
is the idle/correct-registry parity — the same condition the
``intent-hard-pass-breakdown`` figure shows aggregated (a different
campaign, but the same condition) — so this single panel both
establishes the fair baseline and shows the divergence the breakdown
figure cannot.

The full three-panel version (``registry_degradation``) additionally
plots completion time and the cost proxy; it is retained as a
repo/appendix artifact. The cost panel is deliberately excluded from the
paper (iter-4i operator decision: cost results are documented in-repo but
not reported).
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
    # The paper names the three instances by the view they read, never by
    # their implementation names (one name per thing across text and figure).
    labels = {"static-karmada": "Static", "heuristic-scorer": "Registry heuristic",
              "delphi-full": "Live-state"}
    # Completion-aware compliance, drawn as the median of the runs of each
    # cell with min-max bars; the paper prints the median.
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
