# awg-fleet

**One AmneziaWG config. A whole fleet of servers behind it.**

You hand out client configs on a single shared domain — no subdomains, no
"pick a server" list. Each client is pinned to a node at creation, weighted by
the node's capacity, and their traffic **always** egresses from that node's IP:
whichever node DNS happens to hand them, the fleet recognizes the client by the
personal port in their endpoint and relays them home. Reconnects, roaming, DNS
moves — websites keep seeing the same address, so IP-bound sessions survive.
Only if a client's node actually dies is their port repointed at a live node
(and pointed home again on recovery), without touching their config.

```
              vpn.example.com  (one A record: a live, light entrance)
                                    │
        client :40007  ─────────────┤ any node recognizes the port
                                    ▼
        ┌──────────┐          ┌──────────┐          ┌──────────┐
        │  node 1  │ ◄─relay─ │  node 2  │          │  node 3  │  ← crypto-twins:
        │ AmneziaWG│          │ AmneziaWG│          │ AmneziaWG│    same server keys,
        └────┬─────┘          └──────────┘          └──────────┘    same peer list
             ▼ egress: always node 1's IP for client :40007
```

## Why this exists

WireGuard and AmneziaWG are point-to-point: a client config pins one endpoint
and one server public key. You cannot just round-robin a domain across servers
that each hold their own key, because the handshake with the "wrong" key fails.

awg-fleet solves it the way commercial WireGuard providers do: every node in the
fleet shares the **same** server keypair and the **same** peer list. Now a client
whose `Endpoint = vpn.example.com` can hand-shake with any node — so pointing the
domain at a different node is all it takes to send new connections there.

## What it does

- **One domain for everyone.** Every client points at the same bare domain;
  the only per-client bit is the port, which is how the fleet tells them apart
  (DNS can't — a DNS query is anonymous).
- **Capacity-weighted placement.** A new client lands on the node with the
  fewest clients per unit of measured capacity, ties broken by live load. The
  weight is not a knob you guess: each node's cores and real network speed are
  benchmarked at add time and re-measured weekly (Monday 00:00 Krasnoyarsk),
  so a gigabit box automatically takes ~10x the clients of a 100 Mbit one.
- **One stable egress IP per client.** Wherever DNS sends a client, the
  receiving node answers them if they're pinned there and relays them to their
  node otherwise — so their sessions, logins and IP-bound state never notice a
  reconnect.
- **Automatic failover.** Two missed probes and a dead node's clients are
  repointed at a live one within a pass — configs untouched; they return home
  when the node recovers.
- **MTU-safe by construction.** Tunnel MTU is pinned low and every node clamps
  TCP MSS to the path, so large packets (media, video, QUIC) never black-hole
  behind a reseller that quietly delivers a sub-1500 path.
- **Client bundles.** `client add` writes a `.conf`, a QR PNG and a link, ready
  for the AmneziaWG / AmneziaVPN app.

## Quickstart

```bash
pip install -e .

export CF_API_TOKEN=...            # Cloudflare token, Zone:DNS:Edit on the zone

# 1. create the fleet identity (shared server key + obfuscation)
awgfleet init --domain vpn.example.com --zone-id <cloudflare-zone-id>

# 2. add nodes (installs AmneziaWG and pushes the shared config)
awgfleet server add --name de1 --host 91.219.23.166 --password '...' --region de
awgfleet server add --name de2 --host 95.85.231.46  --password '...' --region de

# 3. add a client, get its config bundle
awgfleet client add iphone            # -> clients/iphone.conf, .png (QR), .link

# 4. run the steering controller (health-check + DNS reconcile, forever)
awgfleet run
```

`awgfleet status` shows, in one shot, which nodes are up, their load, and who is
currently published to the domain.

## Commands

| Command | What it does |
|---|---|
| `awgfleet init` | Create `state.json` with a fresh shared server identity |
| `awgfleet server add/rm/list` | Join, drain, or list nodes |
| `awgfleet server bench [name]` | Re-measure node capacity now and refresh weights |
| `awgfleet client add/rm/list` | Create or revoke a client and mirror it to every node |
| `awgfleet client move <name> <server>` | Repin a client to another node (egress IP changes to it) |
| `awgfleet sync` | Re-push the current shared config to all nodes |
| `awgfleet status` | Probe the fleet and show the current rotation |
| `awgfleet run` | Run the steering controller loop |

## How steering decides

Placement happens once, when a client is created: they're pinned to the node
with the fewest clients per unit of capacity weight (ties broken by live
load), and their endpoint becomes `domain:personal-port`.

The weight comes from a real benchmark, not configuration: at add time and
weekly (Monday 00:00 Krasnoyarsk) each node's core count and actual
up/download speed are measured (`speed.cloudflare.com` via curl, ~30s), and
`weight = (0.7·down + 0.3·up)/100 · cpu-factor` — a plain 100 Mbit single-core
VPS is the 1.0 baseline. A failed measurement keeps the previous weight.

Each controller pass (`health_interval`, default 30s):

1. probe every node: TCP liveness, normalized `loadavg`, connected clients;
2. keep every node's steering rules current: a node answers the ports of
   clients pinned to it and relays every other port to its owner's node
   (plain DNAT — the tunnel crypto is end-to-end, relays can't see inside);
   rules are only pushed when they actually change;
3. if a pinned node is down (two missed probes), its clients' ports are
   repointed at the lightest live node — their egress IP changes, which is the
   one case nothing can prevent — and pointed home when it recovers;
4. keep the domain's single A record on a live, light node (grey-cloud, low
   TTL, moved with hysteresis, never published empty) — it only decides which
   node *receives* a client's packets, not which node carries them.

## Honest limitations

- **Shared keys** mean compromise of one node exposes the tunnel. Fine for a
  personal or small-team fleet; not a multi-tenant isolation model.
- **Relaying costs a hop.** When DNS hands a client a node other than their
  own, that node forwards the UDP to the right one: a few extra milliseconds
  and double bandwidth on the entry node for that client's traffic.
- **Placement is per-client, not per-packet.** The fleet balances by putting
  people on the right node up front (and by capacity weight), not by moving a
  live client around — moving them would change their IP, which is exactly
  what pinning exists to avoid.
- **Provisioning targets Ubuntu 22.04 / 24.04** via the AmneziaWG PPA. Other
  distros need the install block in `provision.py` adjusted.
- **Health is indirect.** UDP AmneziaWG cannot be hand-shaken without keys, so a
  reachable SSH/health port stands in for "node is up."
- `state.json` and `clients/` hold private keys and SSH credentials. They are
  git-ignored; keep them somewhere safe.

## Status

Early but real: the shared-key model, config rendering, client lifecycle,
Cloudflare steering and the controller loop are implemented and unit-tested. The
provisioning path has been exercised against Ubuntu 24.04. Treat it as a v0.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design in depth.

## License

MIT. See [LICENSE](LICENSE).
