# Benchmark CRD — thin declarative design

## What this is

`benchmark.delphi.io/v1alpha1` `Benchmark` is a **thin, declarative, installable,
versioned** representation of a Mid4CC benchmark campaign. It is a distinct
object in its own API group, with its own controller and its own
target, separate from the other custom resources in this codebase.

A `Benchmark` object's reconciler does exactly four things, all local to the API
server it runs against:

1. **Validates** the campaign spec (non-empty coverage dimensions after
   defaulting; `k >= 1`; known baselines / disturbance / temporal pattern;
   `burstSize`/`restSeconds` required iff `pattern == burst`).
2. **Computes** a deterministic resolved coverage grid — every
   `(workloadClass × intentProfile × clusterState)` combination repeated `k`
   times (DESIGN.md §12) — plus a stable `sha256` hash (first 16 hex chars) over
   the canonicalized resolved campaign (sorted grid + `k` + temporal + baselines
   + disturbance + specRef).
3. **Materializes** a namespaced campaign ConfigMap `benchmark-<name>`, owned by
   the Benchmark, with keys `resolvedGridHash`, `plannedJobCount`, `specRef`,
   `grid.json`, `temporalProfile.json`, `baselines`, `disturbance`.
4. **Reports** status: `observedGeneration`, `resolvedGridHash`,
   `plannedJobCount`, `phase` (`Rendered` / `Invalid`), and `Validated` / `Ready`
   conditions.

## What this is NOT (and how it reconciles with DESIGN.md §14)

`experiments/DESIGN.md` §14 lists, under *Out of scope for now*:

> "A Kubernetes CRD for 'Experiment' objects. Considered and rejected: YAML specs
> + Python orchestrator already cover every need with one fewer permanent
> control-plane component."

and the development repository's orchestration notes add that a custom
CRD/controller "would require `on-prem`/`karmada-system` cluster-scoped write
surface that is otherwise unnecessary."

The rejection has two halves; this design honors both rather than paraphrasing
them away:

- **(a) It does not replace the orchestrator.** The rejected object was an
  *Experiment runner* — a CRD whose controller would compile specs, submit Jobs,
  drive Karmada, and thereby duplicate the Python orchestrator as a second,
  permanent, in-cluster executor. This `Benchmark` object runs nothing. It has
  **no cluster calls, no orchestrator calls, no Karmada or member-cluster
  calls**. The Python orchestrator remains the sole executor; the Benchmark is a
  declarative *description and validation* of a campaign that the orchestrator
  (or a human) still executes. The materialized ConfigMap is an artifact the
  orchestrator *may* consume, not a command the controller issues.

- **(b) It adds no cluster-scoped *write* surface.** All writes (Benchmark
  status updates, campaign ConfigMap create/update) are granted by a
  **namespaced `Role`** (`deploy/benchmark-rbac.yaml`). The only cluster-scoped
  grant is a **read-only `ClusterRole`** (`get`/`list`/`watch` on `benchmarks`
  and `configmaps`), which is unavoidable: the manager (`main.go`) is built with
  no namespace restriction, so its shared cache's Benchmark informer and the
  `Owns(ConfigMap)` informer list/watch cluster-wide and would fail to start
  without cluster-wide read. A controller that keeps a *secondary*
  host-cluster cache can sidestep this by scoping that cache to one namespace; this controller is required
  to use the manager's default client only, so it inherits the cluster-wide
  cache. This directly answers the second half of the recorded objection — the
  objection was to cluster-scoped *write* surface, and there is none.

In short: §14 rejected a CRD that *becomes a redundant runner and demands
cluster-wide write power*. This CRD is neither — it is a reproducibility and
installability artifact (a campaign gets a typed, versioned, hash-stamped
identity and a validating admission path) that **delegates execution** to the
orchestrator. The distinction is argued here on purpose, because §14 rejected
the *category* "Experiment CRD"; the value added is declarative validation +
deterministic coverage/hash + a consumable ConfigMap, not execution.

## Determinism

Coverage dimensions are deduped and sorted before the grid is built, so the grid
order, `plannedJobCount`, hash, and ConfigMap payload are independent of the
author's declaration order. The hash is pinned in
`controllers/benchmark_controller_test.go` so a refactor that changes
canonicalization fails loudly instead of silently re-baselining.

## Deployment decision that needs confirmation

This controller uses the **manager's default client only** (no secondary
host-cluster client watching the `on-prem` host API via a second cache). Consequently `Benchmark` objects and their ConfigMaps live
on whichever API server the manager is wired to — currently the **Karmada** API
(`main.go` builds the manager on `karmadaCfg`). If campaigns should instead live
on the `on-prem` host API, the wiring must add a host
client/cache and this file, the RBAC namespace, and `SetupWithManager` change
accordingly. This is called out as the one design decision to confirm.

The RBAC shape (`deploy/benchmark-rbac.yaml`) is the same underlying question:
the cluster-wide manager cache forces a cluster-wide read grant, and the
namespaced write Role must be replicated in each namespace where Benchmarks are
created (the examples use `delphi-experiments`). Scoping the Benchmark watch to
specific namespaces instead would require a per-controller/secondary cache — the
very thing the "default client only" constraint precludes. Confirm the intended
API server and namespace set together with the wiring decision above.

## Verify (dry-run, no cluster mutation)

```bash
kubectl --context on-prem apply --dry-run=server -f deploy/benchmark-crd.yaml
kubectl --context on-prem apply --dry-run=server -f deploy/benchmark-rbac.yaml
kubectl --context on-prem apply --dry-run=server -f examples/benchmark-effectiveness.yaml
kubectl --context on-prem apply --dry-run=server -f examples/benchmark-registry-drift.yaml
```
