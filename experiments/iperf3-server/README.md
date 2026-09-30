# iperf3-server — network target for the DELPHI experiments harness

## Purpose

Single Deployment + NodePort Service that serves as the receiving end
of network-class experiment Jobs (`net-stresser` clients running on
`edge-1`, `edge-2`, `public-cloud`). Lives in the dedicated
`delphi-experiments-support` namespace on `on-prem` (class E per
a support namespace, so it is never a measured target).

Image source: `code/stressors/network/iperf3-server/` — operator
builds and pushes via the existing
`scripts/build/build_and_push.sh` (bash, called from WSL).

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

Server-side validate first:

```powershell
kubectl --context on-prem apply --dry-run=server -f experiments\iperf3-server\
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

Automated sessions do not `kubectl delete deploy/svc` in this
namespace (the development setup's kubectl guard blocks destructive verbs
on these resources too).
The operator removes the Deployment, Service, and optionally the
Namespace from their own shell when the experiments suite is done:

```powershell
kubectl --context on-prem delete -f experiments\iperf3-server\service.yaml
kubectl --context on-prem delete -f experiments\iperf3-server\deployment.yaml
# optional — only if no other class-E resources will be added later
kubectl --context on-prem delete -f experiments\iperf3-server\namespace.yaml
```

## Image build (operator)

The image is built from `code/stressors/network/iperf3-server/`:

```bash
# from WSL, in the repo root, after docker login:
docker buildx build \
    --platform linux/amd64 \
    -t <your-registry>/iperf3-server:dev \
    --push \
    code/stressors/network/iperf3-server/
```

amd64-only is sufficient because the server only runs on on-prem.
Edge clients use the multi-arch `net-stresser` image built
separately (see `code/stressors/network/net-stresser/README.md`).

## Health check

Once deployed, the operator can quickly verify the server is
listening:

```powershell
# port-forward and probe locally:
kubectl --context on-prem port-forward -n delphi-experiments-support svc/iperf3-server 31521:5201
# in another terminal:
iperf3 -c localhost -p 31521 -t 5
```

For cross-cluster reachability validation (the actual blocker for
iter-2b), see `experiments/preflight/check-net-routes.ps1`.
