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
with a real two-way call. See **Verifying traffic flow yourself** below for
the exact commands to watch it happen live - that's more useful than a
recording, since you control the pace.

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
# edit .env.local: fill in LIVEKIT_API_SECRET, STUNNER_PASSWORD, STUNNER_HOST_IP, e.g.
#   LIVEKIT_API_SECRET=$(openssl rand -base64 32)
#   STUNNER_PASSWORD=$(openssl rand -base64 24)
#   STUNNER_HOST_IP=$(ipconfig getifaddr en0)   # your real LAN IP - see note below

set -a; source .env.local; set +a
```

`STUNNER_HOST_IP` is not a secret, but it **must** be your machine's real LAN
IP, not `127.0.0.1`/`localhost` - see the callout under **Running a call**
below for why this one is load-bearing for browser clients.

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

## Verifying traffic flow yourself

Run these in separate terminals while the `lk room join` commands above are
active, to watch real traffic move between the client, STUNner and LiveKit
at your own pace (a recording can't substitute for this - run it live):

**Terminal 1 - STUNner's own logs** (allocations, permission checks):
```bash
kubectl --context kind-stunner-livekit -n stunner logs -f deploy/udp-gateway
```

**Terminal 2 - LiveKit's own logs** (participants joining, tracks publishing):
```bash
kubectl --context kind-stunner-livekit logs -f deploy/livekit-server
```

**Terminal 3 - STUNner's relay counters, polled every second:**
```bash
kubectl --context kind-stunner-livekit -n stunner port-forward deploy/udp-gateway 8080:8080 &
watch -n1 'curl -s http://localhost:8080/metrics | grep ^stunner_'
```
`stunner_allocations_active` and the `stunner_cluster_bytes_total`/
`stunner_cluster_packets_total` counters should rise from `0` as soon as the
call starts, and climb continuously while it's active.

**Terminal 4 - make the call** (the commands from **Running a call** above).

What to look for: Terminal 1 shows `stunnerd` creating TURN allocations and
permissions for the call, and denying any `CreatePermission` request for an
address outside the `UDPRoute`'s backend. Terminal 2 shows LiveKit's
`participant active` / `mediaTrack published` / `track subscribed` lines for
both `caller` and `agent`. Terminal 3's counters move in lockstep with the
call. All three are independent, live evidence - not scripted output.

You'll also see `ERROR ... failed Simulcast probing` lines in Terminal 2 -
that's `lk`'s `--publish-demo` synthetic video track hitting a harmless pion
quirk (an SSRC it can't map to a simulcast layer), nothing to do with STUNner
or the relay. Terminal 3's counters keep climbing through it.

> **Setting up a browser client? Two things are required, not optional:**
> 1. Force relay-only ICE on `room.connect()`'s own `rtcConfig` -
>    `{ rtcConfig: { iceTransportPolicy: 'relay' } }` - not on `new Room()`,
>    which `livekit-client` only partially honors. Without this, the browser
>    also offers its own LAN-IP host candidate, and ICE can nominate that
>    pair instead of the relay - it'll look "connected" but carry no media.
> 2. `STUNNER_HOST_IP` must be a real LAN IP, never `127.0.0.1`. Chrome's
>    Local/Private Network Access restrictions block ICE from even
>    attempting TURN gathering against a loopback address - silently, no
>    error. The `lk` CLI isn't a browser, so it never hits either of these.

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

Verified working end-to-end with real audio crossing the relay - see the
callout at the end of **Running a call** for the two settings this depends
on (`STUNNER_HOST_IP` as a real LAN IP, and relay-only ICE).

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
