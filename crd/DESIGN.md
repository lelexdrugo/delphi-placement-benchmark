# Benchmark CRD — thin declarative design

## What this is

`benchmark.delphi.io/v1alpha1` `Benchmark` is a **thin, declarative, installable,
versioned** representation of a benchmark campaign. It is a distinct object in
its own API group, with its own controller (the reconciler), separate from the
other custom resources of the placement middleware. The reconciler ships with
the placement middleware and is not published here; this directory holds the
resource definition, its RBAC and this rationale.

A `Benchmark` object's reconciler does exactly four things, all local to the API
server it runs against:

1. **Validates** the campaign spec (non-empty coverage dimensions after
   defaulting; `k >= 1`; known baselines / disturbance / temporal pattern;
   `burstSize`/`restSeconds` required iff `pattern == burst`).
2. **Computes** a deterministic resolved coverage grid — every
   `(workloadClass × intentProfile × clusterState)` combination repeated `k`
   times — plus a stable `sha256` hash (first 16 hex chars) over the
   canonicalized resolved campaign (sorted grid + `k` + temporal + baselines
   + disturbance + specRef). The grid is the coverage the resource declares
   and validates; the job mix and count come from the named scenario
   specification (`specRef`).
3. **Materializes** a namespaced campaign ConfigMap `benchmark-<name>`, owned by
   the Benchmark, with keys `resolvedGridHash`, `plannedJobCount`, `specRef`,
   `grid.json`, `temporalProfile.json`, `baselines`, `disturbance`.
4. **Reports** status: `observedGeneration`, `resolvedGridHash`,
   `plannedJobCount`, `phase` (`Rendered` / `Invalid`), and `Validated` / `Ready`
   conditions.

## What this is NOT

The resource is deliberately not an experiment runner, and it needs no
cluster-scoped write surface.

- **(a) It does not run experiments.** A runner CRD would compile specs, submit
  Jobs and drive Karmada, duplicating the Python orchestrator as a second,
  permanent, in-cluster executor. The `Benchmark` reconciler runs nothing:
  beyond its own resource and ConfigMap on the API server it runs against, it
  **calls neither the federation nor the orchestrator**, so a campaign can be
  reviewed and versioned like any other manifest. The orchestrator renders the
  named scenario specification into Jobs and replays them through the
  federation.

- **(b) It adds no cluster-scoped *write* surface.** All writes (Benchmark
  status updates, campaign ConfigMap create/update) are granted by a
  **namespaced `Role`** (`crd/benchmark-rbac.yaml`). The only cluster-scoped
  grant is a **read-only `ClusterRole`** (`get`/`list`/`watch` on `benchmarks`
  and `configmaps`), which is unavoidable: the controller manager is built with
  no namespace restriction, so its shared cache's Benchmark informer and the
  `Owns(ConfigMap)` informer list/watch cluster-wide and would fail to start
  without cluster-wide read. A controller that keeps a *secondary* cache can
  sidestep this by scoping that cache to one namespace; this controller uses the
  manager's default client only, so it inherits the cluster-wide cache.

In short, the resource is a reproducibility and installability artifact: a
campaign gets a typed, versioned, hash-stamped identity and a validating
admission path. What it adds is declarative validation and a deterministic
coverage grid and hash, not execution.

## Determinism

Coverage dimensions are deduped and sorted before the grid is built, so the grid
order, `plannedJobCount`, hash, and ConfigMap payload are independent of the
author's declaration order. The reconciler's own tests pin the hash, so a
refactor that changes canonicalization fails loudly instead of silently
re-baselining.

## Where campaigns live

`Benchmark` objects and their campaign ConfigMaps live on the **Karmada API
server** (context `unique-logical-entrypoint`): the controller uses the
manager's default client only, and the manager is wired to the Karmada API.

The RBAC shape (`crd/benchmark-rbac.yaml`) follows from the same choice: the
cluster-wide manager cache forces a cluster-wide read grant, and the namespaced
write Role is replicated in each namespace where Benchmarks are created (the
examples use `delphi-experiments`). Scoping the Benchmark watch to specific
namespaces would instead require a per-controller or secondary cache, which the
default-client design rules out.

## Verify (dry-run, no cluster mutation)

```bash
kubectl --context unique-logical-entrypoint apply --dry-run=server -f crd/benchmark-crd.yaml
kubectl --context unique-logical-entrypoint apply --dry-run=server -f crd/benchmark-rbac.yaml
kubectl --context unique-logical-entrypoint apply --dry-run=server -f examples/benchmark-effectiveness.yaml
kubectl --context unique-logical-entrypoint apply --dry-run=server -f examples/benchmark-registry-drift.yaml
```

The example dry-runs succeed only once the CRD is installed on that API server.
