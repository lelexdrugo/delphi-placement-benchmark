# A declarative benchmark for intent-driven placement across a federated continuum

This repository holds the benchmarking artifact for the paper *Benchmarking
Intent-Driven Placement Across the Federated Computing Continuum: A Declarative
Environment and Evaluation Campaigns*: the declarative campaign API, the scenario
specifications, the campaign orchestrator, and the analysis and metric modules
that score a placement after the fact.

It is the harness, not the middleware. The placement middleware the harness runs
against is the subject of separate work and is not published here.

## Why a benchmark, and not just a scheduler

A continuum does not hold still. Members join, leave, are relabelled, are drained
for maintenance; a node's server is replaced, its architecture changes, an
accelerator is added; the network between clusters is re-cabled. Any description
of the federation, whether a curated registry or a cached snapshot, therefore
drifts away from the thing it describes, and it does so quietly.

That is a problem for anything that reads such a description in order to decide,
and placement reads one every time a workload arrives. Measuring what the drift
costs needs a fixture that holds everything else still: the same workload, the
same declared intent, the same arrival timing, the same seeds. This repository is
that fixture.

The consequence is more general than the study it was built for. Because a
campaign is declared once and replayed from recorded seeds, timings and cluster
state, anything you change about the infrastructure becomes measurable by
difference. Re-run the same campaign after swapping a node, changing an
architecture, adding an accelerator or re-shaping the network, and the two sets
of measurements are comparable. The harness does not model or inject those
changes. It gives you a fixed yardstick across them.

## A campaign, concretely

A campaign is a Kubernetes object. This one compares three placement policies
over a clean federation:

```yaml
apiVersion: benchmark.delphi.io/v1alpha1
kind: Benchmark
metadata:
  name: effectiveness
  namespace: delphi-experiments
spec:
  specRef: effectiveness-taskbound     # the workload + intent spec to render
  workloadClasses: [cpu, memory, network]
  intentProfiles: [latency-sensitive, cost-aware, locality-aware, balanced]
  clusterStates: [idle]                # background load to shape the fleet with
  k: 3                                 # repetitions of each coverage combination
  baselines: [static-karmada, heuristic-scorer, delphi-full]
  disturbance: none
  temporalProfile:
    pattern: poisson
    meanInterArrivalSeconds: 10
    jitterSeconds: 2
    maxConcurrentJobs: 6
    randomSeed: 42
```

Declaring `disturbance: registry-drift` names the axis; the registry variants
themselves are versioned files in `experiments/specs/cluster-registry-variants/`,
applied between cells while the same workload is replayed, so the only thing
differing between cells is what the policy is able to read. The cordon's target
and window (`experiments/specs/cordon-window.yaml`) and the background-load
scenarios (`experiments/specs/cluster-states/`) are versioned the same way.
`examples/` holds both resources.

The reconciler is deliberately thin: it validates the campaign, expands it into a
deterministic coverage grid over (workload class × intent profile × cluster
state) × `k`, records a hash over the resolved grid, and materializes a campaign
`ConfigMap`. It calls neither the federation nor the orchestrator, so a campaign
can be reviewed and versioned like any other manifest. The orchestrator renders
the scenario specification the resource names (`specRef`) and replays it through
the federation; it does not read the `ConfigMap` today, which is the resolved,
hashed record of the campaign.

## What the environment models

A cell fixes one value of each of the four axes above, and the coverage grid is
their product, replayed `k` times. What each axis expresses is worth spelling
out, because the YAML is terse and the decisions behind it do not show in the
field names.

### Workload class

`cpu`, `memory` and `network`, rendered from the Jinja templates in
`experiments/templates/`. Foreground jobs are *task-bound*: each finishes after a
fixed amount of work rather than after a fixed wall-clock duration. That choice
is what makes completion time say anything about placement, since a stressor told
to run for sixty seconds takes sixty seconds wherever it lands, on a 4-core arm64
edge member as readily as on a 12-core cloud one.

### Intent profile

`latency-sensitive`, `cost-aware`, `locality-aware` and `balanced`. Each is a
hard feasibility gate, meaning a required region plus ceilings on the ordinal
latency and cost ladders, together with a set of preferred member classes used
for ranking. The vocabulary is closed, which is what lets the scorer read labels
alone and stay deterministic.

### Cluster state

Ten scenarios in `experiments/specs/cluster-states/`, and they come in two
shapes. A spec may not mix them: the renderer fails fast on one that declares
both.

*Pinned saturation* holds named members at a target fraction of node capacity
for the whole cell, using long-lived background jobs pinned to them. Five
scenarios do this. The three `skewed-*-saturated` ones drive a single member to
roughly 70% and leave the rest alone, one per member class; `edges-saturated`
loads both arm64 members at once; `mixed-load` puts a milder 50% across the
fleet. Each waits on a stabilisation check before the foreground stream starts,
so the fleet is actually in its declared state when the first decision is
taken.

*Continuous disturbance* never settles. The remaining four scenarios spawn
short jobs on a Poisson process across named targets at a declared intensity,
`light` or `heavy`, with per-target overrides derived from each member's
headroom, a steady-state check that confirms the load actually arrived, and a
ceiling on concurrent disturbance jobs that acts as a circuit breaker. The light
variant is calibrated to stay detectable without pushing any member out of the
available-clusters allowlist, which would turn a contention scenario into an
availability one.

The two differ in what a policy can carry over between decisions. Under a
saturation, a policy that consults its own history sees the same load each time
it decides. Under a disturbance, the load it recorded has already changed by the
time it reads it. `idle` is the third case, where nothing loads the fleet, and it
is what the reported campaigns run against.

Background load is persisted under `kind = 'background'`, which the history
queries in `experiments/orchestrator/analysis/queries.sql` exclude and a
no-pollution validator checks. Without that, the mechanism creating the load
would end up inside the evidence a history-reading policy reasons over, and the
axis would contaminate the very thing it exists to vary.

### Arrival timing

`uniform`, `poisson` or `burst`, the last taking a burst size and a rest
interval, each with a jitter. The specification also records a ceiling on
concurrent foreground jobs, which the current submitter does not enforce: jobs
are applied at their planned offsets.
Arrival timing is fixed and replayed like every other axis, because when a job
arrives decides what the federation looks like to the policy that places it. Two runs
that submit the same jobs in a different order are not the same experiment.

### And, orthogonally, the declared registry

The registry is what a policy reads instead of asking the federation, so it gets
its own axis, in `experiments/specs/cluster-registry-variants/`: `r0-full`
agrees with the federation's canonical classification, `r1-missing` drops
labels, `r2-region-drift` renames a region, and `r3-compound-drift` adds to that
rename an inverted cost ladder, as when prices change and the tiers are never
revised. Each variant file states the operator event it stands for and the
registry heuristic's hard compliance its rule predicts.
Exactly one view is perturbed per sweep, which is what keeps a sweep
single-factor and its result attributable.

One caveat about all of this, spelled out again below: the cluster-state model is
richer than what has been measured through it. The reported campaigns run against
`idle`, and no contention result is claimed.

## What is here

| path | what it is |
|---|---|
| `crd/benchmark-crd.yaml` | the `Benchmark` CustomResourceDefinition, the exact schema |
| `crd/benchmark-rbac.yaml` | the roles its controller needs |
| `crd/DESIGN.md` | why the API is shaped this way, and what it deliberately does not do |
| `examples/` | two complete campaigns: a clean comparison, and a registry-drift sweep |
| `experiments/specs/` | the workload and intent specs, the four registry variants (`r0` to `r3`), and the ten cluster-state scenarios |
| `experiments/templates/` | the Jinja templates the specs render into Kubernetes Jobs, which is to say the workloads themselves |
| `experiments/orchestrator/` | the campaign executor: rendering, submission, collection |
| `experiments/orchestrator/analysis/metrics/` | how each metric is computed, offline, from stored artifacts |
| `experiments/orchestrator/calibration/` | renders, submits and reduces the iperf3 probes that measure inter-member round-trip time |
| `experiments/iperf3-server/` | the receiving end those probes and the network workload class talk to |
| `experiments/preflight/` | cross-cluster reachability and certificate checks to run before a campaign |
| `experiments/db/` | dump and restore the decision history, so every policy starts from the same one |

The `experiments/` prefix is not decoration: module paths, the CLI's default
spec locations and the documented `python -m experiments.orchestrator.cli`
invocations all resolve against it, so the tree is laid out as the code expects
to find it.

If you want to check one thing, check
`experiments/orchestrator/analysis/metrics/intent_compliance.py`. It is where a
placement is graded, and it grades against the federation's canonical
classification rather than against whatever the policy believed, which is the
only way a stale-description failure becomes visible at all.

## Characterising your own setup

Comparing placement policies presupposes knowing what you are placing onto, and
a federation assembled from whatever hardware you have is not self-describing.
The same tooling we used to characterise ours is here, which is the part most
likely to be useful to someone whose federation looks nothing like ours.

**Reachability, before anything else.** `experiments/preflight/` probes whether
members can actually reach each other. The interesting one is
`check-net-job-based.ps1`, which writes one `Job` per target through the
federation's own entry point and lets Karmada propagate it, rather than writing
a Pod into each member context directly. The direct approach needs LAN or VPN
routes from wherever you are sitting; this one needs only what the federation
already has. `check-karmada-certs.ps1` reports certificate expiry, which is the
failure that looks like a network problem.

**Round-trip time, measured.**
`experiments/orchestrator/calibration/` renders a small set of pinned iperf3
probes, submits them, and reduces the JSON into per-member figures;
`experiments/iperf3-server/` is the receiving end, and the network workload
class talks to the same deployment. What comes out is a table like
`experiments/specs/cluster-rtt-measured.yaml`, from which the categorical `rtt`
labels in the cluster registry are derived. Those labels are what an intent's
latency ceiling is checked against, so on a different federation they have to be
re-derived rather than copied: ours say nothing about yours.

**A frozen decision history.** A policy that reads its own past decisions is not
comparable to one that does not unless both start from the same past.
`experiments/db/` snapshots the decision store into a versioned dump and
restores it before each measured run, so every policy reads the same past
whatever order you run them in.

None of this is where the science is. It is the part that has to be right before
any of the measurements mean anything, and it is usually the part nobody
publishes.

## What is deliberately not here

**Measured results.** No measurements are published here. The per-job results we
have came from a configuration that has since been superseded, and publishing
them would let a reader compute numbers that the paper itself does not report,
from a setup neither of us should be standing behind. The metric code is here in
full, so a campaign of your own is scorable today; ours are added when runs on
the current stack have been validated.

**The contention axis.** The scenario model expresses background load in the two
shapes described above, and `experiments/specs/cluster-states/` contains ten
declarations of it, but no contention campaign or metric is reported. The
modelling is real and the measurement is not, and those are different claims.

**Network topology.** Locality enters as a declared cluster class. Nothing here
emulates or injects a network topology.

**The replay tests for measured runs.** The analyzer has a second test layer
that re-scores recorded campaigns offline and checks the result against what was
recorded at the time. It depends on the excluded run directories and carries
their values, so it is not included; it arrives with them.

**The middleware.** Out of scope, as above.

One thing that *is* here and might look like a measurement:
`experiments/specs/cluster-rtt-measured.yaml` holds round-trip times taken
between the four members. It is kept deliberately, because it is an input: the
categorical `rtt` labels in the cluster registry are derived from it, and
without it those labels look arbitrary. Two private lab addresses in its
comments are redacted, and the tooling that produced the file is here, so the
honest thing to do on another federation is to regenerate it.

## Running it

The tests need no cluster, no database and no credentials. From the repository
root:

```
pip install -r experiments/orchestrator/requirements.txt
python -m pytest experiments/ -q
```

234 tests pass, 6 skip. The skips are the cases that want a recorded campaign
on disk; see above. The plot generators are covered against fabricated
fixtures committed next to the script that produces them
(`experiments/orchestrator/analysis/plots/tests/fixtures/`), so you can re-run
the generator and diff its output against what is checked in. That directory's
README is blunt about what those numbers are: invented, for exercising plot
shape, not to be cited.

Submitting a campaign, by contrast, does need a federation. The orchestrator
talks to a Karmada control plane through `kubectl` and renders the scenario
specification a `Benchmark` resource names. The on-prem node that the network
workload class and the preflight probes target appears as a documentation
placeholder, `203.0.113.10` (`TARGET_HOST` in the specifications,
`-OnPremNodeIp` in the preflight scripts): set it to your own node's address.

## Reproducing a campaign

Each run preserves its rendered job manifests, its planned timeline, and the
placement and status the control plane reported for every job, so every metric is
recomputed from stored artifacts rather than from live cluster state. Two runs
months apart are therefore comparable, and the whole design is arranged around
keeping them that way.

## Status and licence

This is research code released to accompany a paper, not a supported product. The
API is `v1alpha1` and is expected to change.

The scenario specifications and the example campaigns (`experiments/specs/`,
`examples/`), the measured round-trip table among them, are released under the
Creative Commons Attribution 4.0 International licence
(`LICENSES/CC-BY-4.0.txt`). Everything else, the code and its documentation, is
released under the MIT licence (`LICENSE`, `LICENSES/MIT.txt`).
