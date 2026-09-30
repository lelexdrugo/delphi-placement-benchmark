"""Orchestrator CLI.

iter-1 wired:
    render        spec -> per-Job YAMLs + planned timeline
    dry-run       kubectl server-side validation of every rendered Job

iter-2b extends with:
    prep          render the background-load Jobs of a cluster-state spec
    render-bootstrap
                  expand a Phase-A coverage spec into per-state foreground+background jobs

iter-3a wires `submit` to real apply (gated by
--i-know-this-runs-real-jobs).
iter-4a wires `analysis` (per-run reducer) and extends `collect` to
update manifest with per-job applied_at_utc.
iter-4b wires `calibration` (RTT probe render / submit / reduce / cleanup).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import cluster_state as _cluster_state
from . import collect as _collect
from . import render as _render
from . import submit as _submit
from .calibration import render as _calib_render
from .calibration import submit as _calib_submit
from .calibration import reduce as _calib_reduce
from .analysis import cli as _analysis_cli
from .analysis import tex_template as _tex
from .analysis.plots import cli as _plots_cli


_REPO_ROOT_MARKERS = (".git",)
_DEFAULT_REGISTRY = "experiments/specs/cluster-registry.yaml"
_DEFAULT_TEMPLATES = "experiments/templates"
_DEFAULT_RUNS = "experiments/runs"
_DEFAULT_CLUSTER_STATES = "experiments/specs/cluster-states"
_DEFAULT_CALIBRATION_TEMPLATES = "experiments/calibration"


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    while True:
        if any((cur / m).exists() for m in _REPO_ROOT_MARKERS):
            return cur
        if cur.parent == cur:
            return start.resolve()
        cur = cur.parent


def _resolve(repo: Path, p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (repo / path)


def _auto_run_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}"


def _cmd_render(args: argparse.Namespace, repo: Path) -> int:
    spec_path = _resolve(repo, args.spec)
    registry_path = _resolve(repo, args.registry)
    templates_dir = _resolve(repo, args.templates)
    run_id = args.run_id or _auto_run_id(spec_path.stem)
    output_dir = _resolve(repo, args.runs) / run_id

    # iter-4h-2: multi-state render path.
    multistate_states = getattr(args, "multistate_states", None)
    if multistate_states:
        # iter-4h-4: --cordon-window is single-spec only. Warn loudly
        # rather than silently no-op if both are passed.
        if getattr(args, "cordon_window", None):
            print("[render] WARNING: --cordon-window is ignored under "
                  "--multistate-states (cordon is single-spec layout only "
                  "this iter; see iter-4h-4-cordon-taint.md).", file=sys.stderr)
        states = [s.strip() for s in multistate_states.split(",") if s.strip()]
        if not states:
            print("--multistate-states is empty after splitting on commas",
                  file=sys.stderr)
            return 1
        cluster_states_dir = _resolve(
            repo, getattr(args, "cluster_states_dir", _DEFAULT_CLUSTER_STATES),
        )
        summary = _render.render_multistate(
            spec_path=spec_path,
            registry_path=registry_path,
            cluster_states_dir=cluster_states_dir,
            run_id=run_id,
            templates_dir=templates_dir,
            output_dir=output_dir,
            state_ids=states,
            baseline=args.baseline,
        )
        # Write a minimal top-level manifest pointing at multistate.json
        # so `analysis` / `collect` paths that read manifest.json can
        # still find the run-level metadata.
        manifest_path = output_dir / "manifest.json"
        manifest_payload = {
            "run_id": run_id,
            "spec_path": str(spec_path),
            "spec_id": summary["spec_id"],
            "baseline": args.baseline,
            "layout": "multistate",
            "states": [s["state_id"] for s in summary["states"]],
            "multistate_summary": "multistate.json",
        }
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        print(f"run_id              : {run_id}")
        print(f"layout              : multistate")
        print(f"states              : {', '.join(states)}")
        print(f"total_foreground    : {summary['total_foreground_jobs']}")
        print(f"total_disturbance   : {summary['total_disturbance_jobs']}")
        print(f"total_background    : {summary['total_background_jobs']}")
        print(f"output_dir          : {output_dir}")
        print(f"multistate.json     : {output_dir / 'multistate.json'}")
        if getattr(args, "dry_spawn", False):
            print("\n[dry-spawn] no kubectl apply will run; rendered YAMLs "
                  "and disturbance-timeline-planned.csv files are on disk "
                  "for review. Run `submit --run-id ...` against this "
                  "same run-dir to do a real apply.")
        return 0

    # Legacy single-spec render path.
    jobs = _render.render(
        spec_path=spec_path,
        registry_path=registry_path,
        run_id=run_id,
        templates_dir=templates_dir,
        output_dir=output_dir,
    )

    # iter-4h-4: optional cordon window. Recorded in the manifest so
    # submit_real drives the timed cordon/uncordon. Single-spec layout
    # only (the multistate render path writes its own manifest).
    cordon_window = None
    cordon_window_arg = getattr(args, "cordon_window", None)
    if cordon_window_arg:
        import yaml  # local import keeps `--help` fast
        cw_path = _resolve(repo, cordon_window_arg)
        if not cw_path.exists():
            print(f"--cordon-window file not found: {cw_path}", file=sys.stderr)
            return 1
        with cw_path.open("r", encoding="utf-8") as fh:
            cordon_window = yaml.safe_load(fh)

    manifest_path = _collect.emit_initial_manifest(
        output_dir=output_dir,
        run_id=run_id,
        spec_path=spec_path,
        baseline=args.baseline,
        jobs=jobs,
        cordon_window=cordon_window,
    )

    print(f"run_id           : {run_id}")
    print(f"rendered_jobs    : {len(jobs)}")
    print(f"output_dir       : {output_dir}")
    print(f"manifest         : {manifest_path}")
    print(f"timeline (planned): {output_dir / 'timeline-planned.csv'}")
    if cordon_window is not None:
        print(f"cordon_window    : {cordon_window.get('cordon_target')} "
              f"[{cordon_window.get('cordon_start_fraction')}, "
              f"{cordon_window.get('cordon_end_fraction')}]")
    return 0


def _cmd_dry_run(args: argparse.Namespace, repo: Path) -> int:
    run_dir = _resolve(repo, args.runs) / args.run_id
    jobs_dir = run_dir / "jobs"
    if not jobs_dir.exists():
        print(f"no rendered jobs at {jobs_dir}; run `render` first", file=sys.stderr)
        return 1

    job_paths = sorted(jobs_dir.glob("*.yaml"))
    if not job_paths:
        print(f"no *.yaml files under {jobs_dir}", file=sys.stderr)
        return 1

    results = _submit.dry_run(job_paths, context=args.context)
    failures = 0
    for r in results:
        status = "OK" if r.ok else f"FAIL(exit={r.exit_code})"
        print(f"{status:10s}  {r.job_path.name}")
        if not r.ok:
            failures += 1
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            for line in tail:
                print(f"    {line}")
    print(f"\nsummary: {len(results) - failures}/{len(results)} OK", file=sys.stderr)
    return 0 if failures == 0 else 2


def _cmd_prep(args: argparse.Namespace, repo: Path) -> int:
    state_path = _resolve(repo, args.cluster_state)
    templates_dir = _resolve(repo, args.templates)
    run_id = args.run_id or _auto_run_id(state_path.stem)
    output_dir = _resolve(repo, args.runs) / run_id

    bg_jobs = _cluster_state.render_cluster_state(
        state_path=state_path,
        run_id=run_id,
        templates_dir=templates_dir,
        output_dir=output_dir,
        namespace=args.namespace,
    )

    print(f"run_id            : {run_id}")
    print(f"cluster_state     : {state_path.name}")
    print(f"background_jobs   : {len(bg_jobs)}")
    print(f"output_dir        : {output_dir}")
    for j in bg_jobs:
        print(f"  - {j.workload_class:6s} pin->{j.pin_to:14s} {j.path.name}")
    return 0


def _cmd_render_bootstrap(args: argparse.Namespace, repo: Path) -> int:
    spec_path = _resolve(repo, args.spec)
    registry_path = _resolve(repo, args.registry)
    templates_dir = _resolve(repo, args.templates)
    cluster_states_dir = _resolve(repo, args.cluster_states_dir)
    run_id = args.run_id or _auto_run_id(spec_path.stem)
    output_dir = _resolve(repo, args.runs) / run_id

    summary = _render.render_bootstrap(
        spec_path=spec_path,
        registry_path=registry_path,
        cluster_states_dir=cluster_states_dir,
        run_id=run_id,
        templates_dir=templates_dir,
        output_dir=output_dir,
    )

    manifest_path = output_dir / "bootstrap.json"
    manifest_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"run_id                  : {run_id}")
    print(f"spec                    : {spec_path.name}")
    print(f"output_dir              : {output_dir}")
    print(f"states                  : {len(summary['states'])}")
    print(f"total_foreground_jobs   : {summary['total_foreground_jobs']}")
    print(f"total_background_jobs   : {summary['total_background_jobs']}")
    for s in summary["states"]:
        print(f"  - {s['state_id']:36s} bg={s['background_jobs']:3d}  fg={s['foreground_jobs']:3d}")
    print(f"manifest                : {manifest_path}")
    return 0


def _cmd_submit(args: argparse.Namespace, repo: Path) -> int:
    run_dir = _resolve(repo, args.runs) / args.run_id
    cluster_states_dir = _resolve(repo, args.cluster_states_dir)
    if not args.i_know_this_runs_real_jobs:
        print(
            "refusing to submit: pass --i-know-this-runs-real-jobs to "
            "acknowledge that this will apply Jobs to the live cluster. "
            "Background-load Jobs in delphi-experiments are cleaned up at "
            "end-of-run via the kubectl delete carve-out.",
            file=sys.stderr,
        )
        return 3
    rc = _submit.submit_real(
        run_dir=run_dir,
        context=args.context,
        namespace=args.namespace,
        acknowledged=True,
        fg_deadline_seconds=args.fg_deadline_seconds,
        poll_seconds=args.poll_seconds,
        cluster_states_dir=cluster_states_dir,
        keep_background=args.keep_background,
        keep_disturbance=getattr(args, "keep_disturbance", False),
        stall_threshold_seconds=args.stall_threshold_seconds,
        fail_fast=args.fail_fast,
    )
    # Persist what the analyzer reads live (each foreground Job's PP
    # target + aggregated Job.status startTime/completionTime) NOW, while
    # the Jobs and PPs certainly exist (the operator's end-of-run
    # `delete jobs` removes both, after which a run could otherwise no
    # longer be re-scored). Read-only kubectl get; never raises, so a
    # failed snapshot cannot cost the manifest collection.
    _collect.capture_kube_records(run_dir, context=args.context,
                                  namespace=args.namespace)
    # iter-4a: after submit returns, update manifest.json with the
    # actual_submit_timestamp_utc / submitted_at / finished_at fields
    # the analyzer needs (and mirror placements.json into
    # assigned_cluster). Tolerant of submit failures: collect always
    # runs so the manifest reflects what happened.
    try:
        _collect.collect_results(run_dir)
    except FileNotFoundError as exc:
        print(f"[submit] collect skipped: {exc}", file=sys.stderr)
    return rc


def _cmd_collect(args: argparse.Namespace, repo: Path) -> int:
    run_dir = _resolve(repo, args.runs) / args.run_id
    if getattr(args, "capture_kube_records", False):
        # Opt-in: plain `collect` stays cluster-free. Use this to backfill
        # a run whose Jobs are still in the namespace (e.g. submitted
        # before these artifacts existed, submit died before its tail, or
        # a job finished after the tail captured it).
        _collect.capture_kube_records(run_dir, context=args.context,
                                      namespace=args.namespace)
    try:
        manifest = _collect.collect_results(run_dir)
    except FileNotFoundError as exc:
        print(f"collect: {exc}", file=sys.stderr)
        return 1
    print(f"manifest updated: {run_dir / 'manifest.json'}")
    print(f"submitted_at: {manifest.get('submitted_at')}")
    print(f"finished_at : {manifest.get('finished_at')}")
    print(f"jobs        : {len(manifest.get('jobs') or [])}")
    return 0


# ---------------------------------------------------------------------------
# iter-4b: RTT calibration subcommands
# ---------------------------------------------------------------------------

def _cmd_calib_render(args: argparse.Namespace, repo: Path) -> int:
    """Render 12 pinned iperf3 probe Jobs into a run directory."""
    run_id = args.run_id or _auto_run_id("calibration")
    output_dir = _resolve(repo, args.runs) / run_id
    templates_dir = _resolve(repo, args.templates)
    sources = tuple(args.sources) if args.sources else _calib_render.DEFAULT_SOURCES

    plan = _calib_render.render(
        run_id=run_id,
        output_dir=output_dir,
        templates_dir=templates_dir,
        target_host=args.target_host,
        sources=sources,
        target=args.target,
        replicas=args.replicas,
        target_port=args.target_port,
        duration_seconds=args.duration_seconds,
        namespace=args.namespace,
        image=args.image,
    )
    print(f"run_id     : {run_id}")
    print(f"output_dir : {output_dir}")
    print(f"target_host: {plan.target_host}:{plan.target_port}")
    print(f"image      : {plan.image}")
    print(f"probes     : {len(plan.probes)}  ({len(sources)} sources × {args.replicas} replicas)")
    for entry in plan.probes:
        print(f"  - {entry['source']:14s} replica={entry['replica']} -> {entry['name']}")
    return 0


def _cmd_calib_submit(args: argparse.Namespace, repo: Path) -> int:
    run_dir = _resolve(repo, args.runs) / args.run_id
    if not args.i_know_this_runs_real_jobs:
        print(
            "refusing to submit: pass --i-know-this-runs-real-jobs to "
            "acknowledge that this will apply Jobs to the live cluster. "
            "Probes are pinned and labelled role=background; cleanup uses "
            "the experiments-cleanup carve-out via `calibration cleanup`.",
            file=sys.stderr,
        )
        return 3
    return _calib_submit.submit_real(
        run_dir=run_dir,
        context=args.context,
        namespace=args.namespace,
        per_probe_deadline_seconds=args.per_probe_deadline_seconds,
        poll_seconds=args.poll_seconds,
        acknowledged=True,
    )


def _cmd_calib_reduce(args: argparse.Namespace, repo: Path) -> int:
    run_dir = _resolve(repo, args.runs) / args.run_id
    out_path = _calib_reduce.reduce_run(
        run_dir=run_dir,
        namespace=args.namespace,
        repo_root=repo,
        write_specs_yaml=not args.no_write_specs,
    )
    print(f"output: {out_path}")
    return 0


def _cmd_calib_cleanup(args: argparse.Namespace, repo: Path) -> int:
    del repo
    if not args.i_know_this_runs_real_jobs:
        print(
            "refusing to delete: pass --i-know-this-runs-real-jobs to "
            "acknowledge label-selector Job deletion in delphi-experiments.",
            file=sys.stderr,
        )
        return 3
    return _calib_submit.cleanup_run(
        run_id=args.run_id,
        context=args.context,
        namespace=args.namespace,
        acknowledged=True,
    )


def _cmd_calibration(args: argparse.Namespace, repo: Path) -> int:
    """Dispatcher for the `calibration <action>` subcommand group."""
    handler = {
        "render": _cmd_calib_render,
        "submit": _cmd_calib_submit,
        "reduce": _cmd_calib_reduce,
        "cleanup": _cmd_calib_cleanup,
    }.get(args.calib_action)
    if handler is None:
        print("calibration requires an action: render | submit | reduce | cleanup",
              file=sys.stderr)
        return 2
    return handler(args, repo)


def _cmd_populate_tex(args: argparse.Namespace, repo: Path) -> int:
    """iter-4g.1 composite populator.

    Walks each --run-dir's analysis/summary.json and merges them into
    one composite via ``tex_template.populate_composite``. Default
    target list mirrors the analyzer's ``--write-tex-defaults``:
    canonical content/06 + proposal 06 (when present).
    """
    summary_paths: list[Path] = []
    for d in args.run_dirs:
        rd = _resolve(repo, d)
        sp = rd / "analysis" / "summary.json"
        if not sp.exists():
            print(f"populate-tex: missing analysis/summary.json in {rd}",
                  file=sys.stderr)
            return 1
        summary_paths.append(sp)

    if args.tex_sources:
        tex_sources = [_resolve(repo, t) for t in args.tex_sources]
    else:
        defaults = [
            _resolve(repo, "docs/draft-paper-latex/content/06_EvaluationResults.tex"),
            _resolve(
                repo,
                "docs/draft-paper-latex/proposals/evaluation-alignment-2026-05-22/"
                "06_EvaluationResults.tex",
            ),
        ]
        tex_sources = [t for t in defaults if t.exists()]
        if not tex_sources:
            print("populate-tex: no --tex passed and no default .tex sources exist",
                  file=sys.stderr)
            return 1

    for tex in tex_sources:
        out = _tex.populate_composite(tex, summary_paths)
        print(f"wrote {out}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="experiments.orchestrator")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("render", help="Render a spec into per-Job YAMLs and emit a planned manifest.")
    pr.add_argument("--spec", required=True, help="Path to the experiment spec YAML.")
    pr.add_argument("--run-id", default=None, help="Run identifier; auto-generated if omitted.")
    pr.add_argument("--baseline", default="DELPHI-Full",
                    choices=["Static-Karmada", "Heuristic-Scorer", "DELPHI-Full"])
    pr.add_argument("--registry", default=_DEFAULT_REGISTRY)
    pr.add_argument("--templates", default=_DEFAULT_TEMPLATES)
    pr.add_argument("--runs", default=_DEFAULT_RUNS)
    # iter-4h-2 multistate render: when set, switches to render_multistate.
    pr.add_argument(
        "--multistate-states", default=None,
        help="Comma-separated list of cluster-state ids "
             "(e.g. idle,mixed-disturbance-light). When set, the renderer "
             "expands the foreground spec across the named states and "
             "writes states/<state>/{foreground,disturbance,background} "
             "subdirs plus a multistate.json manifest. iter-4h-2.")
    pr.add_argument(
        "--cluster-states-dir", default=_DEFAULT_CLUSTER_STATES,
        help="Cluster-state spec directory (consumed when "
             "--multistate-states is set).")
    pr.add_argument(
        "--dry-spawn", action="store_true",
        help="Render YAMLs and timelines but skip every kubectl apply "
             "(no cluster contact). Useful to inspect the planned "
             "disturbance schedule before a real submit. iter-4h-2 § 7.1.")
    pr.add_argument(
        "--cordon-window", default=None,
        help="Path to a cordon-window.yaml. When set (single-spec render "
             "only), the planned window is recorded in manifest.json so "
             "submit_real drives the timed karmadactl cordon/uncordon. "
             "iter-4h-4.")
    pr.set_defaults(func=_cmd_render)

    pd = sub.add_parser("dry-run", help="Server-side dry-run every rendered Job for a run-id.")
    pd.add_argument("--run-id", required=True)
    pd.add_argument("--context", default="unique-logical-entrypoint")
    pd.add_argument("--runs", default=_DEFAULT_RUNS)
    pd.set_defaults(func=_cmd_dry_run)

    pp = sub.add_parser("prep", help="Render the background-load Jobs from a cluster-state spec.")
    pp.add_argument("--cluster-state", required=True,
                    help="Path to a cluster-state YAML (e.g. experiments/specs/cluster-states/idle.yaml).")
    pp.add_argument("--run-id", default=None)
    pp.add_argument("--namespace", default="delphi-experiments")
    pp.add_argument("--templates", default=_DEFAULT_TEMPLATES)
    pp.add_argument("--runs", default=_DEFAULT_RUNS)
    pp.set_defaults(func=_cmd_prep)

    pb = sub.add_parser("render-bootstrap",
                        help="Expand a Phase-A bootstrap spec (coverage criterion) into per-state jobs.")
    pb.add_argument("--spec", required=True, help="Path to a bootstrap spec YAML.")
    pb.add_argument("--run-id", default=None)
    pb.add_argument("--registry", default=_DEFAULT_REGISTRY)
    pb.add_argument("--templates", default=_DEFAULT_TEMPLATES)
    pb.add_argument("--runs", default=_DEFAULT_RUNS)
    pb.add_argument("--cluster-states-dir", default=_DEFAULT_CLUSTER_STATES)
    pb.set_defaults(func=_cmd_render_bootstrap)

    ps = sub.add_parser(
        "submit",
        help="Apply a rendered run to the cluster. Operator-only; requires "
             "--i-know-this-runs-real-jobs.")
    ps.add_argument("--run-id", required=True)
    ps.add_argument("--context", default="unique-logical-entrypoint")
    ps.add_argument("--namespace", default="delphi-experiments")
    ps.add_argument("--runs", default=_DEFAULT_RUNS)
    ps.add_argument("--cluster-states-dir", default=_DEFAULT_CLUSTER_STATES)
    ps.add_argument("--fg-deadline-seconds", type=int, default=1800,
                    help="Per-state wait deadline for foreground completion. "
                         "Increase for long-running stress jobs.")
    ps.add_argument("--poll-seconds", type=int, default=10)
    ps.add_argument("--keep-background", action="store_true",
                    help="Skip kubectl delete of background-load Jobs at "
                         "end-of-state. Use only when intentionally leaving "
                         "load for inspection; the operator must clean up "
                         "by hand afterwards.")
    ps.add_argument("--keep-disturbance", action="store_true",
                    help="iter-4h-2 multistate synonym of --keep-background. "
                         "Skips end-of-state bulk-delete of pinned "
                         "disturbance Jobs (and single-pin background Jobs "
                         "when present). Both flags are accepted; the "
                         "multistate code path honours either.")
    ps.add_argument("--stall-threshold-seconds", type=int, default=120,
                    help="Foreground wait loop emits a STALL probe (events + "
                         "pod-counter snapshot) when a Job has been Active "
                         "for this many seconds without a terminal "
                         "transition. Set to 0 to disable the probe. Default "
                         "120s, which catches ImagePullBackOff well before "
                         "BackoffLimitExceeded rolls up to Failed.")
    ps.add_argument("--fail-fast", action="store_true",
                    help="Abort the wait loop as soon as the first Job "
                         "reaches Failed. Survivors are reported as "
                         "TimedOut. Opt-in; default keeps the original "
                         "behaviour of draining all jobs to a terminal "
                         "state or the deadline.")
    ps.add_argument("--i-know-this-runs-real-jobs",
                    dest="i_know_this_runs_real_jobs",
                    action="store_true",
                    help="Required safety gate. Without it, submit refuses to "
                         "touch the cluster.")
    ps.set_defaults(func=_cmd_submit)

    pc = sub.add_parser(
        "collect",
        help="Update manifest.json with per-job applied_at_utc + "
             "submitted_at / finished_at from submission-log.csv, and "
             "mirror placements.json / job-status.json into "
             "assigned_cluster / completed_at. Runs automatically at the "
             "end of `submit` (which also captures both artifacts); rerun "
             "by hand when iterating on the manifest without "
             "re-submitting. No cluster contact unless "
             "--capture-kube-records is given.")
    pc.add_argument("--run-id", required=True)
    pc.add_argument("--runs", default=_DEFAULT_RUNS)
    pc.add_argument("--capture-kube-records", dest="capture_kube_records",
                    action="store_true",
                    help="Also read each applied foreground Job's "
                         "PropagationPolicy and aggregated Job.status "
                         "(read-only kubectl get) into runs/<id>/"
                         "placements.json and job-status.json before "
                         "updating the manifest. `submit` does this "
                         "automatically; use it to backfill a run whose "
                         "Jobs still exist. Never replaces an "
                         "already-recorded value; fills missing ones.")
    pc.add_argument("--context", default="unique-logical-entrypoint")
    pc.add_argument("--namespace", default="delphi-experiments")
    pc.set_defaults(func=_cmd_collect)

    # iter-4b: RTT calibration. `calibration <action>` keeps the four
    # actions (render / submit / reduce / cleanup) under one umbrella so
    # the top-level CLI surface stays uncluttered.
    pcal = sub.add_parser(
        "calibration",
        help="RTT calibration: render + submit pinned iperf3 probes, "
             "reduce them into experiments/specs/cluster-rtt-measured.yaml.")
    pcal_sub = pcal.add_subparsers(dest="calib_action", required=True)

    pcr = pcal_sub.add_parser(
        "render",
        help="Render 12 pinned iperf3 probe Jobs into a run directory.")
    pcr.add_argument("--run-id", default=None,
                     help="Calibration run id; auto-generated as calibration-<ts> if omitted.")
    pcr.add_argument("--target-host", required=True,
                     help="On-prem node IP (discover via "
                          "`kubectl --context on-prem get nodes -o wide`).")
    pcr.add_argument("--target", default=_calib_render.DEFAULT_TARGET,
                     help="Destination cluster label recorded in the probe "
                          "annotations and the reduce output (default: on-prem).")
    pcr.add_argument("--target-port", type=int,
                     default=_calib_render.DEFAULT_TARGET_PORT,
                     help="NodePort exposing iperf3-server on on-prem.")
    pcr.add_argument("--sources", nargs="+", default=None,
                     help="Source clusters; default = on-prem public-cloud edge-1 edge-2.")
    pcr.add_argument("--replicas", type=int,
                     default=_calib_render.DEFAULT_REPLICAS,
                     help="Probes per (source, target) pair (default: 3).")
    pcr.add_argument("--duration-seconds", type=int,
                     default=_calib_render.DEFAULT_DURATION_SECONDS,
                     help="iperf3 -t value, per probe (default: 5).")
    pcr.add_argument("--namespace", default=_calib_render.DEFAULT_NAMESPACE)
    pcr.add_argument("--image", default=_calib_render.DEFAULT_IMAGE,
                     help="Container image carrying an iperf3 binary. "
                          "Default is the multi-arch (linux/amd64, "
                          "linux/arm64) iperf3-server build; the python "
                          "entrypoint is overridden inside the Job spec.")
    pcr.add_argument("--templates", default=_DEFAULT_CALIBRATION_TEMPLATES,
                     help="Directory containing rtt-probe.yaml.j2.")
    pcr.add_argument("--runs", default=_DEFAULT_RUNS)
    pcr.set_defaults(func=_cmd_calibration)

    pcs = pcal_sub.add_parser(
        "submit",
        help="Apply rendered probes (gated). Operator-only.")
    pcs.add_argument("--run-id", required=True)
    pcs.add_argument("--context", default="unique-logical-entrypoint")
    pcs.add_argument("--namespace", default=_calib_render.DEFAULT_NAMESPACE)
    pcs.add_argument("--per-probe-deadline-seconds", type=int, default=120)
    pcs.add_argument("--poll-seconds", type=int, default=5)
    pcs.add_argument("--runs", default=_DEFAULT_RUNS)
    pcs.add_argument("--i-know-this-runs-real-jobs",
                     dest="i_know_this_runs_real_jobs",
                     action="store_true",
                     help="Required safety gate.")
    pcs.set_defaults(func=_cmd_calibration)

    pcd = pcal_sub.add_parser(
        "reduce",
        help="Fetch probe logs (read-only on member contexts), aggregate "
             "p50/p95, write experiments/specs/cluster-rtt-measured.yaml.")
    pcd.add_argument("--run-id", required=True)
    pcd.add_argument("--namespace", default=_calib_render.DEFAULT_NAMESPACE)
    pcd.add_argument("--no-write-specs", action="store_true",
                     help="Skip writing cluster-rtt-measured.yaml; "
                          "only emit <run-dir>/samples.json.")
    pcd.add_argument("--runs", default=_DEFAULT_RUNS)
    pcd.set_defaults(func=_cmd_calibration)

    pcl = pcal_sub.add_parser(
        "cleanup",
        help="Single label-selector delete of the calibration run's Jobs "
             "(uses the experiments-cleanup carve-out).")
    pcl.add_argument("--run-id", required=True)
    pcl.add_argument("--context", default="unique-logical-entrypoint")
    pcl.add_argument("--namespace", default=_calib_render.DEFAULT_NAMESPACE)
    pcl.add_argument("--i-know-this-runs-real-jobs",
                     dest="i_know_this_runs_real_jobs",
                     action="store_true")
    pcl.set_defaults(func=_cmd_calibration)

    # iter-4a: evaluation analyzer. Delegates the subparser wiring to
    # the analysis subpackage's own argparse module so the surface
    # stays in one place.
    _analysis_cli.add_parser(sub)

    # iter-4g: Section 6 plot generators. Top-level `plots <subcommand>`
    # to keep the existing `analysis --run-dir ...` reducer untouched.
    _plots_cli.add_parser(sub)

    # iter-4g.1: composite tex populator. Takes N --run-dir flags
    # (typically the three baseline run-dirs) and emits one populated
    # .tex that has every `_static` / `_heuristic` / `_full` cell
    # filled in a single pass — replaces the previous "run analyzer
    # three times with --write-tex-defaults and let the last writer
    # win" workflow.
    pt = sub.add_parser(
        "populate-tex",
        help="Populate a Section 6 .tex source from N summary.json files "
             "(one per baseline) in a single pass. Writes "
             "<source>.populated.tex alongside the source.",
    )
    pt.add_argument(
        "--run-dir", action="append", required=True, dest="run_dirs",
        help="Path to a run-dir whose analysis/summary.json should be "
             "merged into the composite. Pass once per baseline.",
    )
    pt.add_argument(
        "--tex", action="append", dest="tex_sources",
        default=None,
        help="Source .tex path. Repeatable. If omitted, defaults to "
             "both content/06_EvaluationResults.tex and the proposal "
             "06_EvaluationResults.tex when present.",
    )
    pt.set_defaults(func=_cmd_populate_tex)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo = _find_repo_root(Path(__file__).parent)
    return args.func(args, repo)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
