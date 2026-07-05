# awg-fleet

**One AmneziaWG config. A whole fleet of servers behind it.**

You hand out a single client config on a single domain. Behind that domain sits
a pool of AmneziaWG nodes. awg-fleet health-checks them and steers DNS so new
connections land on a live, unloaded node, and if a node dies or gets blocked,
clients roll onto another one without touching their config.

No panel to click through, no "pick a server" list. One key, one domain, the
fleet sorts itself out.

```
                         vpn.example.com  (A records, low TTL)
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        ┌──────────┐       ┌──────────┐       ┌──────────┐
        │  node 1  │       │  node 2  │       │  node 3  │   ← crypto-twins:
        │ AmneziaWG│       │ AmneziaWG│       │ AmneziaWG│     same server keys,
        └──────────┘       └──────────┘       └──────────┘     same peer list
              ▲                  ▲                  ▲
              └──────── controller: probe + reconcile DNS ─────┘
```

## Why this exists

WireGuard and AmneziaWG are point-to-point: a client config pins one endpoint
and one server public key. You cannot just round-robin a domain across servers
that each hold their own key, because the handshake with the "wrong" key fails.

awg-fleet solves it the way commercial WireGuard providers do: every node in the
fleet shares the **same** server keypair and the **same** peer list. Now a client
whose `Endpoint = vpn.example.com` can hand-shake with any node. A domain with
several A records spreads new handshakes; dropping an A record drains a node.

## What it does

- **One config for the whole fleet.** Clients point at the domain, never at a node.
- **Automatic failover.** A node that stops answering leaves DNS within one pass.
- **Load shedding.** A node over its load threshold leaves rotation so new
  handshakes go elsewhere; it rejoins when it cools down.
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

1. probe every node: TCP liveness plus normalized `loadavg`;
2. a node must be alive to be in rotation (down node leaves at once);
3. among alive nodes, those under `load_threshold` are published;
4. if every alive node is overloaded, keep the single least-loaded one, so the
   fleet never goes dark;
5. reconcile the domain's A records to exactly that set (grey-cloud, low TTL).

WireGuard roams, so a client that is **already** connected stays on its node until
it reconnects. Steering acts on new and renewed handshakes, which is exactly what
you want for draining a dying or overloaded node.

## Honest limitations

- **Shared keys** mean compromise of one node exposes the tunnel. Fine for a
  personal or small-team fleet; not a multi-tenant isolation model.
- **Load steering is coarse.** DNS round-robin cannot weight, so the knob is
  in-rotation / out-of-rotation, not fine-grained weights. Real weighting needs
  a Cloudflare Load Balancer (paid) or a custom pool controller.
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
