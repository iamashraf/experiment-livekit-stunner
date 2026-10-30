# experiment-livekit-stunner

A local proof-of-concept for running [LiveKit](https://livekit.io/) behind the
[STUNner](https://github.com/l7mp/stunner) WebRTC gateway inside Kubernetes,
based on STUNner's own [LiveKit example](https://github.com/l7mp/stunner/blob/main/docs/examples/livekit/README.md).

## The question this answers

A common belief is that WebRTC media servers (LiveKit, FreeSWITCH, Janus, ...)
can't run as ordinary Kubernetes workloads, because their UDP media ports need
to be directly reachable - which usually means `hostNetwork: true` or one
dedicated public IP per pod. STUNner removes that requirement: clients are
told (via LiveKit's `turn_servers` config) to always reach the media server
through STUNner's TURN relay. The media server itself becomes a completely
ordinary pod - no `hostNetwork`, no dedicated IP, no special treatment at all.
Only STUNner's own gateway needs to be reachable, and it needs that **once**,
regardless of how many media-server pods sit behind it.

This repo stands up that architecture locally (kind + Podman) and proves it
with a real two-way call.

## Architecture

```
 browser / lk CLI
       |
       |  ws:// signaling               UDP/TURN media
       v                                      v
 +-----------+                        +----------------+
 | Ingress   |                        | STUNner Gateway |
 | (nginx)   |--- HTTP/WS (7880) ---> |  (stunnerd)     |
 +-----------+        |               +----------------+
                       v                       |
                +----------------+             | relayed UDP only,
                | livekit-server |<------------+  restricted by UDPRoute
                | (ordinary pod, |
                |  no            |
                |  hostNetwork)  |
                +----------------+
                       |
                +----------------+
                |     redis      |
                +----------------+
```

Key points proven by this setup:

- `livekit-server` runs as a plain pod: `hostNetwork: false`, no NodePort, no
  dedicated IP. Confirmed via `kubectl get pod -o jsonpath='{.spec.hostNetwork}'`
  (empty = default/false) throughout every test in this repo.
- STUNner's dataplane (`stunnerd`) is exposed via a standard Kubernetes
  `Service type=LoadBalancer`, provisioned by [MetalLB](https://metallb.io/)
  (so the `Gateway` resource gets a real external IP, exactly as it would on
  EKS/GKE/AKS), with its `NodePort` pinned to `3478` via STUNner's
  `stunner.l7mp.io/nodeport` annotation. **No `hostNetwork` anywhere.**
- LiveKit's signaling (the WebSocket join handshake) goes through a normal
  `Ingress` (`ingress-nginx`, deployed the standard `kind`-documented way with
  `hostPort`, not a port-forward).
- The `UDPRoute` in `stunner` namespace restricts the relay's backend to only
  the `livekit-server` Service - proven in the logs: STUNner actively denies
  `CreatePermission` requests to any other address.

## Local environment

Built on a Mac, using:
- [Podman](https://podman.io/) (rootless machine) as the container runtime
- [`kind`](https://kind.sigs.k8s.io/) (`KIND_EXPERIMENTAL_PROVIDER=podman`) for the Kubernetes cluster
- [Helm](https://helm.sh/) to install STUNner
- [MetalLB](https://metallb.io/) for real `LoadBalancer` IPs inside `kind`
- [`ingress-nginx`](https://kubernetes.github.io/ingress-nginx/) (kind's `hostPort` deploy manifest)
- [`livekit-cli`](https://github.com/livekit/livekit-cli) (`lk`) as the verified test client

### Known local quirks (not present on a real cluster)

- `kind`'s Podman nodes run inside a Podman VM; the Mac host can only reach
  ports explicitly declared in `kind-cluster.yaml`'s `extraPortMappings`
  (`80`, `443`, `3478/udp`). This is why STUNner's `NodePort` had to be pinned
  to `3478` - it rides the pre-declared port mapping.
- Kubernetes' default NodePort range (`30000-32767`) doesn't include `3478`,
  so the cluster's `kube-apiserver` was started with
  `--service-node-port-range=3478-32767` (see `kind-cluster.yaml` / setup
  notes below).

None of this exists in a real cloud cluster, where STUNner's `Service
type=LoadBalancer` just gets a normal routable IP directly.

## Files

| File | Purpose |
|---|---|
| `.env.example` | Required env vars, empty/placeholder values - copy to `.env.local` (gitignored) and fill in |
| `kind-cluster.yaml` | kind cluster config: UDP 3478 + TCP 80/443 port mappings, ingress-ready node label |
| `livekit-call-stunner.yaml` | STUNner `GatewayClass`/`GatewayConfig`/`Gateway`/`UDPRoute` (NodePort pinned to 3478) |
| `livekit-server-minimal.yaml` | `livekit-server` + `redis`, ordinary pods, no Ingress/TLS baked in |
| `livekit-ingress.yaml` | Plain HTTP `Ingress` exposing LiveKit's signaling port |
| `metallb-config.yaml` | MetalLB `IPAddressPool` + `L2Advertisement` for the kind network |
| `token_server.py` | Minimal LiveKit JWT issuer (mints real tokens on request, signed with `$LIVEKIT_API_SECRET` - see **Secrets** below) |
| `ui/index.html` | Browser UI: audio-only call, live per-participant audio-level meters, fetches tokens from `token_server.py` on demand |

## Secrets

Nothing in this repo contains a real secret value. `livekit-server-minimal.yaml`
and `livekit-call-stunner.yaml` reference `${LIVEKIT_API_SECRET}`,
`${STUNNER_USERNAME}` and `${STUNNER_PASSWORD}` as placeholders, applied with
`envsubst` (the same pattern STUNner's own upstream docs use for
`${STUNNER_IP}`/`${INGRESS_IP}`).

`.env.example` documents the required variables with empty/placeholder
values and is the only one committed. Copy it to `.env.local` (gitignored),
fill in real values, and source it in every shell that applies the
manifests, runs `token_server.py`, or calls `lk` - they all need to agree:

```bash
cp .env.example .env.local
# edit .env.local: fill in LIVEKIT_API_SECRET and STUNNER_PASSWORD, e.g.
#   LIVEKIT_API_SECRET=$(openssl rand -base64 32)
#   STUNNER_PASSWORD=$(openssl rand -base64 24)

set -a; source .env.local; set +a
```

## Setup

```bash
# tooling
brew install podman kind helm mkcert livekit-cli

# cluster
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name stunner-livekit --config kind-cluster.yaml

# widen the NodePort range so 3478 is allowed (see "known local quirks" above)
podman exec stunner-livekit-control-plane sh -c \
  "sed -i '/- kube-apiserver/a\\    - --service-node-port-range=3478-32767' /etc/kubernetes/manifests/kube-apiserver.yaml"

# Gateway API + STUNner CRDs and chart
kubectl apply --server-side --force-conflicts \
  -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.2/experimental-install.yaml
kubectl apply -f https://raw.githubusercontent.com/l7mp/stunner-helm/main/helm/stunner/crds/stunner-crds.yaml
helm repo add stunner https://l7mp.io/stunner && helm repo update
helm install stunner stunner/stunner --create-namespace --namespace=stunner-system --skip-crds

# MetalLB
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml
kubectl apply -f metallb-config.yaml

# STUNner Gateway/UDPRoute, LiveKit, Ingress (envsubst fills in the secrets exported above)
kubectl create namespace stunner
envsubst < livekit-call-stunner.yaml | kubectl apply -f -
envsubst < livekit-server-minimal.yaml | kubectl apply -f -
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
kubectl apply -f livekit-ingress.yaml
```

## Running a call (verified)

```bash
ROOM="demo-$(date +%s)"
lk room join "$ROOM" --identity caller --url http://localhost \
  --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --publish-demo --auto-subscribe --yes &
lk room join "$ROOM" --identity agent --url http://localhost \
  --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET" \
  --publish-demo --auto-subscribe --yes &
```

Then watch STUNner's relay counters move:

```bash
kubectl -n stunner port-forward deploy/udp-gateway 8080:8080 &
curl -s http://localhost:8080/metrics | grep ^stunner_
```

### What this proved, repeatedly, in this session

- `stunner_allocations_active` rose from `0` to `4`+ as soon as the call
  started (one TURN allocation per media connection).
- `stunner_cluster_bytes_total` (STUNner's relay-to-backend counter) climbed
  in lockstep with the call, from a true baseline of `0`.
- Both participants' `"track subscribed"` log lines showed each side
  receiving the *other's* track.
- STUNner's own logs show it **denying** `CreatePermission` requests to
  addresses outside the `UDPRoute`'s backend (e.g. a host candidate on the
  test machine's LAN IP) - proof it's enforcing the allow-list, not acting as
  an open relay.
- `livekit-server`'s pod spec has no `hostNetwork` field set, throughout.

## Browser UI + token server

`ui/index.html` is a minimal, dependency-light call page: enter an identity
and room, click Connect, and it fetches a real token from `token_server.py`
(a ~90-line stdlib HTTP server that signs LiveKit JWTs with `$LIVEKIT_API_SECRET`)
before joining. No pre-baked tokens, no TLS needed (both the page and the
LiveKit signaling endpoint are plain `http://localhost`, which browsers treat
as a secure context for `getUserMedia`). It's audio-only by design, with a
live per-participant audio-level meter instead of video, since the point is
proving the media *path*, not demoing a video UI.

```bash
python3 -m http.server 8081 --directory ui &
python3 token_server.py &   # requires LIVEKIT_API_SECRET exported, see Secrets above
```

Open two tabs:
- `http://localhost:8081/?identity=caller&room=uicall`
- `http://localhost:8081/?identity=agent&room=uicall`

This UI path is manual/human-driven only for now; an automated headless-browser
test was attempted and removed because it didn't reliably exercise the real
relay path (it's a harder problem than it looks - two ICE agents running on
the same machine under headless Chromium don't behave the same as two real
browsers on separate networks). The CLI path above is the one with repeated,
verified proof.

## Local-only quirks vs. a real cluster

Everything above maps directly onto a real cloud cluster, with these local
substitutions removed:
- STUNner's `LoadBalancer` Service would get a real routable IP from the
  cloud provider - no NodePort-pinning or NodePort-range widening needed.
- The `Ingress` would sit behind a real cloud load balancer / DNS name - no
  `kind` `hostPort` tricks needed.
- TLS would be real (cert-manager + a real domain), not skipped as it is
  here (this PoC deliberately runs everything over plain `http://localhost`,
  since `localhost` is a secure context for `getUserMedia` without TLS).
