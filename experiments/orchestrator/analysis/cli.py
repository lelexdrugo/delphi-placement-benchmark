"""argparse wiring for the `analysis` subcommand.

Imported by experiments/orchestrator/cli.py via add_parser; not a
standalone entry point.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .runner import run_analysis


_DEFAULT_REGISTRY = "experiments/specs/cluster-registry.yaml"
_DEFAULT_DB_ENV = "experiments/db/.env"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Wire the `analysis` subcommand onto the orchestrator's CLI."""
    p = subparsers.add_parser(
        "analysis",
        help="Reduce an experiments/runs/<id>/ directory into per-metric "
             "CSVs + summary.json.",
    )
    p.add_argument("--run-dir", required=True,
                   help="Path to experiments/runs/<id>/. Relative paths "
                        "resolve against the repo root.")
    p.add_argument("--baseline", default=None,
                   choices=["static-karmada", "heuristic-scorer", "delphi-full",
                            "Static-Karmada", "Heuristic-Scorer", "DELPHI-Full"],
                   help="Override the baseline label in manifest.json.")
    p.add_argument("--registry", default=_DEFAULT_REGISTRY,
                   help="Path to the REFERENCE (ground-truth) registry "
                        "experiments/specs/cluster-registry.yaml. Intent "
                        "compliance is always scored against this, never a "
                        "variant.")
    p.add_argument("--registry-variant", default=None,
                   help="The ACTIVE registry variant that was "
                        "mounted into the decision-maker for this run. "
                        "Accepts a path or a short id (e.g. 'r2'), resolved "
                        "under experiments/specs/cluster-registry-variants/. "
                        "Used ONLY to compute summary.json.baselines.<bl>."
                        "registry_diff; the reference still scores compliance.")
    p.add_argument("--db-env", default=_DEFAULT_DB_ENV,
                   help="Path to experiments/db/.env (gitignored).")
    p.add_argument("--no-db", action="store_true",
                   help="Skip DB-backed metrics and the no-pollution check.")
    p.add_argument("--no-kube", action="store_true",
                   help="Skip live kubectl queries; placements and Job status "
                        "come from the run's persisted placements.json and "
                        "job-status.json when present.")
    p.add_argument("--no-portforward", action="store_true",
                   help="Skip the auto port-forward helper; assume one is "
                        "already running on PG_LOCAL_PORT (default 5432).")
    p.add_argument("--skip-pollution-check", action="store_true",
                   help="Explicitly skip the no-pollution invariant. Sets "
                        "summary.json.no_pollution_ok = null (loud).")
    p.add_argument("--aggregator-context", default="unique-logical-entrypoint")
    p.add_argument("--aggregator-namespace", default="delphi-experiments")
    p.set_defaults(func=_cmd)
    return p


def _resolve(repo: Path, p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (repo / path)


_VARIANTS_DIR = "experiments/specs/cluster-registry-variants"


def _resolve_variant(repo: Path, value: str | None) -> Path | None:
    """Resolve --registry-variant to a file: a path, a `<id>.yaml`, or a
    `<id>-*.yaml` glob under the variants dir. Raises ValueError on a
    missing / ambiguous id so the caller can fail loudly."""
    if not value:
        return None
    p = Path(value)
    if p.is_absolute() and p.exists():
        return p
    cand = repo / value
    if cand.exists() and cand.is_file():
        return cand
    vdir = repo / _VARIANTS_DIR
    direct = vdir / f"{value}.yaml"
    if direct.exists():
        return direct
    matches = sorted(vdir.glob(f"{value}-*.yaml"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"--registry-variant {value!r} is ambiguous under {vdir}: "
            f"{[m.name for m in matches]}"
        )
    raise ValueError(f"--registry-variant {value!r} not found under {vdir}")


def _cmd(args: argparse.Namespace, repo: Path) -> int:
    run_dir = _resolve(repo, args.run_dir)
    registry_path = _resolve(repo, args.registry)
    try:
        registry_variant_path = _resolve_variant(repo, args.registry_variant)
    except ValueError as exc:
        import sys
        sys.stderr.write(f"{exc}\n")
        return 1

    return run_analysis(
        run_dir=run_dir,
        registry_path=registry_path,
        registry_variant_path=registry_variant_path,
        baseline=args.baseline,
        use_db=not args.no_db,
        use_kube=not args.no_kube,
        skip_pollution_check=args.skip_pollution_check,
        aggregator_context=args.aggregator_context,
        aggregator_namespace=args.aggregator_namespace,
        db_env=_resolve(repo, args.db_env),
        auto_portforward=not args.no_portforward,
    )
