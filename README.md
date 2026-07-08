# awg-fleet

**One AmneziaWG config. A whole fleet of servers behind it.**

You hand out a single client config on a single domain — every client gets the
same endpoint, no subdomains, no "pick a server" list. Behind that domain sits
a pool of AmneziaWG nodes. awg-fleet health-checks them and keeps the domain's
one A record on whichever node is least loaded right now, so each new
connection lands on the lightest server — today that's node A, tomorrow node B.
If a node dies or gets blocked, the record moves within a minute and clients
roll onto another node without touching their config.

```
                    vpn.example.com  (one A record, low TTL,
                          │           always the lightest node)
              ┌───────────┴───────────┬──────────────────┐
              ▼                       ▼                  ▼
        ┌──────────┐            ┌──────────┐       ┌──────────┐
        │  node 1  │            │  node 2  │       │  node 3  │   ← crypto-twins:
        │ AmneziaWG│            │ AmneziaWG│       │ AmneziaWG│     same server keys,
        └──────────┘            └──────────┘       └──────────┘     same peer list
              ▲                       ▲                  ▲
              └── controller: probe load + move the record ──┘
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

- **One config for the whole fleet.** Every client points at the same bare
  domain, never at a node and never at a per-client subdomain.
- **Load balancing at connect time.** The domain always resolves to the least
  loaded node, so whoever connects next lands on the lightest server. As users
  pile onto it, the record moves on and the fleet fills evenly.
- **Automatic failover.** Two missed probes and the record is on another node;
  reconnecting clients follow it without touching their config.
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
| `awgfleet client add/rm/list` | Create or revoke a client and mirror it to every node |
| `awgfleet sync` | Re-push the current shared config to all nodes |
| `awgfleet status` | Probe the fleet and show the current rotation |
| `awgfleet run` | Run the steering controller loop |

## How steering decides

Each pass (`health_interval`, default 30s):

1. probe every node: TCP liveness, normalized `loadavg`, and how many clients
   are currently connected to it;
2. score each node: smoothed CPU load plus a weight per connected user, so a
   box that is busy on connections (not CPU) still reads as loaded;
3. publish the single lightest healthy node under the domain (grey-cloud,
   low TTL) — the record only moves when another node is lighter by a real
   margin, so it never flaps on a wobble;
4. if the published node misses two probes in a row, fail over to the best
   remaining node at once; if the only node left is overloaded, keep it
   published — the fleet never goes dark.

WireGuard keeps the resolved IP for the life of a session, so a client that is
**already** connected stays on its node; the record steers whoever connects
*next*. That is what balances the fleet: new connections land on the lightest
node until it stops being the lightest, then the record moves on.

## Honest limitations

- **Shared keys** mean compromise of one node exposes the tunnel. Fine for a
  personal or small-team fleet; not a multi-tenant isolation model.
- **Balancing acts on new connections only.** DNS cannot move a live tunnel, so
  an already-connected client stays put until it reconnects. Over hours the
  fleet evens out; it cannot rebalance a spike instantly.
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
