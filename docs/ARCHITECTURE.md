# Architecture

## The core trick: crypto-twin nodes

A WireGuard/AmneziaWG client config pins a single `Endpoint` and a single server
`PublicKey`. To let one config reach many servers, every node in the fleet runs
with the **same** server private key and the **same** set of peers. From a
client's point of view there is one server that happens to answer on several IPs.

There is no per-node key negotiation to coordinate, and every client shares
the one bare domain — no per-client subdomains. What tells clients apart is
the **port**: DNS queries are anonymous, so identity can't live in the
hostname resolution, but every packet that reaches any node carries the
client's personal endpoint port, and that is enough to steer them.

## State

`state.json` is the single source of truth (git-ignored, it holds secrets):

- the shared server keypair and obfuscation parameters (`Jc/Jmin/Jmax/S1/S2/H1..H4`);
- the tunnel subnet, MTU, listen port, DNS;
- the list of nodes (host + SSH credentials + region + capacity weight);
- the list of clients (keypair + allocated `/32` + pinned node + personal port).

Address allocation is deterministic: the gateway takes the first host of the
subnet, clients take the lowest free `/32` after it.

## Modules

| Module | Responsibility |
|---|---|
| `keys.py` | Curve25519 keypairs, pre-shared keys, AmneziaWG obfuscation params |
| `models.py` | `Server`, `Client`, `FleetConfig` dataclasses |
| `state.py` | Atomic load/save of `state.json` |
| `render.py` | Render server/client AmneziaWG configs and the per-node steering rules |
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

## Client pinning: one domain, a port per client

A new client is pinned to the node with the fewest clients per unit of
capacity `weight` (ties broken by live load) and gets endpoint
`<domain>:<steer_port_base + address offset>`. Every node then runs the same
steering rule set (dedicated iptables chains, rebuilt idempotently):

- a port pinned **here** → `REDIRECT` to the real listen port (answered locally);
- any other port → `DNAT` to its owner's node (+ `MASQUERADE`, so replies
  return along the same path).

So whichever node DNS hands a client, their tunnel terminates on — and their
traffic egresses from — their own node. Reconnects and even mid-session DNS
moves keep the same public IP, which is what keeps IP-bound sessions (banks,
logins) alive. The relay sees only AmneziaWG UDP; the crypto is end-to-end
between client and the shared server identity.

The rules ride with `awg0.conf` on every sync (`steer.sh`, re-applied by
PostUp after a reboot). The controller overlays failover on each pass: a
pinned node that misses two probes gets its clients' ports repointed at the
lightest live node — the only moment a client's egress IP can change — and
pointed home again on recovery. Rules are pushed only to nodes whose script
actually changed, so a steady fleet costs zero SSH calls per pass.

## Steering loop (DNS)

DNS's remaining job is picking a live, light *entrance*:

```
every health_interval seconds:
    probes  = [probe(node) for node in enabled nodes]      # concurrent
    score   = EWMA(cpu load) + USER_WEIGHT * connected users, per node
    primary = current published node
    if primary missed 2 probes:            primary = best healthy node
    elif another node is lighter by GAP:   primary = that node
    elif primary overloaded and a node is lighter by GAP: shed to it
    reconcile A(domain) == {primary}                       # exactly one record
    reconcile steering rules on every live node            # see pinning above
```

The gap plus the EWMA is the hysteresis: a load wobble never bounces the
record, and a record move never breaks anyone — packets that land on the new
entrance are relayed to each client's pinned node like any others. The record
is never published empty while any node is alive: a slightly stale record
still routes, an empty record does not.

On startup the controller sweeps up `nX.<domain>` records left behind by the
old per-client-subdomain scheme; clients issued under that scheme (or before
pinning) keep working as plain domain-followers and get pinned when their
config is re-issued.

## Where it can grow

- Weighted steering via a Cloudflare Load Balancer pool, or a controller that
  adjusts weights instead of membership.
- Geo steering once nodes span regions (nearest healthy node per client).
- A read-only status API / small dashboard over the controller.
- Host-key pinning for SSH once node IPs are stable.
- Pluggable DNS providers behind the `cloudflare.py` interface.
