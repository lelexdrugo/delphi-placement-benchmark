"""DELPHI evaluation analyzer.

Read-only post-run reducer that turns ``experiments/runs/<id>/`` into
per-metric CSVs, a ``summary.json``, and populated ``.tex`` siblings
for the paper's Section 6 placeholder cells.

See ``experiments/plans/iter-4a-analyzer-and-pilot.md`` for the design.
"""
from __future__ import annotations

from .runner import run_analysis

__all__ = ["run_analysis"]
