"""RTT calibration subpackage — iter-4b.

Renders, submits, and reduces a small set of pinned iperf3 probes that
measure source -> on-prem RTT for paper credibility. The analyzer reads
the categorical rtt label in experiments/specs/cluster-registry.yaml, so
these measurements only ever appear in the paper's testbed table and
Discussion/Limitations — they never alter placement behaviour.

The subpackage intentionally does not depend on experiments.orchestrator.analysis
(iter-4a) so it can be developed and merged independently. Where the
calibration outputs would naturally feed iter-4a (e.g. a future column in
the analyzer's testbed dump), the integration lives in iter-4a, not here.
"""
