"""Top-level composer for one analysis pass.

Reads the run-dir (including the collection-time ``placements.json`` /
``job-status.json`` when present) + cluster-registry + (optionally) DB + (optionally)
kubectl. Computes every metric. Validates no-pollution. Writes the
deterministic outputs.

Idempotency: running this function twice with the same inputs and the
same cluster/DB state produces a byte-identical analysis/ directory.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import kube as _kube
from . import loaders as _loaders
from . import registry_diff as _registry_diff
from . import tex_template as _tex
from . import validators as _val
from . import writers as _writers
from .db import AnalyzerDB, StubDB
from .models import AnalysisContext, JobRecord, MetricResult, RunArtifacts, Summary
from .metrics import (
    compute_admission_overhead,
    compute_completion_decomposition,
    compute_completion_time,
    compute_cordon_response,
    compute_cost_proxy,
    compute_data_movement,
    compute_decision_latency,
    compute_intent_compliance,
    compute_placement_success,
    compute_telemetry_overhead,
    compute_traceability,
)


_METRIC_PIPELINE: tuple[tuple[str, Any], ...] = (
    ("placement_success", compute_placement_success),
    ("completion_time", compute_completion_time),
    ("completion_decomposition", compute_completion_decomposition),
    ("decision_latency", compute_decision_latency),
    ("admission_overhead", compute_admission_overhead),
    ("telemetry_overhead", compute_telemetry_overhead),
    ("intent_compliance", compute_intent_compliance),
    ("cost_proxy", compute_cost_proxy),
    ("data_movement", compute_data_movement),
    ("traceability", compute_traceability),
)


def _parse_utc(s: Optional[str]) -> Optional[datetime]:
    return _loaders._parse_utc(s) if s else None


def _enrich_with_kube(jobs: list[JobRecord], ctx: AnalysisContext) -> list[JobRecord]:
    """Layer live Job + PropagationPolicy state onto the loaded records.

    Placement precedence: a placement persisted at collection time
    (``placements.json`` / ``manifest.json.assigned_cluster``, loaded into
    ``persisted_cluster``) wins over the live PP lookup, together with the
    PP provenance recorded alongside it. The live PP can only differ if it
    was re-materialised after the job ran, and preferring the collection-
    time record keeps the result independent of *when* analysis runs. The
    live value is kept in ``live_cluster`` so the runner can report any
    disagreement. Without a persisted placement this is exactly the
    pre-existing behaviour (live target, live PP provenance).

    Job.status timestamps follow the same rule per field: a persisted
    ``start_time`` / ``completion_time`` (``job-status.json``) wins, a
    null one falls back to the live value (e.g. a job that completed
    after the submit tail captured it), and ``live_*`` keeps the live
    reading for the disagreement report. ``completed`` is upgraded from
    the effective completion time, as it was from the live one before.
    """
    out: list[JobRecord] = []
    for j in jobs:
        if not j.applied:
            out.append(j)
            continue
        job_doc = _kube.get_job(ctx.aggregator_context, ctx.aggregator_namespace, j.name)
        pp_doc = _kube.get_propagation_policy(ctx.aggregator_context, ctx.aggregator_namespace, j.name)
        live_start = _kube.job_start_time(job_doc)
        live_completion = _kube.job_completion_time(job_doc)
        k8s_start_time = j.persisted_start_time or live_start or j.k8s_start_time
        k8s_completion_time = j.persisted_completion_time or live_completion or j.k8s_completion_time
        completed = bool(j.completed) or k8s_completion_time is not None
        live_cluster = _kube.selected_cluster_from_pp(pp_doc)
        if j.persisted_cluster:
            selected = j.persisted_cluster
            pp_generation = j.pp_generation or _kube.pp_generation(pp_doc)
            pp_created_at = j.pp_created_at or _kube.pp_creation_time(pp_doc)
        else:
            selected = live_cluster or j.selected_cluster
            pp_generation = _kube.pp_generation(pp_doc)
            pp_created_at = _kube.pp_creation_time(pp_doc) or j.pp_created_at
        out.append(replace(
            j,
            selected_cluster=selected,
            live_cluster=live_cluster,
            k8s_completion_time=k8s_completion_time,
            k8s_start_time=k8s_start_time,
            live_start_time=live_start,
            live_completion_time=live_completion,
            pp_generation=pp_generation,
            pp_created_at=pp_created_at,
            completed=completed,
        ))
    return out


def _persisted_disagreement_notes(jobs: Iterable[JobRecord]) -> list[str]:
    """One note per (job, field) whose persisted and live values differ.

    Covers the placement and the two Job.status timestamps. Sorted by
    job name (fields in a fixed order) so summary.json stays
    byte-idempotent. The persisted value is the one scored (see
    ``_enrich_with_kube``).
    """
    def _iso(v: Any) -> str:
        return v.isoformat() if isinstance(v, datetime) else str(v)

    notes: list[str] = []
    for j in sorted(jobs, key=lambda r: r.name):
        if j.persisted_cluster and j.live_cluster and j.persisted_cluster != j.live_cluster:
            notes.append(
                f"placement disagreement for {j.name}: persisted={j.persisted_cluster} "
                f"live={j.live_cluster}; scored the persisted (collection-time) value"
            )
        for field, persisted, live in (
            ("start_time", j.persisted_start_time, j.live_start_time),
            ("completion_time", j.persisted_completion_time, j.live_completion_time),
        ):
            if persisted and live and persisted != live:
                notes.append(
                    f"job-status {field} disagreement for {j.name}: persisted={_iso(persisted)} "
                    f"live={_iso(live)}; scored the persisted (collection-time) value"
                )
    return notes


def _enrich_with_db(jobs: list[JobRecord], db: Any) -> list[JobRecord]:
    foreground_names = [j.name for j in jobs if j.role == "foreground"]
    if not foreground_names:
        return jobs

    rows = db.run_query("request_for_jobs", foreground_names)
    by_name = {row["job_name"]: row for row in rows}

    out: list[JobRecord] = []
    for j in jobs:
        row = by_name.get(j.name)
        if row is None:
            out.append(j)
            continue
        out.append(replace(
            j,
            db_request_id=str(row.get("id")) if row.get("id") is not None else None,
            db_status=str(row.get("status") or ""),
            db_kind=str(row.get("kind") or ""),
            db_created_at=row.get("created_at"),
            db_last_attempt_at=row.get("last_attempt_at"),
            db_clusters_score=row.get("clusters_score"),
            db_reason=str(row.get("reason") or "") or None,
        ))
    return out


def _build_baseline_summary(results: dict[str, MetricResult]) -> dict[str, Any]:
    """Shape the per-baseline projection that summary.json carries.

    Includes both the aggregate dicts (for the populator's nested
    lookups) and a few convenience derived keys like
    ``most_frequent_selected_cluster`` for the proposal/06
    ``class_*`` placeholders.
    """
    projection: dict[str, Any] = {}
    for name, result in results.items():
        projection[name] = dict(result.aggregate) if result.aggregate else {}
        projection[name]["status"] = result.status
        if result.note:
            projection[name]["note"] = result.note

    # Derived: most-frequent selected cluster (proposal/06 class_<baseline>).
    traceability = results.get("traceability")
    if traceability and traceability.rows:
        from collections import Counter
        counts = Counter(
            r.get("selected_cluster", "")
            for r in traceability.rows
            if r.get("selected_cluster")
        )
        if counts:
            projection["most_frequent_selected_cluster"] = counts.most_common(1)[0][0]
    return projection


def _resolve_window(manifest: dict[str, Any]) -> tuple[Optional[datetime], datetime]:
    start = _parse_utc(manifest.get("submitted_at"))
    end = _parse_utc(manifest.get("finished_at")) or datetime.now(timezone.utc)
    return start, end


def run_analysis(
    *,
    run_dir: Path,
    registry_path: Path,
    registry_variant_path: Optional[Path] = None,
    baseline: Optional[str] = None,
    db: Optional[Any] = None,
    use_db: bool = True,
    use_kube: bool = True,
    skip_pollution_check: bool = False,
    write_tex: Optional[Iterable[Path]] = None,
    aggregator_context: str = "unique-logical-entrypoint",
    aggregator_namespace: str = "delphi-experiments",
) -> int:
    """Run the analyzer end-to-end.

    Returns an exit code suitable for the CLI:
      0 - ok
      1 - input error
      2 - pollution detected
      3 - DB error (and --no-db not set)
      4 - tex populator error
    """
    if not run_dir.exists():
        sys.stderr.write(f"run-dir not found: {run_dir}\n")
        return 1

    notes: list[str] = []

    # Load file-only data (including any persisted placements.json).
    try:
        jobs, manifest = _loaders.build_job_records(run_dir)
    except ValueError as exc:
        sys.stderr.write(f"input error: {exc}\n")
        return 1
    registry = _loaders.load_registry(registry_path)
    layout = _loaders.detect_layout(run_dir)
    start_utc, end_utc = _resolve_window(manifest)

    # Effective baseline: CLI override → manifest → "unknown".
    eff_baseline = baseline or str(manifest.get("baseline") or "unknown")
    if eff_baseline not in (
        "static-karmada", "heuristic-scorer", "delphi-full",
        "DELPHI-Full", "Static-Karmada", "Heuristic-Scorer",
    ):
        notes.append(f"non-standard baseline label {eff_baseline!r}")
    # Normalise the canonical 3 names we use as JSON keys.
    eff_key = {
        "DELPHI-Full": "delphi-full",
        "Static-Karmada": "static-karmada",
        "Heuristic-Scorer": "heuristic-scorer",
    }.get(eff_baseline, eff_baseline)

    ctx = AnalysisContext(
        use_db=use_db,
        use_kube=use_kube,
        skip_pollution_check=skip_pollution_check,
        aggregator_context=aggregator_context,
        aggregator_namespace=aggregator_namespace,
    )

    # Enrich.
    if use_kube:
        try:
            jobs = _enrich_with_kube(jobs, ctx)
        except Exception as exc:  # pragma: no cover - defensive
            notes.append(f"kubectl enrichment failed: {exc}")

    actual_db = db
    if use_db and actual_db is None:
        try:
            actual_db = AnalyzerDB(
                queries_path=Path(__file__).parent / "queries.sql",
                env_file=run_dir.parents[1] / "db" / ".env",
                auto_portforward=True,
                repo_root=run_dir.parents[2] if len(run_dir.parents) >= 3 else None,
            )
        except RuntimeError as exc:
            notes.append(f"psycopg unavailable: {exc}")
            actual_db = StubDB()
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write(f"DB connection error: {exc}\n")
            return 3

    if actual_db is not None and use_db:
        try:
            jobs = _enrich_with_db(jobs, actual_db)
        except Exception as exc:
            sys.stderr.write(f"DB query failed: {exc}\n")
            return 3

    artifacts = RunArtifacts(
        run_id=str(manifest.get("run_id") or run_dir.name),
        run_dir=run_dir,
        baseline=eff_key,
        layout=layout,
        submitted_at=start_utc,
        finished_at=end_utc if manifest.get("finished_at") else None,
        jobs=tuple(jobs),
        registry=registry,
        notes=tuple(notes),
    )

    # Persisted vs live disagreements — placement and Job.status
    # timestamps (only possible when both a persisted record and a live
    # object exist). Logged loudly; appended
    # after RunArtifacts so each note lands in summary.json exactly once.
    for note in _persisted_disagreement_notes(jobs):
        sys.stderr.write(f"WARN: {note}\n")
        notes.append(note)

    # No-pollution invariant.
    no_pollution_ok: Optional[bool] = None
    pollution_rows: list[dict[str, Any]] = []
    if actual_db is not None and not skip_pollution_check:
        fg_names = [j.name for j in jobs if j.role == "foreground"]
        bg_names = [j.name for j in jobs if j.role == "background"]
        try:
            ok, _ = _val.assert_no_pollution(
                db=actual_db,
                foreground_job_names=fg_names,
                background_job_names=bg_names,
            )
            no_pollution_ok = ok
        except _val.PollutionDetected as pol:
            pollution_rows = list(pol.forward_leaked) + list(pol.reverse_leaked)
            no_pollution_ok = False
    elif skip_pollution_check:
        notes.append("pollution check explicitly skipped (--skip-pollution-check)")

    # Compute metrics. The cordon_response metric (iter-4h-4) is only
    # added when the run carries an orchestrator-written
    # cordon-timestamps.yaml, so non-cordon runs are byte-identical (no
    # new CSV, no new summary key) and the existing idempotency fixtures
    # are untouched.
    pipeline = list(_METRIC_PIPELINE)
    if (run_dir / "cordon-timestamps.yaml").exists():
        pipeline.append(("cordon_response", compute_cordon_response))

    results: dict[str, MetricResult] = {}
    for name, fn in pipeline:
        results[name] = fn(artifacts, ctx)

    # Join coverage assertion.
    db_rows = sum(1 for j in jobs if j.role == "foreground" and j.db_request_id)
    fg_count = sum(1 for j in jobs if j.role == "foreground" and j.applied)
    join_ok = _val.assert_join_coverage(
        foreground_count=fg_count,
        db_request_count=db_rows,
    ) if use_db else None
    if join_ok is False:
        notes.append(f"join coverage: {db_rows} DB rows for {fg_count} applied foreground")

    # Compose summary and write outputs.
    analysis_dir = run_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    baseline_proj = _build_baseline_summary(results)
    # iter-4h-3: registry-degradation provenance. Describes how the
    # ACTIVE variant differed from the REFERENCE (ground-truth) registry
    # — which is what intent_compliance scored against. Deterministic
    # (sorted cells) so idempotency holds; additive (legacy runs get a
    # "full"/empty diff). See registry_diff.py and iter-4h-3 plan § 6.2.
    baseline_proj["registry_diff"] = _registry_diff.compute_registry_diff(
        reference=registry,
        reference_path=registry_path,
        active_path=registry_variant_path,
        manifest=manifest,
    )
    summary = Summary(
        run_id=artifacts.run_id,
        baseline=eff_key,
        no_pollution_ok=no_pollution_ok,
        join_coverage_ok=join_ok,
        submitted_at=artifacts.submitted_at,
        finished_at=artifacts.finished_at,
        baselines={eff_key: baseline_proj},
        notes=tuple(list(artifacts.notes) + notes),
    )

    _writers.write_metric_csvs(analysis_dir, results, pollution_rows)
    summary_path = _writers.write_summary(analysis_dir, summary)

    # tex_template populator.
    if write_tex:
        for tex_src in write_tex:
            tex_src = Path(tex_src)
            if not tex_src.exists():
                sys.stderr.write(f"tex source not found: {tex_src}\n")
                return 4
            try:
                _tex.populate(tex_src, summary_path)
            except Exception as exc:  # pragma: no cover
                sys.stderr.write(f"tex populator failed for {tex_src}: {exc}\n")
                return 4

    if actual_db is not None:
        try:
            actual_db.close()
        except Exception:
            pass

    # Final exit code: pollution = 2, everything else 0.
    if no_pollution_ok is False:
        return 2
    return 0
