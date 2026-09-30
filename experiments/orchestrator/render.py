"""Spec → list of rendered Job YAML files.

Pure function world: read YAML, expand distribution into per-job
records (with deterministic ordering from random_seed), render each
through the appropriate Jinja template, write to
runs/<run-id>/jobs/*.yaml. Also emit timeline-planned.csv expressing
the planned submission schedule from the temporal_profile.
"""
from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

_VALID_PATTERNS = {"uniform", "poisson", "burst"}
_VALID_CLASSES = {"cpu", "memory", "network"}

# Keys the templates expect in the `images` ctx variable. The
# prepare-images.sh script writes these into experiments/runs/<run-id>/
# images.json.
_REQUIRED_IMAGE_KEYS = ("stress_ng", "net_stresser")


def _load_images(run_output_dir: Path) -> dict[str, str]:
    """Load the per-experiment image manifest produced by
    experiments/runtime/prepare-images.sh.

    The renderer expects an immutable per-experiment image tag (the
    run-id) so the Kubernetes default imagePullPolicy=IfNotPresent
    gives the cache-once-per-node behaviour a real steady-state system
    would see. See iter-4c.2 and the docstring of prepare-images.sh.

    Failure mode: the file is missing or malformed → raise
    FileNotFoundError / KeyError with an operator-actionable message
    pointing at prepare-images.sh. We deliberately do NOT fall back to
    a hardcoded :dev default, because that silently re-introduces the
    mutable-tag drift that iter-4c.2 is designed to eliminate.
    """
    manifest_path = run_output_dir / "images.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"image manifest not found at {manifest_path}.\n"
            f"Run `bash experiments/runtime/prepare-images.sh "
            f"--run-id <id> --mode {{build|retag}}` before render. "
            f"See iter-4c operator steps in experiments/RUNBOOK.md."
        )
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{manifest_path} is not valid JSON: {exc}"
        ) from exc
    missing = [k for k in _REQUIRED_IMAGE_KEYS if k not in data]
    if missing:
        raise KeyError(
            f"{manifest_path} is missing required keys: {missing}. "
            f"Re-run prepare-images.sh to regenerate."
        )
    # Only the resolved references go into the Jinja context — extra
    # metadata (mode, source_tag, prepared_at_utc, ...) stays in the
    # manifest for audit but does not need to reach the template.
    return {k: data[k] for k in _REQUIRED_IMAGE_KEYS}


@dataclass(frozen=True)
class RenderedJob:
    name: str
    path: Path
    workload_class: str
    intent_profile: str
    planned_submit_offset_seconds: float


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _normalised_distribution(distribution: list[dict[str, Any]],
                             total_jobs: int,
                             rng: random.Random) -> list[tuple[str, str]]:
    """Resolve (workload_class, intent_profile) for every job index.

    Weight rounding uses largest-remainder so the sum equals total_jobs
    exactly. RNG is consumed for tie-breaking on equal remainders.
    """
    weight_sum = sum(d["weight"] for d in distribution)
    if weight_sum <= 0:
        raise ValueError("distribution weights must sum > 0")

    raw_counts = []
    for d in distribution:
        wc = d["workload_class"]
        ip = d["intent"]
        if wc not in _VALID_CLASSES:
            raise ValueError(f"unknown workload_class={wc!r}")
        share = total_jobs * d["weight"] / weight_sum
        raw_counts.append((wc, ip, share))

    floors = [(wc, ip, int(math.floor(s)), s - math.floor(s))
              for (wc, ip, s) in raw_counts]
    assigned = sum(f[2] for f in floors)
    remainder = total_jobs - assigned

    # Distribute the residue to the largest fractional parts; break ties via RNG.
    indexed = list(enumerate(floors))
    indexed.sort(key=lambda kv: (-kv[1][3], rng.random()))
    counts = [f[2] for f in floors]
    for i in range(remainder):
        counts[indexed[i][0]] += 1

    jobs: list[tuple[str, str]] = []
    for entry, n in zip(raw_counts, counts):
        wc, ip = entry[0], entry[1]
        jobs.extend([(wc, ip)] * n)

    rng.shuffle(jobs)
    return jobs


def _arrival_offsets(temporal: dict[str, Any],
                     n_jobs: int,
                     rng: random.Random) -> list[float]:
    """Return n_jobs absolute submit offsets (seconds since t0)."""
    pattern = temporal["pattern"]
    if pattern not in _VALID_PATTERNS:
        raise ValueError(f"unknown temporal_profile.pattern={pattern!r}")

    mean = float(temporal["mean_inter_arrival_seconds"])
    jitter = float(temporal.get("jitter_seconds") or 0.0)

    offsets: list[float] = []
    t = 0.0
    if pattern == "uniform":
        for _ in range(n_jobs):
            delta = mean + rng.uniform(-jitter, jitter)
            t += max(0.0, delta)
            offsets.append(t)
    elif pattern == "poisson":
        rate = 1.0 / mean if mean > 0 else 0.0
        for _ in range(n_jobs):
            inter = rng.expovariate(rate) if rate > 0 else 0.0
            inter += rng.uniform(-jitter, jitter)
            t += max(0.0, inter)
            offsets.append(t)
    elif pattern == "burst":
        burst = int(temporal["burst_size"])
        rest = float(temporal["rest_seconds"])
        if burst <= 0:
            raise ValueError("burst_size must be > 0")
        remaining = n_jobs
        while remaining > 0:
            this_burst = min(burst, remaining)
            for _ in range(this_burst):
                delta = jitter * rng.random()
                t += max(0.0, delta)
                offsets.append(t)
            remaining -= this_burst
            if remaining > 0:
                t += rest
    return offsets


def _propagation_requirements_json(registry: dict[str, Any],
                                   intent_profile: str) -> str:
    profiles = registry.get("intent_profiles", {})
    if intent_profile not in profiles:
        raise ValueError(f"intent_profile {intent_profile!r} not in cluster-registry")
    return json.dumps(profiles[intent_profile]["propagation_requirements"])


def _job_name(spec_id: str, run_id: str, idx: int, wc: str) -> str:
    # Kubernetes name constraint: <= 63 chars, DNS-1123 label.
    # When run_id already starts with spec_id- (the _auto_run_id case),
    # skip the redundant spec_id prefix; otherwise the duplicated stem
    # eats budget and the per-job idx falls off the end on truncation,
    # collapsing several jobs onto the same Kubernetes Job name.
    # idx is placed before wc so that if truncation still kicks in, the
    # cosmetic workload-class tail is dropped instead of the index.
    if run_id == spec_id or run_id.startswith(f"{spec_id}-"):
        base = f"delphi-{run_id}-{idx:03d}-{wc}"
    else:
        base = f"delphi-{spec_id}-{run_id}-{idx:03d}-{wc}"
    if len(base) > 63:
        base = base[:63].rstrip("-")
    return base.lower()


def render(spec_path: Path,
           registry_path: Path,
           run_id: str,
           templates_dir: Path,
           output_dir: Path) -> list[RenderedJob]:
    spec = _load_yaml(spec_path)
    registry = _load_yaml(registry_path)
    rng = random.Random(spec.get("random_seed", 0))

    distribution = spec["distribution"]
    total_jobs = int(spec["total_jobs"])
    namespace = spec["namespace"]
    parameters = spec["parameters"]
    resources = spec["resources"]
    temporal = spec["temporal_profile"]

    assignments = _normalised_distribution(distribution, total_jobs, rng)
    offsets = _arrival_offsets(temporal, total_jobs, rng)

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = output_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    # iter-4c.2: images.json is the per-experiment image manifest
    # produced by prepare-images.sh. Required (see _load_images for
    # rationale).
    images = _load_images(output_dir)

    rendered: list[RenderedJob] = []
    for idx, ((wc, ip), offset) in enumerate(zip(assignments, offsets)):
        template_name = f"job-{wc}.yaml.j2"
        tmpl = env.get_template(template_name)
        name = _job_name(spec["spec_id"], run_id, idx, wc)
        ctx = {
            "job_name": name,
            "namespace": namespace,
            "spec_id": spec["spec_id"],
            "run_id": run_id,
            "state_id": spec.get("cluster_state", ""),
            "intent_profile": ip,
            "propagation_requirements_json": _propagation_requirements_json(registry, ip),
            "parameters": parameters[wc],
            "resources": resources[wc],
            "images": images,
        }
        rendered_yaml = tmpl.render(**ctx)
        out_path = jobs_dir / f"{idx:03d}-{name}.yaml"
        out_path.write_text(rendered_yaml, encoding="utf-8")
        rendered.append(RenderedJob(
            name=name,
            path=out_path,
            workload_class=wc,
            intent_profile=ip,
            planned_submit_offset_seconds=offset,
        ))

    _write_timeline(output_dir / "timeline-planned.csv", rendered)
    return rendered


def _write_timeline(path: Path, jobs: list[RenderedJob]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "job_index", "job_name",
            "planned_submit_offset_seconds",
            "workload_class", "intent_profile",
        ])
        for idx, j in enumerate(jobs):
            writer.writerow([
                idx, j.name,
                f"{j.planned_submit_offset_seconds:.3f}",
                j.workload_class, j.intent_profile,
            ])


def _coverage_to_assignments(coverage: dict[str, Any],
                             rng: random.Random) -> list[tuple[str, str]]:
    """Expand a coverage criterion (workload_classes * intent_profiles *
    cluster_states * k) into a deterministic list of (workload_class,
    intent_profile) pairs for a SINGLE cluster_state sub-batch.

    The cluster_states dimension is materialised by the caller (one sub-
    batch per state). Within a sub-batch, every (wc, intent) combination
    is represented exactly k times, then the list is shuffled with the
    provided RNG so foreground arrivals within a sub-batch are not
    block-ordered.
    """
    wcs = list(coverage["workload_classes"])
    intents = list(coverage["intent_profiles"])
    k = int(coverage["k"])
    if not wcs or not intents or k <= 0:
        raise ValueError("coverage must declare non-empty workload_classes, intent_profiles, and k>0")
    pairs: list[tuple[str, str]] = []
    for wc in wcs:
        if wc not in _VALID_CLASSES:
            raise ValueError(f"unknown workload_class in coverage: {wc!r}")
        for ip in intents:
            for _ in range(k):
                pairs.append((wc, ip))
    rng.shuffle(pairs)
    return pairs


def render_bootstrap(spec_path: Path,
                     registry_path: Path,
                     cluster_states_dir: Path,
                     run_id: str,
                     templates_dir: Path,
                     output_dir: Path) -> dict[str, Any]:
    """Render the Phase-A bootstrap suite from a coverage-style spec.

    For each cluster_state in `spec.coverage.cluster_states`:
      1. resolve the matching cluster-state spec file under
         `cluster_states_dir` (e.g. `idle.yaml`, `mixed-load.yaml`),
      2. render its background-load Jobs into
         `<output_dir>/states/<state_id>/background/`,
      3. expand the coverage criterion into a per-state list of (wc, intent)
         pairs,
      4. render the per-state foreground Jobs into
         `<output_dir>/states/<state_id>/foreground/` and emit a
         `timeline-planned.csv` per state.

    Returns a structured summary that the CLI converts into a top-level
    bootstrap manifest.
    """
    spec = _load_yaml(spec_path)
    if "coverage" not in spec:
        raise ValueError(f"{spec_path} is not a bootstrap spec (missing 'coverage')")

    rng = random.Random(spec.get("random_seed", 0))
    coverage = spec["coverage"]
    namespace = spec["namespace"]
    parameters = spec["parameters"]
    resources = spec["resources"]
    temporal = spec["temporal_profile"]

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    states_dir = output_dir / "states"
    states_dir.mkdir(parents=True, exist_ok=True)

    # iter-4c.2: images.json is the per-experiment image manifest
    # produced by prepare-images.sh. Required (see _load_images for
    # rationale). Loaded once and threaded through both the foreground
    # and the background (cluster_state) render paths.
    images = _load_images(output_dir)

    summary: dict[str, Any] = {
        "spec_id": spec["spec_id"],
        "run_id": run_id,
        "states": [],
        "total_foreground_jobs": 0,
        "total_background_jobs": 0,
    }

    # Local import avoids a circular import at module top-level if both
    # ever cross-reference in the future.
    from . import cluster_state as _cs

    for state_id in coverage["cluster_states"]:
        state_path = cluster_states_dir / f"{state_id}.yaml"
        if not state_path.exists():
            raise FileNotFoundError(
                f"cluster-state spec '{state_id}' not found at {state_path}"
            )
        state_out = states_dir / state_id
        state_out.mkdir(parents=True, exist_ok=True)

        bg_jobs = _cs.render_cluster_state(
            state_path=state_path,
            run_id=run_id,
            templates_dir=templates_dir,
            output_dir=state_out,
            namespace=namespace,
            spec_id_override=spec["spec_id"],
            images=images,
        )

        pairs = _coverage_to_assignments(coverage, rng)
        offsets = _arrival_offsets(temporal, len(pairs), rng)

        fg_dir = state_out / "foreground"
        fg_dir.mkdir(parents=True, exist_ok=True)
        fg_jobs: list[RenderedJob] = []
        for idx, ((wc, ip), offset) in enumerate(zip(pairs, offsets)):
            template_name = f"job-{wc}.yaml.j2"
            tmpl = env.get_template(template_name)
            # Kubernetes name constraint: <=63 chars, DNS-1123 label.
            # iter-4c fix: long state_ids (e.g. 'skewed-edge-1-saturated'
            # = 23 chars) push the workload_class + idx discriminator past
            # byte 63 when state_id sits before them in the name, and the
            # truncation then collapses all 36 per-state jobs onto a
            # handful of duplicated names (observed on attempt
            # phase-a-20260524T214332Z: only 3/36 of each skewed-* state
            # applied successfully). Mirror the iter-4a.1 _job_name fix
            # by placing idx + wc early so the cosmetic state_id tail is
            # dropped on truncation, not the discriminator. The cluster
            # state is still encoded in the `delphi.experiments/state-id`
            # K8s label (set by every stressor template), so filtering
            # and observability are preserved.
            if run_id == spec["spec_id"] or run_id.startswith(f"{spec['spec_id']}-"):
                base = f"delphi-{run_id}-{idx:03d}-{wc}-{state_id}".lower()
            else:
                base = f"delphi-{spec['spec_id']}-{run_id}-{idx:03d}-{wc}-{state_id}".lower()
            if len(base) > 63:
                base = base[:63].rstrip("-")
            ctx = {
                "job_name": base,
                "namespace": namespace,
                "spec_id": spec["spec_id"],
                "run_id": run_id,
                "state_id": state_id,
                "intent_profile": ip,
                "propagation_requirements_json": _propagation_requirements_json(
                    _load_yaml(registry_path), ip
                ),
                "parameters": parameters[wc],
                "resources": resources[wc],
                "images": images,
            }
            rendered_yaml = tmpl.render(**ctx)
            out_path = fg_dir / f"{idx:03d}-{base}.yaml"
            out_path.write_text(rendered_yaml, encoding="utf-8")
            fg_jobs.append(RenderedJob(
                name=base,
                path=out_path,
                workload_class=wc,
                intent_profile=ip,
                planned_submit_offset_seconds=offset,
            ))
        _write_timeline(state_out / "timeline-planned.csv", fg_jobs)

        # Coverage verification per state: each (wc, intent) combination
        # must appear exactly k times. Surfacing this in the summary makes
        # the analyser able to assert it offline.
        from collections import Counter
        per_combo = Counter((j.workload_class, j.intent_profile) for j in fg_jobs)
        summary["states"].append({
            "state_id": state_id,
            "background_jobs": len(bg_jobs),
            "foreground_jobs": len(fg_jobs),
            "per_combo_counts": {f"{c[0]}__{c[1]}": n for c, n in per_combo.items()},
        })
        summary["total_foreground_jobs"] += len(fg_jobs)
        summary["total_background_jobs"] += len(bg_jobs)

    return summary


# ---------------------------------------------------------------------------
# iter-4h-2: multi-state Phase-B rendering
# ---------------------------------------------------------------------------

def _short_job_name(short_spec: str, short_baseline: str, short_state: str,
                    run_id_ts: str, idx: int, short_wc: str) -> str:
    """Build a DNS-1123-safe foreground Job name using the iter-4h-2
    short-code convention.

    Layout (see iter-4h-2 plan § 6.0 length-budget analysis):
        delphi-<short_spec>-<short_baseline>-<short_state>-<run_id_ts>-<NNN>-<short_wc>

    The run_id timestamp suffix is kept intact (it is the unique
    discriminator across cells of the same campaign); idx and wc are
    placed last so that, if truncation kicks in despite the short
    codes, only cosmetic tail is dropped, not the cluster-state
    discriminator.
    """
    base = (
        f"delphi-{short_spec}-{short_baseline}-{short_state}-"
        f"{run_id_ts}-{idx:03d}-{short_wc}"
    )
    base = base.lower()
    if len(base) > 63:
        base = base[:63].rstrip("-").rstrip(".")
    return base


def render_multistate(spec_path: Path,
                      registry_path: Path,
                      cluster_states_dir: Path,
                      run_id: str,
                      templates_dir: Path,
                      output_dir: Path,
                      state_ids: list[str],
                      *,
                      baseline: str,
                      disturbance_tail_seconds: float = 60.0,
                      ) -> dict[str, Any]:
    """Render a foreground spec + a list of cluster-state specs.

    iter-4h-2's primary entry point. For each `state_id` in the list,
    renders the foreground spec into a per-state subdir (so each cell
    gets its own Kubernetes Job names) and dispatches the cluster-state
    spec to either `render_cluster_state` (single-pin legacy) or
    `render_disturbance_stream` (disturbance-stream new) based on
    which key the spec carries.

    The rendered tree:

        <output_dir>/
            images.json                       # iter-4c.2 manifest (caller-provided)
            multistate.json                   # this function's output
            states/
                <state_id>/
                    foreground/*.yaml         # the foreground spec rendered for this state
                    timeline-planned.csv      # foreground arrival timeline
                    {background,disturbance}/*.yaml  # per state's class
                    disturbance-timeline-planned.csv # iff disturbance-stream

    Note: the foreground spec is *identical* across states; only the
    rendered Kubernetes Job names differ (state_id appears in the
    short-coded name suffix per § 6.0). Cross-state metric comparison
    stays fair because the same `(workload_class, intent_profile,
    parameters)` set is materialised per state.

    Returns a structured `multistate.json` summary that the submit
    subcommand reads to walk the per-state subdirs.
    """
    spec = _load_yaml(spec_path)
    registry = _load_yaml(registry_path)
    # Note: per-state RNGs are seeded inside the loop below (one per
    # state) so each state gets a deterministic but distinct shuffle.
    # No global rng needed at this scope.

    spec_id = spec["spec_id"]
    namespace = spec["namespace"]
    parameters = spec["parameters"]
    resources = spec["resources"]
    temporal = spec["temporal_profile"]
    distribution = spec["distribution"]
    total_jobs = int(spec["total_jobs"])

    # Short codes — fail fast if any long form is missing from the
    # _short_codes mapping (§ 6.0 contract).
    from . import _short_codes
    short_spec = _short_codes.spec_id_short(spec_id)
    short_baseline = _short_codes.baseline_short(baseline)
    # run_id_ts is the timestamp tail; the short prefix (etb-df-mdh-…)
    # is built per state. The caller's run_id is the full long form
    # `<spec>-<baseline>-<state>-<UTC>` for human readability; the
    # rendered Job names use only the UTC tail to stay in the budget.
    run_id_ts = run_id.split("-")[-1]

    output_dir.mkdir(parents=True, exist_ok=True)
    states_dir = output_dir / "states"
    states_dir.mkdir(parents=True, exist_ok=True)

    # iter-4c.2: images.json is the per-experiment image manifest.
    # Loaded once and threaded through both foreground and per-state
    # cluster-state renders.
    images = _load_images(output_dir)

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    summary: dict[str, Any] = {
        "spec_id": spec_id,
        "baseline": baseline,
        "run_id": run_id,
        "namespace": namespace,
        "states": [],
        "total_foreground_jobs": 0,
        "total_disturbance_jobs": 0,
        "total_background_jobs": 0,
    }

    # Local import to keep the module-level import graph linear.
    from . import cluster_state as _cs

    for state_id in state_ids:
        state_path = cluster_states_dir / f"{state_id}.yaml"
        if not state_path.exists():
            raise FileNotFoundError(
                f"cluster-state spec '{state_id}' not found at {state_path}"
            )
        state_out = states_dir / state_id
        state_out.mkdir(parents=True, exist_ok=True)

        # Validate up front so the function fails before any kubectl
        # apply could see a half-rendered tree.
        state_spec = _cs._load_yaml(state_path)
        _cs.validate_state_spec(state_spec, source=state_path)
        has_disturbance = (
            "disturbance" in state_spec and state_spec["disturbance"] is not None
        )

        # ---- foreground: render the same spec per state ----------
        # Per-state RNG seeded from the foreground spec's random_seed
        # plus the state index, so each state gets a deterministic but
        # state-distinct shuffle. Without this, every state would share
        # the same shuffled distribution which is fine for fairness but
        # makes the rendered names collide on truncation.
        state_rng = random.Random(spec.get("random_seed", 0) * 100003
                                  + hash(state_id) % 10_000)
        assignments = _normalised_distribution(distribution, total_jobs, state_rng)
        offsets = _arrival_offsets(temporal, total_jobs, state_rng)

        fg_dir = state_out / "foreground"
        fg_dir.mkdir(parents=True, exist_ok=True)
        short_state = _short_codes.state_id_short(state_id)

        fg_jobs: list[RenderedJob] = []
        for idx, ((wc, ip), offset) in enumerate(zip(assignments, offsets)):
            template_name = f"job-{wc}.yaml.j2"
            tmpl = env.get_template(template_name)
            short_wc = _short_codes.workload_class_short(wc)
            name = _short_job_name(
                short_spec, short_baseline, short_state,
                run_id_ts, idx, short_wc,
            )
            ctx = {
                "job_name": name,
                "namespace": namespace,
                "spec_id": spec_id,
                "run_id": run_id,
                "state_id": state_id,
                "intent_profile": ip,
                "propagation_requirements_json": _propagation_requirements_json(
                    registry, ip
                ),
                "parameters": parameters[wc],
                "resources": resources[wc],
                "images": images,
            }
            rendered_yaml = tmpl.render(**ctx)
            out_path = fg_dir / f"{idx:03d}-{name}.yaml"
            out_path.write_text(rendered_yaml, encoding="utf-8")
            fg_jobs.append(RenderedJob(
                name=name,
                path=out_path,
                workload_class=wc,
                intent_profile=ip,
                planned_submit_offset_seconds=offset,
            ))
        _write_timeline(state_out / "timeline-planned.csv", fg_jobs)

        # Foreground window for the disturbance stream — last planned
        # offset plus a tail so disturbance stays alive while the slow
        # foreground Jobs are still running.
        foreground_window_seconds = max((j.planned_submit_offset_seconds
                                         for j in fg_jobs), default=0.0)

        # ---- cluster-state path: single-pin OR disturbance-stream --
        n_disturbance = 0
        n_background = 0
        if has_disturbance:
            disturbance_jobs = _cs.render_disturbance_stream(
                state_path=state_path,
                run_id=run_id,
                templates_dir=templates_dir,
                output_dir=state_out,
                foreground_window_seconds=foreground_window_seconds,
                namespace=namespace,
                spec_id_override=spec_id,
                images=images,
                disturbance_tail_seconds=disturbance_tail_seconds,
            )
            n_disturbance = len(disturbance_jobs)
        else:
            bg_jobs = _cs.render_cluster_state(
                state_path=state_path,
                run_id=run_id,
                templates_dir=templates_dir,
                output_dir=state_out,
                namespace=namespace,
                spec_id_override=spec_id,
                images=images,
            )
            n_background = len(bg_jobs)

        # Per-(wc, intent) census for the analyzer.
        from collections import Counter
        per_combo = Counter((j.workload_class, j.intent_profile) for j in fg_jobs)
        summary["states"].append({
            "state_id": state_id,
            "short_state": short_state,
            "foreground_jobs": len(fg_jobs),
            "background_jobs": n_background,
            "disturbance_jobs": n_disturbance,
            "has_disturbance": has_disturbance,
            "per_combo_counts": {f"{c[0]}__{c[1]}": n for c, n in per_combo.items()},
        })
        summary["total_foreground_jobs"] += len(fg_jobs)
        summary["total_disturbance_jobs"] += n_disturbance
        summary["total_background_jobs"] += n_background

    # Persist the multistate manifest for the submit subcommand.
    (output_dir / "multistate.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary

