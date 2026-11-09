"""DELPHI evaluation analyzer.

Read-only post-run reducer that turns ``experiments/runs/<id>/`` into
per-metric CSVs and a ``summary.json``. See ``README.md`` in this
directory for the operator-level surface.
"""
from __future__ import annotations

from .runner import run_analysis

__all__ = ["run_analysis"]
