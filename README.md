# A declarative benchmark for intent-driven placement across a federated continuum

This repository holds the benchmarking artifact for the paper *Benchmarking
Intent-Driven Placement Across the Federated Computing Continuum: A Declarative
Environment and Evaluation Campaigns* (Mid4CC '26, ACM,
[doi:10.1145/3861040.3862572](https://doi.org/10.1145/3861040.3862572)): the
declarative campaign API, the scenario specifications, the campaign orchestrator,
and the analysis and metric modules that score a placement after the fact.

It contains the harness only. The placement middleware the harness runs against
is the subject of separate work and is not published here.

## Why a benchmark

A continuum does not hold still. Members join, leave, are relabelled, are drained
for maintenance; a node's server is replaced, its architecture changes, an
accelerator is added; the network between clusters is re-cabled. Any description
of the federation, whether a curated registry or a cached snapshot, therefore
drifts away from the thing it describes, and it does so quietly.

That is a problem for anything that reads such a description in order to decide,
and placement reads one every time a workload arrives. Measuring what the drift
costs needs a fixture that holds everything else still: the same workload, the
same declared intent, the same arrival timing, the same seeds. This repository
provides that fixture.

Because a campaign is declared once and replayed from recorded seeds, timings and
cluster state, you can run the same campaign again after changing the
infrastructure. Swap a node, change an architecture, add an accelerator or
re-cable the network, and the second run replays the same workload, intent,
timing and seeds as the first. The harness does not model or inject those
changes.

## An example campaign

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

This is the paper's undisturbed campaign; Listing 1 in the paper shows it
abridged. The three baselines are the paper's static, registry-heuristic and
agentic instances. `static-karmada` places every job on one configured member,
`heuristic-scorer` ranks the members from the declared registry, and
`delphi-full` is the multi-agent LLM backend of the placement middleware, which
is not published here.

Declaring `disturbance: registry-drift` names the axis. The registry variants
themselves are versioned files in `experiments/specs/cluster-registry-variants/`.
The operator applies them between cells through the middleware while the same
workload is replayed, so two cells differ only in what the policy can read. The
cordon's target and window (`experiments/specs/cordon-window.yaml`) and the
background-load scenarios (`experiments/specs/cluster-states/`) are versioned the
same way. `examples/` holds the undisturbed campaign and its registry-drift
version.

The reconciler is deliberately thin. It validates the campaign, expands it into a
deterministic coverage grid over (workload class × intent profile × cluster
state) × `k`, records a hash over the resolved campaign, and writes it to a
campaign `ConfigMap`. It does not call the federation or the orchestrator, so a
campaign can be reviewed and versioned like any other manifest. The reconciler
ships with the placement middleware and is not published here; `crd/` holds the
resource definition, and `crd/DESIGN.md` explains its design. The orchestrator in
this repository is given the scenario specification directly
(`render --spec experiments/specs/<specRef>.yaml`), renders it into Jobs and
replays them through the federation. The specification fixes the job mix and the
job count: `effectiveness-taskbound` draws 40 jobs from six weighted (workload
class, intent profile) pairs with seed 42.

## What the environment models

The coverage grid a campaign declares is workload class × intent profile ×
cluster state, with each combination at least `k` times. The paper uses *cell*
for one policy under one disturbance setting, replayed `k` times, and
`experiments/orchestrator/analysis/reduce_cells.py` reduces the runs of a cell to
their median. The axes are worth describing in prose, because the YAML is terse
and the reasons behind each choice do not show in the field names.

### Workload class

`cpu`, `memory` and `network`, rendered from the Jinja templates in
`experiments/templates/`. Foreground jobs are *task-bound*: each finishes after a
fixed amount of work instead of after a fixed wall-clock duration. With a fixed
amount of work, completion time depends on where the job runs. A stressor told to
run for sixty seconds takes sixty seconds wherever it lands, on a 4-core arm64
edge member as on a 12-core cloud one.

### Intent profile

`latency-sensitive`, `cost-aware`, `locality-aware` and `balanced`. Each is a
hard feasibility gate, meaning a required region plus ceilings on the ordinal
latency and cost ladders, together with a set of preferred member classes used
for ranking. The vocabulary is closed, so the scorer can read labels alone and
stay deterministic.

### Cluster state

Ten scenarios in `experiments/specs/cluster-states/`, in two shapes. A spec
cannot mix them: the renderer rejects one that declares both.

*Pinned saturation* holds named members at a target fraction of node capacity
for the whole cell, using long-lived background jobs pinned to them. Five
scenarios work this way. The three `skewed-*-saturated` ones drive a single
member to about 70% and leave the others idle, one per member class;
`edges-saturated` loads both arm64 members at once; `mixed-load` puts a milder
50% across the fleet. On the bootstrap path, the submitter waits before starting
the foreground stream until node utilisation is within a band around the target.
The wait is best-effort. It gives up after its timeout (300 s by default) and is
skipped when metrics-server is missing, and in both cases the stream starts
anyway.

*Continuous disturbance* never settles. The other four scenarios spawn short jobs
on a Poisson process across named targets at a declared intensity, `light` or
`heavy`, with per-target overrides derived from each member's headroom. Before
the foreground stream starts, a steady-state wait counts the active disturbance
Jobs on each target. It does not measure load, and it can time out. A ceiling on
concurrent disturbance jobs works as a circuit breaker. The light variant is
calibrated to stay detectable without pushing any member out of the
available-clusters allowlist, which would turn a contention scenario into an
availability one.

The two shapes differ in what a policy can carry over between decisions. Under a
saturation, a policy that consults its own history sees the same load each time
it decides. Under a disturbance, the load it recorded has already changed by the
time it reads it. `idle`, where nothing loads the fleet, is the third case and
the one the reported campaigns run against. The seed history they restore
(`experiments/specs/bootstrap.yaml`) holds every combination of workload class and
intent profile, three times each, in four of the ten states: `idle`,
`mixed-load`, `skewed-edge-1-saturated` and `skewed-on-prem-saturated`.

Background load is persisted under `kind = 'background'`. The history queries in
`experiments/orchestrator/analysis/queries.sql` exclude it, as the middleware's
own history filter does, and a no-pollution validator checks that nothing slipped
through. Without that, the jobs that create the load would end up in the history
a policy reasons over, and the axis would contaminate what it is meant to vary.

### Arrival timing

`uniform`, `poisson` or `burst`, the last with a burst size and a rest interval,
each with a jitter. The specification also records a ceiling on concurrent
foreground jobs, which the current submitter does not enforce: jobs are applied
at their planned offsets. Arrival timing is fixed and replayed like every other
axis, because when a job arrives decides what the federation looks like to the
policy that places it. Two runs that submit the same jobs in a different order
are different experiments.

### Declared registry

The registry is what a policy reads instead of asking the federation, so it gets
its own axis, in `experiments/specs/cluster-registry-variants/`. `r0-full` agrees
with the federation's canonical classification, `r1-missing` drops the latency
and cost labels of both edge members, `r2-region-drift` renames the public
cloud's region, and `r3-compound-drift` adds an inverted cost ladder to that
rename, as when prices change and nobody revises the tiers. Each drift variant
(`r1` to `r3`) names the operator event it stands for and the hard compliance the
registry heuristic should score on it. The heuristic scores each member by
whether its declared region matches the required one (one or zero), multiplied by
the mean closeness of its declared latency and cost classes to the requested ones
on the ladder `low < medium < high`, where an exact class scores one and an
adjacent class one half. The highest score wins, and ties go to the lexically
first member. A sweep perturbs exactly one view, so it stays single-factor and
its result can be attributed.

## What is here

| path | what it is |
|---|---|
| `crd/benchmark-crd.yaml` | the `Benchmark` CustomResourceDefinition, the exact schema |
| `crd/benchmark-rbac.yaml` | the roles its controller needs |
| `crd/DESIGN.md` | why the API is shaped this way, and what it deliberately leaves out |
| `examples/` | two complete campaigns: the undisturbed comparison and its registry-drift version |
| `experiments/specs/` | the workload and intent specs, the four registry variants (`r0` to `r3`), and the ten cluster-state scenarios |
| `experiments/templates/` | the Jinja templates the specs render into Kubernetes Jobs |
| `experiments/orchestrator/` | the campaign executor: rendering, submission, collection |
| `experiments/orchestrator/analysis/metrics/` | how each metric is computed, offline, from stored artifacts |
| `experiments/orchestrator/calibration/`, `experiments/calibration/` | rendering, submission and reduction of the iperf3 probes that measure inter-member round-trip time, and the probes' Job template |
| `experiments/iperf3-server/` | the receiving end those probes and the network workload class talk to |
| `experiments/preflight/` | cross-cluster reachability and certificate checks to run before a campaign |
| `experiments/db/` | dump and restore of the decision history, so every policy starts from the same one |

Keep the `experiments/` prefix: module paths, the CLI's default spec locations
and the documented `python -m experiments.orchestrator.cli` invocations all
resolve against it.

If you want to check one thing, check
`experiments/orchestrator/analysis/metrics/intent_compliance.py`. It is where a
placement is graded, and it grades against the federation's canonical
classification, not against what the policy believed. A stale description shows
up as a failure only when placements are graded this way.

The paper's three metrics are computed by the modules below. Each is written per
run to `analysis/summary.json`, and `reduce_cells.py` reduces it over the `k`
runs of a cell.

| paper | module | key in `summary.json` |
|---|---|---|
| intent compliance (hard tier, completion-aware) | `metrics/intent_compliance.py` | `hard_pass_rate_completion_aware` |
| time to pod spawn | `metrics/completion_decomposition.py` | `median_wait_s` |
| cordon response | `metrics/cordon_response.py` | `during_cordon_strict_fraction_targeting_cordoned` |

Intent compliance is completion-aware: the denominator is every submitted
measured job, and a job that did not complete counts as non-compliant. Time to
pod spawn runs from the moment `kubectl apply` returned for the Job, taken on the
operator's machine, to the Job's `status.startTime` as the control plane reports
it, so the two clocks should be synchronised with NTP. Pod scheduling, image pull
and container start happen after `startTime` and are not part of it.

## Characterising your own setup

Comparing placement policies presupposes knowing what you are placing onto, and
a federation assembled from whatever hardware you have is not self-describing.
The tooling we used to characterise ours is here too, and it is probably the most
useful part for someone whose federation looks nothing like ours.

**Reachability, before anything else.** `experiments/preflight/` probes whether
members can reach each other. `check-net-job-based.ps1` writes one `Job` per
target through the federation's entry point and lets Karmada propagate it,
instead of writing a Pod into each member context directly. Writing Pods
directly needs LAN or VPN routes from your workstation. The job-based check needs
no extra routes, but it does need a placement component that honours the
`delphi.experiments/pin-to-cluster` annotation (ours is the unpublished
middleware). Without one, use `check-net-routes.ps1` or
`check-net-route-public-cloud.ps1`. `check-karmada-certs.ps1` reports certificate
expiry, a failure that is easy to mistake for a network problem.

**Round-trip time, measured.** `experiments/orchestrator/calibration/` renders a
small set of pinned iperf3 probes from `experiments/calibration/rtt-probe.yaml.j2`,
submits them, and reduces their JSON output into per-member figures.
`experiments/iperf3-server/` is the receiving end, and the network workload class
talks to the same deployment. The result is a table like
`experiments/specs/cluster-rtt-measured.yaml`, which records what the probes
observed. The categorical `rtt` labels in the cluster registry are declared
classes, and the measured table is one input to them. On our testbed the
public-cloud member measured below its declared class, because it sits on the
same network as the probe target, and the registry keeps the declared class; the
comments in `experiments/specs/cluster-registry.yaml` explain why. An intent's
latency ceiling is checked against these labels, so on another federation they
have to be declared for that federation instead of copied from ours.

**A frozen decision history.** A policy that reads its own past decisions is not
comparable to one that does not unless both start from the same past.
`experiments/db/` snapshots the decision store into a versioned dump and
restores it before each measured run, so every policy reads the same past
whatever order you run them in.

None of this produces results, but the measurements depend on all of it, and
tooling like this is rarely published.

## What is not here

**Measured results.** No measurements are published here. The paper's values come
from validated runs, three per cell, of which the paper reports the median; their
run directories are not part of this release. The metric code is complete, so you
can score a campaign of your own.

**Contention campaigns.** The scenario model expresses background load in the two
shapes described above, and `experiments/specs/cluster-states/` holds ten
declarations of it; the campaigns in the paper use the `idle` state.

**Network topology.** Locality enters as a declared cluster class. Nothing here
emulates or injects a network topology.

**The replay tests for measured runs.** Tests that re-score recorded campaigns
offline need those run directories and contain their values, so they are left out
too.

**Analysis of multi-state runs.** `render --multistate-states` renders one
campaign over several cluster states, but only for `idle` and the four
disturbance scenarios; the five pinned saturations are rendered by
`render-bootstrap` only. The analyzer scores single-state runs, which is what the
reported campaigns are. Given a multi-state run, it stops with an explicit error
instead of returning empty metrics.

**The middleware, the reconciler and the workload images.** The placement
middleware, the `Benchmark` reconciler and the operator scripts that apply a
registry variant to the middleware are out of scope, as said above. The templates
in `experiments/templates/` define the Jobs, but the task-bound behaviour they
request (`WORK_MODE=ops` and the per-class operation counts) is implemented by the
stressor images they reference, and those images are not published either.

One file here might look like a measurement:
`experiments/specs/cluster-rtt-measured.yaml` holds round-trip times taken
between the members. It stays because it is an input to the declared `rtt`
classes, and without it those classes look arbitrary. LAN-only addresses are left
out. The tooling that produced the file is in the repository, so on another
federation you should regenerate it.

## Running it

The tests need no cluster, no database and no credentials. From the repository
root:

```
pip install -r experiments/orchestrator/requirements.txt
python -m pytest experiments/ -q
```

All 236 tests pass. The plot generators are tested against fabricated fixtures
committed next to the script that produces them
(`experiments/orchestrator/analysis/plots/tests/fixtures/`), so you can re-run the
generator and diff its output against the committed files. That directory's
README says plainly what those numbers are: invented to exercise the plots, and
not to be cited.

Submitting a campaign needs a federation and a placement component. Ours was four
single-node Kubernetes v1.36.4 members behind a Karmada v1.12.2 control plane.
Each foreground Job declares its intent in the `optimize-propagation` and
`propagation-requirements` annotations, and Karmada places it only after a
placement component has turned them into a `PropagationPolicy`. Background load,
disturbance Jobs and the calibration probes need a component that honours the
`delphi.experiments/pin-to-cluster` annotation. Our placement component is the
unpublished middleware, and the policy under test is chosen in its deployment.
The orchestrator reaches Karmada through `kubectl`, by default with the context
`unique-logical-entrypoint` and the namespace `delphi-experiments`.

A campaign runs in two phases. Phase A runs once and builds the seed history,
which `experiments/db/` then dumps:

```
python -m experiments.orchestrator.cli render-bootstrap --spec experiments/specs/bootstrap.yaml --run-id <bootstrap-run-id>
python -m experiments.orchestrator.cli submit --run-id <bootstrap-run-id> --i-know-this-runs-real-jobs
```

Phase B restores that dump before every measured run (see
`experiments/db/README.md`). Each run is then:

```
python -m experiments.orchestrator.cli render --spec experiments/specs/effectiveness-taskbound.yaml --run-id <run-id> --baseline <Static-Karmada|Heuristic-Scorer|DELPHI-Full>
python -m experiments.orchestrator.cli submit --run-id <run-id> --i-know-this-runs-real-jobs
python -m experiments.orchestrator.cli analysis --run-dir experiments/runs/<run-id>
```

`--baseline` records which policy the middleware is running; it does not change
it. For the cordon campaign, pass
`--cordon-window experiments/specs/cordon-window.yaml` to `render`. `submit` then
cordons the target member with `karmadactl`, which must be on `PATH`.

Before rendering, every run directory, the bootstrap one included, needs
`experiments/runs/<run-id>/images.json`. It is a JSON object whose keys
`stress_ng` and `net_stresser` give the full references of the stressor images in
your registry. Use an immutable tag, so that every node runs the same image.
Every Job carries its run ID as a label, and Kubernetes limits label values to 63
characters, so keep `--run-id` within that. The on-prem node that the network
workload class and the preflight probes target appears as the documentation
address `203.0.113.10` (`TARGET_HOST` in the specifications, `-OnPremNodeIp` in
the preflight scripts). Replace it with your node's address.

A finished run can be scored again offline, without a cluster or a database:

```
python -m experiments.orchestrator.cli analysis --run-dir experiments/runs/<run-id> --no-db --no-kube
```

## Reproducing a campaign

Each run keeps its rendered job manifests, its planned timeline, and the
placement and status the control plane reported for every job. The three
reported metrics are recomputed from these files instead of from the live
cluster, so a run can be re-scored long after it ran. The analyzer's other
outputs (decision latency, decision traceability and the no-pollution check) read
the decision store and need the database.

## Status and licence

This is research code released to accompany a paper, not a supported product. The
API is `v1alpha1` and is expected to change.

The scenario specifications and the example campaigns (`experiments/specs/`,
`examples/`), the measured round-trip table among them, are released under the
Creative Commons Attribution 4.0 International licence
(`LICENSES/CC-BY-4.0.txt`). Everything else, the code and its documentation, is
released under the MIT licence (`LICENSE`, `LICENSES/MIT.txt`).

## Citing

If you use this artifact, please cite the paper:

```bibtex
@inproceedings{ScaffidiMilitone2026BenchmarkingIntentDrivenPlacement,
  author    = {Scaffidi Militone, Gabriele and Apiletti, Daniele and Malnati, Giovanni},
  title     = {Benchmarking Intent-Driven Placement Across the Federated Computing
               Continuum: A Declarative Environment and Evaluation Campaigns},
  booktitle = {4th International Workshop on Middleware for the Computing Continuum (Mid4CC '26)},
  year      = {2026},
  publisher = {ACM},
  doi       = {10.1145/3861040.3862572}
}
```
