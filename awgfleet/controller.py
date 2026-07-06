"""Steering loop: probe nodes, collect metrics, publish one active node in DNS.

The policy is active-passive, not round-robin, and that is deliberate. Every
node is a crypto-twin and NATs client traffic behind its *own* public IP
(MASQUERADE), so a client's live connections are tied to whichever node it
handshook with. On mobile the phone's IP changes constantly (CGNAT rebinds,
tower handoffs); each change makes the app re-resolve the domain. If the domain
carried several A records, that re-resolution could land the client on a
*different* node mid-session and every NAT'd connection would break - the tunnel
stays up but "everything drops." Publishing a single IP keeps a re-resolving
client on the same node, so the session survives.

So exactly one node is in DNS at a time: the lightest healthy one. Load is
smoothed with an EWMA so a spike doesn't move it; the choice is sticky, so a
small load wobble never bounces clients between nodes; the primary only sheds to
another node when it is sustainedly overloaded *and* a clearly lighter node
exists. Failover is fast: two missed probes in a row and the standby (already a
warm crypto-twin carrying every peer) takes over. The fleet never goes dark - if
the only node left is overloaded, it stays published.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .cloudflare import Cloudflare
from .health import Probe, probe
from .models import FleetConfig
from .render import client_endpoint_host
from .stats import ONLINE_WINDOW, StatsDB

EWMA_ALPHA = 0.4
DOWN_STREAK_OUT = 2  # consecutive failed probes before the primary fails over
UP_STREAK_IN = 2  # consecutive healthy probes before a node may become primary
SHED_GAP = 0.35  # how much lighter another node must be before an overloaded primary hands off
USER_WEIGHT = 0.02  # each connected user counts like this much CPU load in the score


class Steerer:
    """Carries the smoothed load and streak state across passes."""

    def __init__(self):
        self.ewma: dict[str, float] = {}
        self.users: dict[str, int] = {}
        self.up: dict[str, int] = {}
        self.down: dict[str, int] = {}
        self.prio: dict[str, int] = {}
        self.primary: str | None = None  # the single host currently published in DNS
        self.in_rot: set[str] = set()  # kept in sync with primary for the panel/meta
        self.pub: dict[str, str] = {}  # steering subdomain -> last-published IP (change cache)

    def _best(self, hosts) -> str:
        """Highest priority first, then lightest score."""
        return min(hosts, key=lambda h: (-self.prio.get(h, 0), self.score(h)))

    def score(self, host: str) -> float:
        """A node's load, blending smoothed CPU with its live user count, so a
        box that is busy on connections (not CPU) is still seen as loaded."""
        return self.ewma.get(host, 0.0) + USER_WEIGHT * self.users.get(host, 0)

    def decide(self, cfg: FleetConfig, probes: list[Probe]) -> list[str]:
        now = int(time.time())
        thr = cfg.load_threshold
        for p in probes:
            h = p.server.host
            self.prio[h] = getattr(p.server, "priority", 0)
            if p.alive:
                self.up[h] = self.up.get(h, 0) + 1
                self.down[h] = 0
                load = p.load if p.load is not None else 0.0
                self.ewma[h] = EWMA_ALPHA * load + (1 - EWMA_ALPHA) * self.ewma.get(h, load)
                self.users[h] = sum(
                    1
                    for v in p.peers.values()
                    if v.get("handshake") and now - v["handshake"] < ONLINE_WINDOW
                )
            else:
                self.down[h] = self.down.get(h, 0) + 1
                self.up[h] = 0

        alive = [p.server.host for p in probes if p.alive]
        # a node must prove itself before it can be trusted as primary; fall back
        # to any live node so a cold fleet still comes up on its first good probe
        ready = [h for h in alive if self.up.get(h, 0) >= UP_STREAK_IN] or alive

        prim = self.primary
        prim_ok = prim is not None and self.down.get(prim, 0) < DOWN_STREAK_OUT
        preferred = self._best(ready) if ready else None
        if not prim_ok:
            # cold start or failover: the primary is gone, take the best node
            self.primary = preferred
        elif (
            preferred is not None
            and self.prio.get(preferred, 0) > self.prio.get(prim, 0)
            and self.score(preferred) < thr * 0.9
        ):
            # a higher-priority node is healthy and comfortably under threshold:
            # it preempts (an overloaded preferred node stays out, so no flapping)
            self.primary = preferred
        elif self.score(prim) >= thr * 1.1:
            # primary sustainedly overloaded: shed, but only to a clearly lighter
            # node (hysteresis) so a wobble never bounces everyone between nodes
            best = min(ready, key=self.score)
            if best != prim and self.score(prim) - self.score(best) > SHED_GAP:
                self.primary = best
        # otherwise keep the current primary: stickiness is the whole point, so a
        # mobile client that re-resolves mid-session lands on the same node again

        self.in_rot = {self.primary} if self.primary else set()
        return [self.primary] if self.primary else []


@dataclass
class ReconcileResult:
    probes: list[Probe]
    in_rotation: list[str]


async def reconcile_once(cfg: FleetConfig, cf: Cloudflare, steerer: Steerer, stats: StatsDB) -> ReconcileResult:
    active = [s for s in cfg.servers if s.enabled]
    probes = list(await asyncio.gather(*(probe(s) for s in active))) if active else []

    interval = cfg.health_interval
    for p in probes:
        if p.alive:
            try:
                stats.collect(p.server.name, p.peers, interval)
            except Exception:
                pass

    ips = steerer.decide(cfg, probes)
    if ips:  # never publish an empty set; a stale record still routes
        cf.reconcile_a_records(cfg.cf_zone_id, cfg.domain, ips, ttl=60)

    # Per-client steering: keep each load-distributed client on its home node,
    # fail it over to the lightest live node while home is down, and put it back
    # when home recovers. Cloudflare is only touched when a target actually
    # changes, so 40 steady records cost one API call, not 40 per pass.
    alive_hosts = {p.server.host for p in probes if p.alive}
    fallback = min(alive_hosts, key=steerer.score) if alive_hosts else None
    for c in cfg.clients:
        if not c.home_host:
            continue
        target = c.home_host if c.home_host in alive_hosts else fallback
        if not target:
            continue
        sub = client_endpoint_host(cfg, c)
        if steerer.pub.get(sub) != target:
            try:
                cf.reconcile_a_records(cfg.cf_zone_id, sub, [target], ttl=60)
                steerer.pub[sub] = target
            except Exception:
                pass

    name_by_host = {s.host: s.name for s in cfg.servers}
    stats.set_meta(
        rotation=[name_by_host.get(h, h) for h in ips],
        rotation_ips=ips,
        load={p.server.name: round(steerer.ewma.get(p.server.host, 0.0), 3) for p in probes},
        alive={p.server.name: p.alive for p in probes},
        updated=int(time.time()),
    )
    return ReconcileResult(probes=probes, in_rotation=ips)


async def run_controller(state, cf: Cloudflare, on_pass=None) -> None:
    """Reload state each pass so panel/CLI changes apply without a restart."""
    steerer = Steerer()
    stats = StatsDB()
    interval = 30
    while True:
        try:
            cfg = state.load()
            interval = cfg.health_interval
            result = await reconcile_once(cfg, cf, steerer, stats)
            if on_pass:
                on_pass(result)
        except Exception as exc:
            if on_pass:
                on_pass(exc)
        await asyncio.sleep(interval)
