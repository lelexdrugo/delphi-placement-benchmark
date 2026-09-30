"""Read each probe's iperf3 -J stdout and produce
`experiments/specs/cluster-rtt-measured.yaml`.

Where the stdout lives.
    Each probe pins itself to its source cluster. Karmada propagates the
    Job to that member context. The Pod's stdout — a single JSON
    document — is fetched per-probe with `kubectl --context <source>
    logs job/<name> -n delphi-experiments`. Reads on any member context
    are always allowed by the hooks; only writes are gated.

iperf3 -J output and units.
    Modern iperf3 emits, under `end.streams[*].sender`, the fields
    `mean_rtt`, `min_rtt`, `max_rtt` in **microseconds**. We convert to
    milliseconds (÷1000.0) before aggregation. If the field is absent
    (older client, kernel without `tcp_info`), we surface a `null` for
    that probe instead of inventing a value.

What we report.
    For each (source, target) pair we compute:
      - rtt_ms_p50: median of per-probe mean_rtt;
      - rtt_ms_p95: 95th percentile of per-probe mean_rtt;
      - n: number of successful samples that contributed.
    Plus an ISO-8601 `measured_at` and the git commit if available.
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from . import render as _render


@dataclass(frozen=True)
class ProbeSample:
    name: str
    source: str
    target: str
    replica: int
    mean_rtt_ms: Optional[float]
    min_rtt_ms: Optional[float]
    max_rtt_ms: Optional[float]
    raw_present: bool
    note: str


def _kubectl(args: list[str], *, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True, text=True, check=False, timeout=timeout,
    )


def _fetch_logs(source_context: str, namespace: str, job_name: str,
                *, timeout_seconds: int = 90) -> tuple[str, str]:
    """Return (stdout, note). Tolerates the typical failure modes
    (job not found, no pods scheduled yet, slow edge kubeconfig) so the
    reduce step never crashes on a single laggy probe. Edge contexts in
    pull-mode Karmada can be slow to respond; a generous default timeout
    keeps the reducer forgiving without blocking forever.
    """
    try:
        proc = _kubectl([
            "--context", source_context, "logs", f"job/{job_name}",
            "-n", namespace,
        ], timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return "", f"kubectl-logs-timeout-{timeout_seconds}s"
    except OSError as exc:
        return "", f"kubectl-os-error: {exc}"
    if proc.returncode != 0:
        return "", (proc.stderr or "").strip()[:300]
    return proc.stdout, ""


def _parse_iperf_json(stdout: str) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    """Pull (mean_rtt_ms, min_rtt_ms, max_rtt_ms, note) from iperf3 -J output.

    iperf3 writes a single JSON document on stdout. RTT fields under
    end.streams[0].sender are microseconds. Some kernels / iperf3
    builds may omit the fields; we report None in that case.
    """
    if not stdout.strip():
        return None, None, None, "empty-stdout"
    # iperf3 -J emits exactly one JSON document; tolerate trailing whitespace.
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError as exc:
        # Some older builds prefix non-JSON banners. Try to locate the JSON.
        first_brace = stdout.find("{")
        last_brace = stdout.rfind("}")
        if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
            return None, None, None, f"json-decode: {exc}"
        try:
            obj = json.loads(stdout[first_brace:last_brace + 1])
        except json.JSONDecodeError as exc2:
            return None, None, None, f"json-decode: {exc2}"

    streams = (obj.get("end") or {}).get("streams") or []
    if not streams:
        return None, None, None, "no-end-streams"
    sender = streams[0].get("sender") or {}
    mean_us = sender.get("mean_rtt")
    min_us = sender.get("min_rtt")
    max_us = sender.get("max_rtt")
    if mean_us is None and min_us is None and max_us is None:
        return None, None, None, "no-rtt-fields"
    return (
        (float(mean_us) / 1000.0) if mean_us is not None else None,
        (float(min_us) / 1000.0) if min_us is not None else None,
        (float(max_us) / 1000.0) if max_us is not None else None,
        "ok",
    )


def _percentile(values: list[float], pct: float) -> float:
    """Linear interpolation percentile. statistics.quantiles needs >=2
    points; we sidestep that with a single small implementation since
    n=3 is the common case here.
    """
    if not values:
        raise ValueError("percentile of empty sequence")
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return s[lo]
    frac = rank - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _git_commit(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def collect_samples(
    *,
    run_dir: Path,
    namespace: str = "delphi-experiments",
) -> list[ProbeSample]:
    plan = _render.load_plan(run_dir)
    samples: list[ProbeSample] = []
    for entry in plan.probes:
        source = entry["source"]
        name = entry["name"]
        stdout, note = _fetch_logs(source, namespace, name)
        if not stdout:
            samples.append(ProbeSample(
                name=name, source=source, target=entry["target"],
                replica=entry["replica"],
                mean_rtt_ms=None, min_rtt_ms=None, max_rtt_ms=None,
                raw_present=False, note=note or "no-logs",
            ))
            continue
        mean_ms, min_ms, max_ms, parse_note = _parse_iperf_json(stdout)
        samples.append(ProbeSample(
            name=name, source=source, target=entry["target"],
            replica=entry["replica"],
            mean_rtt_ms=mean_ms, min_rtt_ms=min_ms, max_rtt_ms=max_ms,
            raw_present=True, note=parse_note,
        ))
    return samples


def aggregate_pairs(samples: list[ProbeSample]) -> dict[str, dict[str, object]]:
    """Group samples by (source, target) and produce p50/p95/n."""
    by_pair: dict[tuple[str, str], list[float]] = {}
    for s in samples:
        if s.mean_rtt_ms is None:
            continue
        by_pair.setdefault((s.source, s.target), []).append(s.mean_rtt_ms)
    out: dict[str, dict[str, object]] = {}
    for (src, dst), vals in by_pair.items():
        key = f"{src}__to__{dst}"
        out[key] = {
            "source": src,
            "target": dst,
            "n": len(vals),
            "rtt_ms_p50": round(_percentile(vals, 50.0), 3),
            "rtt_ms_p95": round(_percentile(vals, 95.0), 3),
        }
    return out


def reduce_run(
    *,
    run_dir: Path,
    namespace: str = "delphi-experiments",
    repo_root: Optional[Path] = None,
    write_specs_yaml: bool = True,
) -> Path:
    """Collect logs, write `<run-dir>/samples.json`, optionally write
    `experiments/specs/cluster-rtt-measured.yaml`. Returns the written
    spec path (or the samples.json path if write_specs_yaml=False).
    """
    samples = collect_samples(run_dir=run_dir, namespace=namespace)
    aggregated = aggregate_pairs(samples)

    samples_path = run_dir / "samples.json"
    samples_path.write_text(json.dumps(
        [
            {
                "name": s.name, "source": s.source, "target": s.target,
                "replica": s.replica,
                "mean_rtt_ms": s.mean_rtt_ms,
                "min_rtt_ms": s.min_rtt_ms,
                "max_rtt_ms": s.max_rtt_ms,
                "raw_present": s.raw_present,
                "note": s.note,
            }
            for s in samples
        ],
        indent=2,
    ), encoding="utf-8")

    if not write_specs_yaml:
        return samples_path

    if repo_root is None:
        # render.py: experiments/orchestrator/calibration/reduce.py ->
        #     parents[3] = experiments/../ = repo root.
        repo_root = Path(__file__).resolve().parents[3]

    spec_path = repo_root / "experiments" / "specs" / "cluster-rtt-measured.yaml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)

    measured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "measured_at": measured_at,
        "git_commit": _git_commit(repo_root),
        "run_dir": str(run_dir.relative_to(repo_root)) if run_dir.is_absolute() else str(run_dir),
        "n_probes_attempted": len(samples),
        "n_probes_with_rtt": sum(1 for s in samples if s.mean_rtt_ms is not None),
        "pairs": aggregated,
        "note": (
            "Per-probe iperf3 sender.mean_rtt converted from microseconds "
            "to milliseconds. p50/p95 computed over per-pair replicas. "
            "Reads from member-cluster contexts (read-only). Target is "
            "on-prem iperf3-server NodePort 31521."
        ),
    }
    spec_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return spec_path
