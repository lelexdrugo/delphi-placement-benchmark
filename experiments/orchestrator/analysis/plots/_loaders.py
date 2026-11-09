"""Read-only loaders for plot inputs.

Plots consume two kinds of input from a run directory:

- ``analysis/summary.json``           per-baseline aggregate.
- ``analysis/<metric>.csv``           per-job rows for distributions
  and per-(state, baseline) facets.

These helpers tolerate the synthetic fixtures (which live under
``tests/fixtures/synthetic-*/analysis/``) as well as real run-dirs
under ``experiments/runs/<id>/analysis/``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


@dataclass(frozen=True)
class RunBundle:
    """Everything one generator might need from one run-dir.

    Generators pull what they need; absent CSVs come back as empty
    DataFrames so callers can detect "metric unavailable" by
    ``df.empty`` rather than a try/except.
    """
    run_dir: Path
    baseline_key: str
    summary: dict[str, Any]
    placement_success: pd.DataFrame
    completion_time: pd.DataFrame
    completion_decomposition: pd.DataFrame
    decision_latency: pd.DataFrame
    intent_compliance: pd.DataFrame
    cost_proxy: pd.DataFrame
    data_movement: pd.DataFrame
    traceability: pd.DataFrame


def _analysis_dir(run_dir: Path) -> Path:
    """Resolve the per-run analysis output dir.

    Synthetic fixtures and real runs both follow
    ``<run-dir>/analysis/``, so this is a single concat.
    """
    return run_dir / "analysis"


def _read_csv_safe(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    # Pandas reads empty CSVs as a 0×0 DataFrame; guard the empty-file
    # case to keep the contract uniform.
    return df


def load_summary(run_dir: Path) -> dict[str, Any]:
    """Load ``<run-dir>/analysis/summary.json``."""
    path = _analysis_dir(run_dir) / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"summary.json not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def baseline_key_of(summary: dict[str, Any]) -> str:
    """Return the canonical baseline key of a single-baseline summary.

    The analyzer emits ``baseline`` at the top level and the same
    string as the single key under ``baselines``. Prefer top-level
    when present.
    """
    top = summary.get("baseline")
    if top:
        return str(top).lower()
    keys = list((summary.get("baselines") or {}).keys())
    return keys[0].lower() if keys else "unknown"


def load_bundle(run_dir: Path) -> RunBundle:
    """Load every metric the plot generators might consume.

    Per-metric CSVs that don't exist (e.g. data-movement on a run
    where every job was region=any) become empty DataFrames so
    generators can fall back cleanly.
    """
    summary = load_summary(run_dir)
    adir = _analysis_dir(run_dir)
    return RunBundle(
        run_dir=run_dir,
        baseline_key=baseline_key_of(summary),
        summary=summary,
        placement_success=_read_csv_safe(adir / "placement-success.csv"),
        completion_time=_read_csv_safe(adir / "completion-time.csv"),
        completion_decomposition=_read_csv_safe(adir / "completion-decomposition.csv"),
        decision_latency=_read_csv_safe(adir / "decision-latency.csv"),
        intent_compliance=_read_csv_safe(adir / "intent-compliance.csv"),
        cost_proxy=_read_csv_safe(adir / "cost-proxy.csv"),
        data_movement=_read_csv_safe(adir / "data-movement.csv"),
        traceability=_read_csv_safe(adir / "traceability.csv"),
    )


def load_bundles(run_dirs: list[Path]) -> list[RunBundle]:
    """Load multiple run-dirs in the order given.

    Plot generators rely on the order to control left-to-right column
    layout when the caller passes baselines in a non-canonical order.
    """
    return [load_bundle(p) for p in run_dirs]


def summary_metric(summary: dict[str, Any], metric: str, key: Optional[str] = None) -> Any:
    """Pluck ``summary['baselines'][<baseline-key>][metric]``.

    When ``key`` is omitted, the first baseline key in the summary is
    used — convenient for the analyzer's single-baseline runs.
    """
    baselines = summary.get("baselines") or {}
    eff_key = key or next(iter(baselines.keys()), None)
    if eff_key is None:
        return None
    return (baselines.get(eff_key) or {}).get(metric)
