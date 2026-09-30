"""Render the 12 RTT calibration probe Jobs into a run directory.

Topology (iter-4b plan).
    sources     = {on-prem, public-cloud, edge-1, edge-2}
    target      = on-prem (iperf3-server, NodePort 31521)
    replicas    = 3 per (source, target) pair  →  12 probes total

Every Job is pinned with delphi.experiments/pin-to-cluster: <source>;
the decision-maker handles that as an immediate placement
(score 1.0, no NATS publish), so the probe lands on the intended
member cluster regardless of the active baseline. Each Job is also
labelled delphi.experiments/role: "background" so the no-pollution
filter at decision_repository.go excludes it from MCP history,
similarity, and agent reasoning (kind='foreground' WHERE clause).

The render step is pure: it never touches the cluster. The subsequent
submit step gates real apply behind --i-know-this-runs-real-jobs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


DEFAULT_SOURCES = ("on-prem", "public-cloud", "edge-1", "edge-2")
DEFAULT_TARGET = "on-prem"
DEFAULT_REPLICAS = 3
DEFAULT_TARGET_PORT = 31521          # matches experiments/iperf3-server/service.yaml
DEFAULT_DURATION_SECONDS = 5
DEFAULT_NAMESPACE = "delphi-experiments"
DEFAULT_IMAGE = "<your-registry>/iperf3-server:latest"
DEFAULT_ACTIVE_DEADLINE_SECONDS = 120


@dataclass(frozen=True)
class RenderedProbe:
    name: str
    path: Path
    source: str
    target: str
    replica: int


@dataclass(frozen=True)
class RenderPlan:
    """Pure description of what render() produced. Persisted as
    `<output_dir>/probes.json` so submit and reduce can rediscover the
    probe set without re-parsing every YAML.
    """
    run_id: str
    spec_id: str
    namespace: str
    target: str
    target_host: str
    target_port: int
    duration_seconds: int
    image: str
    probes: list[dict[str, Any]]


def _probe_name(spec_id: str, run_id: str, source: str, replica: int) -> str:
    base = f"{spec_id}-{run_id}-{source}-r{replica:02d}".lower()
    # Job names are DNS-1123 labels (<= 63 chars).
    if len(base) > 63:
        base = base[:63].rstrip("-")
    return base


def render(
    *,
    run_id: str,
    output_dir: Path,
    templates_dir: Path,
    target_host: str,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    target: str = DEFAULT_TARGET,
    replicas: int = DEFAULT_REPLICAS,
    target_port: int = DEFAULT_TARGET_PORT,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    namespace: str = DEFAULT_NAMESPACE,
    image: str = DEFAULT_IMAGE,
    active_deadline_seconds: int = DEFAULT_ACTIVE_DEADLINE_SECONDS,
    spec_id: str = "rtt-calibration",
) -> RenderPlan:
    """Render `len(sources) * replicas` probe Jobs under
    `<output_dir>/jobs/` and write the probe inventory to
    `<output_dir>/probes.json`.

    target_host is the on-prem node IP discovered out of band by the
    operator (`kubectl --context on-prem get nodes -o wide`) and passed
    on the CLI. We deliberately do not auto-discover it here: the
    iperf3 NodePort + node-IP coupling is identical to what the
    iter-2a preflight already documents in
    experiments/iperf3-server/service.yaml.
    """
    if not sources:
        raise ValueError("at least one source cluster is required")
    if replicas <= 0:
        raise ValueError("replicas must be > 0")
    if not target_host:
        raise ValueError("target_host is required (the on-prem node IP)")

    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("rtt-probe.yaml.j2")

    output_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir = output_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    probes: list[RenderedProbe] = []
    idx = 0
    for source in sources:
        for replica in range(1, replicas + 1):
            name = _probe_name(spec_id, run_id, source, replica)
            ctx = {
                "job_name": name,
                "namespace": namespace,
                "spec_id": spec_id,
                "run_id": run_id,
                "source": source,
                "target": target,
                "target_host": target_host,
                "target_port": str(target_port),
                "duration_seconds": str(duration_seconds),
                "replica": str(replica),
                "image": image,
                "active_deadline_seconds": active_deadline_seconds,
            }
            rendered_yaml = tmpl.render(**ctx)
            out_path = jobs_dir / f"{idx:03d}-{name}.yaml"
            out_path.write_text(rendered_yaml, encoding="utf-8")
            probes.append(RenderedProbe(
                name=name, path=out_path,
                source=source, target=target, replica=replica,
            ))
            idx += 1

    plan = RenderPlan(
        run_id=run_id,
        spec_id=spec_id,
        namespace=namespace,
        target=target,
        target_host=target_host,
        target_port=target_port,
        duration_seconds=duration_seconds,
        image=image,
        probes=[
            {
                "name": p.name,
                # POSIX slashes so submit/reduce can rejoin the path on
                # any platform; Path("a/b") is portable even on Windows.
                "path": p.path.relative_to(output_dir).as_posix(),
                "source": p.source,
                "target": p.target,
                "replica": p.replica,
            }
            for p in probes
        ],
    )
    (output_dir / "probes.json").write_text(
        json.dumps(plan.__dict__, indent=2), encoding="utf-8",
    )
    return plan


def load_plan(run_dir: Path) -> RenderPlan:
    """Reverse of the JSON dump in render()."""
    raw = json.loads((run_dir / "probes.json").read_text(encoding="utf-8"))
    return RenderPlan(**raw)


def discover_target_host_yaml() -> str:
    """Read the iperf3-server NodePort from the manifest as a sanity hint.
    Not the on-prem node IP — that requires cluster contact.
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[3]
    svc_yaml = repo_root / "experiments" / "iperf3-server" / "service.yaml"
    if not svc_yaml.exists():
        return ""
    try:
        doc = yaml.safe_load(svc_yaml.read_text(encoding="utf-8"))
        ports = doc.get("spec", {}).get("ports") or []
        if ports:
            return str(ports[0].get("nodePort", ""))
    except Exception:
        return ""
    return ""
