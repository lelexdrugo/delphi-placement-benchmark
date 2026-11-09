# iperf3-server — network target for the benchmark experiments

## Purpose

Single Deployment + NodePort Service that serves as the receiving end
of network-class experiment Jobs (`net-stresser` clients running on
`edge-1`, `edge-2`, `public-cloud`) and of the RTT calibration probes.
Lives in the dedicated `delphi-experiments-support` namespace on
`on-prem` (a support namespace, labelled
`delphi.experiments/class: support`), not a measured target.

The image is not published here: build it from the Dockerfile you use,
push it to your registry, and set the image reference in
`deployment.yaml`. The Deployment passes `PORT=5201` and probes the
server on container port 5201.

## Files

| File | Kind | Purpose |
|---|---|---|
| `namespace.yaml` | `Namespace` | Creates `delphi-experiments-support`. Idempotent. |
| `deployment.yaml` | `Deployment` | One replica. Set the image to your own registry before applying. |
| `service.yaml` | `Service` | NodePort 31521 → container port 5201. |

NodePort is intentional — ClusterIP would not be reachable from
member-cluster pods without a Karmada multi-cluster service mesh
(`ServiceImport`/`ServiceExport`), which is out of scope for this
research artifact. The trade-off: nodePort 31521 is reserved
globally on on-prem; coordinate with other tenants if collisions
arise.

## Deploy (operator runbook)

Validate first (client-side: a server-side dry-run of the whole directory fails until the namespace exists):

```powershell
kubectl --context on-prem apply --dry-run=client -f experiments\iperf3-server\
```

If validation passes, apply:

```powershell
kubectl --context on-prem apply -f experiments\iperf3-server\namespace.yaml
kubectl --context on-prem apply -f experiments\iperf3-server\deployment.yaml
kubectl --context on-prem apply -f experiments\iperf3-server\service.yaml
```

Wait for the pod to reach `Ready` (the deployment has TCP readiness
and liveness probes):

```powershell
kubectl --context on-prem get pods -n delphi-experiments-support -l app.kubernetes.io/name=iperf3-server -w
```

Record the on-prem node IP — net-stresser clients on member clusters
need it as `TARGET_HOST`:

```powershell
kubectl --context on-prem get nodes -o wide
```

The `INTERNAL-IP` of the on-prem node is the address experiment specs
should use (e.g. set in `experiments/specs/effectiveness.yaml`
parameter `network.TARGET_HOST`).

## Tear down (operator runbook)

Remove the Service, the Deployment and optionally the Namespace when
the experiments are done:

```powershell
kubectl --context on-prem delete -f experiments\iperf3-server\service.yaml
kubectl --context on-prem delete -f experiments\iperf3-server\deployment.yaml
# optional — only if no other support resources will be added later
kubectl --context on-prem delete -f experiments\iperf3-server\namespace.yaml
```

## Image build (operator)

Build and push the image from your iperf3-server Dockerfile:

```bash
# after docker login:
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -t <your-registry>/iperf3-server:<tag> \
    --push \
    <path-to-your-iperf3-server-build-context>
```

The server itself only runs on on-prem, but the RTT calibration probes
run the same image as an iperf3 *client* on the arm64 edge members, so
build it multi-arch. Network-class workload Jobs use a separate
multi-arch `net-stresser` image, which is not published here either.

## Health check

Once deployed, the operator can quickly verify the server is
listening:

```powershell
# port-forward and probe locally:
kubectl --context on-prem port-forward -n delphi-experiments-support svc/iperf3-server 31521:5201
# in another terminal:
iperf3 -c localhost -p 31521 -t 5
```

For cross-cluster reachability validation, see
`experiments/preflight/check-net-job-based.ps1` (or
`experiments/preflight/check-net-routes.ps1` when you have direct
routes to every member).
