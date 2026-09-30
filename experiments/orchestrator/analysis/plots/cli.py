"""argparse wiring for the ``plots`` top-level subcommand.

Wired by ``experiments/orchestrator/cli.py`` via ``add_parser``.
Mirrors the ``calibration <action>`` pattern: one sub-subparser per
figure module, each accepting ``--run-dirs N+``, ``--out``, and a
common set of formatting flags.

The full command shape is::

    python -m experiments.orchestrator plots \\
        placement-effectiveness \\
        --run-dirs runs/A runs/B runs/C \\
        --out docs/.../figs/placement-effectiveness \\
        --format both \\
        [--synthetic-preview]

A top-level ``plots`` was chosen over ``analysis plots`` so the
existing ``analysis --run-dir`` reducer keeps working unchanged.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from . import (
    cluster_state,
    completion_decomposition,
    cordon_avoidance,
    intent_hard_pass_breakdown,
    latency_vs_compliance_tradeoff,
    overhead,
    placement_effectiveness,
    registry_degradation,
    registry_degradation_compliance,
    sensitivity,
    traceability,
)


# Each entry: subcommand name -> (help, generator module).
_GENERATORS: tuple[tuple[str, str, object], ...] = (
    (
        "placement-effectiveness",
        "Grouped bar of intent-compliance (hard tier) per workload class and baseline. "
        "iter-4g.1 note: tautological on iter-4d (placement_success=1.0 across baselines); "
        "kept for future RQ3/RQ4 campaigns where it may differentiate.",
        placement_effectiveness,
    ),
    (
        "cluster-state",
        "Boxplot of completion time per (cluster state, baseline). iter-4g.1 demoted "
        "to appendix (iter-4d ran idle-only; multi-state expansion lands in iter-4d.2).",
        cluster_state,
    ),
    (
        "intent-hard-pass-breakdown",
        "iter-4g.1 NEW: per-intent-profile hard_pass_rate, grouped by baseline. "
        "Surfaces where each baseline's compliance budget breaks (paper-critical RQ1).",
        intent_hard_pass_breakdown,
    ),
    (
        "latency-vs-compliance-tradeoff",
        "iter-4g.1 NEW: scatter of (decision_latency.median_ms, intent.hard_pass_rate) "
        "per baseline, log x-axis. Visualises the systems tradeoff (paper-critical RQ1/RQ2).",
        latency_vs_compliance_tradeoff,
    ),
    (
        "overhead",
        "Stacked-bar middleware overhead decomposition per baseline. "
        "Admission + telemetry components are SYNTHETIC until iter-4d.1.",
        overhead,
    ),
    (
        "completion-decomposition",
        "iter-4d.0b: stacked-bar split of completion_time into decision "
        "wait (time-to-pod-spawn) + execution runtime, per baseline. Reads "
        "each run-dir's summary.json completion_decomposition block; a "
        "pending/absent block renders empty. Shows DELPHI's tall completion "
        "bar is mostly decision wait, not execution (RQ1/RQ2).",
        completion_decomposition,
    ),
    (
        "sensitivity",
        "Two-panel sensitivity plot (staleness + reasoning delay). "
        "Curves are SYNTHETIC until iter-4f.",
        sensitivity,
    ),
    (
        "traceability",
        "Heatmap of cluster scores per representative job (one per intent profile).",
        traceability,
    ),
    (
        "cordon-avoidance",
        "iter-4h-4: grouped bars (pre/during/post cordon) of the fraction of "
        "decisions targeting the cordoned cluster, per baseline. Reads each "
        "run-dir's summary.json cordon_response aggregate (RQ4).",
        cordon_avoidance,
    ),
    (
        "registry-degradation",
        "iter-4h-3: three-panel registry-degradation tolerance "
        "(hard_pass_rate + completion median + cost_proxy) vs registry variant, "
        "one line per baseline. Pass all (baseline x variant) run-dirs via --run-dirs; "
        "each cell's variant comes from summary.json registry_diff.variant_id. "
        "Repo/appendix artifact — the paper cites the compliance-only variant.",
        registry_degradation,
    ),
    (
        "registry-degradation-compliance",
        "iter-4h-3 PAPER RQ3 figure: the compliance panel only of "
        "registry-degradation (Heuristic collapses as the registry drifts; "
        "DELPHI + Static stay flat). Same --run-dirs as registry-degradation. "
        "Excludes the completion/cost panels (cost is out of the paper per the "
        "iter-4i operator decision).",
        registry_degradation_compliance,
    ),
)


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Wire the ``plots`` subcommand onto the orchestrator CLI."""
    p = subparsers.add_parser(
        "plots",
        help="Section 6 figure generators: render PDFs/PNGs from "
             "summary.json + per-metric CSVs in one or more run-dirs.",
    )
    plots_sub = p.add_subparsers(dest="plot_action", required=True)
    for name, summary, module in _GENERATORS:
        pp = plots_sub.add_parser(name, help=summary)
        _add_common_args(pp)
        pp.set_defaults(func=_make_handler(module))
    return p


def _add_common_args(pp: argparse.ArgumentParser) -> None:
    pp.add_argument(
        "--run-dirs", nargs="+", required=True,
        help="One or more run directories. Each must contain "
             "analysis/summary.json (and the per-metric CSVs the "
             "generator consumes).",
    )
    pp.add_argument(
        "--out", required=True,
        help="Output path stem. Extensions (.pdf, .png, .SYNTHETIC.pdf) "
             "are appended automatically.",
    )
    pp.add_argument(
        "--format", default="both", choices=("pdf", "png", "both"),
        help="Output format. Default: both. 'both' writes <stem>.pdf "
             "and <stem>.png side-by-side.",
    )
    pp.add_argument(
        "--synthetic-preview", action="store_true",
        help="Stamp the figure with a SYNTHETIC watermark and emit "
             "with '.SYNTHETIC' before the extension. Use whenever "
             "the input run-dirs are not real measurements — keeps "
             "previews un-citable.",
    )
    pp.add_argument(
        "--height-in", type=float, default=None,
        help="Figure height in inches. Only honoured by generators that "
             "accept a height_in argument (registry-degradation*).",
    )
    pp.add_argument(
        "--no-title", action="store_true",
        help="Suppress the in-axes title so the figure is camera-ready: "
             "the description belongs in the LaTeX \\caption, not an "
             "iteration-specific in-axes title. Only honoured by "
             "generators that accept a show_title argument; ignored by "
             "the rest.",
    )


def _make_handler(module: object) -> Callable[[argparse.Namespace, Path], int]:
    def handler(args: argparse.Namespace, repo: Path) -> int:
        run_dirs = [_resolve(repo, d) for d in args.run_dirs]
        out_path = _resolve(repo, args.out)
        formats = ["pdf", "png"] if args.format == "both" else [args.format]
        kwargs: dict[str, object] = {
            "formats": formats,
            "synthetic_preview": args.synthetic_preview,
        }
        # Only pass show_title to generators that declare it, so the
        # shared --no-title flag works without every generator having to
        # accept it.
        import inspect
        if "show_title" in inspect.signature(module.generate).parameters:  # type: ignore[attr-defined]
            kwargs["show_title"] = not args.no_title
        if args.height_in and "height_in" in inspect.signature(module.generate).parameters:  # type: ignore[attr-defined]
            kwargs["height_in"] = args.height_in
        written = module.generate(  # type: ignore[attr-defined]
            run_dirs,
            out_path,
            **kwargs,
        )
        for p in written:
            print(f"wrote {p}")
        return 0
    return handler


def _resolve(repo: Path, p: str) -> Path:
    """Resolve a CLI-supplied path against the repo root if relative."""
    path = Path(p)
    return path if path.is_absolute() else (repo / path)
