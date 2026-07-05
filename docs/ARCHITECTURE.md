# Architecture

## The core trick: crypto-twin nodes

A WireGuard/AmneziaWG client config pins a single `Endpoint` and a single server
`PublicKey`. To let one config reach many servers, every node in the fleet runs
with the **same** server private key and the **same** set of peers. From a
client's point of view there is one server that happens to answer on several IPs.

The consequence is that steering becomes a pure DNS problem: whichever node IPs
are published under the domain are the ones new handshakes reach. There is no
per-node key negotiation to coordinate.

## State

`state.json` is the single source of truth (git-ignored, it holds secrets):

- the shared server keypair and obfuscation parameters (`Jc/Jmin/Jmax/S1/S2/H1..H4`);
- the tunnel subnet, MTU, listen port, DNS;
- the list of nodes (host + SSH credentials + region);
- the list of clients (keypair + allocated `/32`).

Address allocation is deterministic: the gateway takes the first host of the
subnet, clients take the lowest free `/32` after it.

## Modules

| Module | Responsibility |
|---|---|
| `keys.py` | Curve25519 keypairs, pre-shared keys, AmneziaWG obfuscation params |
| `models.py` | `Server`, `Client`, `FleetConfig` dataclasses |
| `state.py` | Atomic load/save of `state.json` |
| `render.py` | Render server and client AmneziaWG config text |
| `ssh.py` | Async SSH exec and SFTP upload (asyncssh) |
| `provision.py` | Install AmneziaWG on a node, push the shared config |
| `clients.py` | Allocate address, add/remove client, emit `.conf` / QR / link |
| `cloudflare.py` | Reconcile the domain's A records to a target IP set |
| `health.py` | TCP liveness and normalized load probes |
| `controller.py` | Rotation policy and the reconcile loop |
| `cli.py` | The `awgfleet` command line |

## Config is MTU-safe on purpose

Every node's `awg0.conf` pins `MTU = 1280` and its `PostUp` clamps TCP MSS to the
path MTU. This is deliberate: a low, obfuscation-friendly tunnel MTU plus MSS
clamping means large packets never fragment into a black hole, which is the exact
failure that makes "the VPN loads pages but never loads video" on resellers whose
real path MTU is below 1500.

## Steering loop

```
every health_interval seconds:
    probes  = [probe(node) for node in enabled nodes]      # concurrent
    alive   = [p for p in probes if p.alive]
    inrot   = [p for p in alive if p.load < load_threshold]
    if not inrot and alive:
        inrot = [least loaded of alive]                    # never go dark
    reconcile A(domain) == {p.host for p in inrot}
```

The set is never published empty while any node is alive: a slightly stale record
still routes, an empty record does not.

## Where it can grow

- Weighted steering via a Cloudflare Load Balancer pool, or a controller that
  adjusts weights instead of membership.
- Geo steering once nodes span regions (nearest healthy node per client).
- A read-only status API / small dashboard over the controller.
- Host-key pinning for SSH once node IPs are stable.
- Pluggable DNS providers behind the `cloudflare.py` interface.
