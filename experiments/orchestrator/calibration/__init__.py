"""RTT calibration subpackage.

Renders, submits, and reduces a small set of pinned iperf3 probes that
measure source -> on-prem RTT. The analyzer reads the categorical rtt
label in experiments/specs/cluster-registry.yaml, so these measurements
document the testbed; they never alter placement behaviour.

The subpackage does not depend on experiments.orchestrator.analysis, so
the two can be developed and tested independently.
"""
