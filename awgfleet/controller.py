"""Steering loop: probe nodes, collect metrics, keep the domain on the
least-loaded node.

Every client points at the same single domain — no per-client subdomains, no
static assignment. The controller publishes exactly one A record under that
domain: the lightest healthy node right now. New handshakes therefore always
land on the least-loaded server, and as clients pile onto it and its score
rises past another node's, the record moves on — today everyone new lands on
node A, tomorrow on node B. Clients that are already connected stay where they
are (WireGuard keeps the resolved IP for the session), so moving the record
spreads *new* connections without kicking anyone off.

The load score blends smoothed CPU (EWMA, so a spike doesn't move the record)
with the live user count (so a box busy on connections, not CPU, still reads
as loaded). The record only moves when another node is lighter by a real
margin — a small wobble never bounces it back and forth. Failover is fast: two
missed probes and the standby (already a warm crypto-twin carrying every peer)
takes over. The fleet never goes dark — if the only node left is overloaded,
it stays published.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from .cloudflare import Cloudflare
from .health import Probe, probe
from .models import FleetConfig
from .stats import ONLINE_WINDOW, StatsDB

EWMA_ALPHA = 0.4
DOWN_STREAK_OUT = 2  # consecutive failed probes before the primary fails over
UP_STREAK_IN = 2  # consecutive healthy probes before a node may become primary
REBALANCE_GAP = 0.15  # how much lighter another node must be before the record moves
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
        elif preferred is not None:
            if (
                preferred != prim
                and self.prio.get(preferred, 0) > self.prio.get(prim, 0)
                and self.score(preferred) < thr * 0.9
            ):
                # a higher-priority node is healthy and comfortably under
                # threshold: it preempts (an overloaded one stays out, no flap)
                self.primary = preferred
            elif (
                preferred != prim
                and self.prio.get(preferred, 0) >= self.prio.get(prim, 0)
                and self.score(prim) - self.score(preferred) > REBALANCE_GAP
            ):
                # the actual balancing: another node is clearly lighter, so new
                # connections should land there from now on. The gap (plus the
                # EWMA) is the hysteresis — a wobble never bounces the record.
                self.primary = preferred
            elif self.score(prim) >= thr:
                # primary slammed: shed to the lightest node even if that means
                # stepping down from a pinned (higher-priority) one
                best = min(ready, key=self.score)
                if best != prim and self.score(prim) - self.score(best) > REBALANCE_GAP:
                    self.primary = best

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

    name_by_host = {s.host: s.name for s in cfg.servers}
    stats.set_meta(
        rotation=[name_by_host.get(h, h) for h in ips],
        rotation_ips=ips,
        load={p.server.name: round(steerer.ewma.get(p.server.host, 0.0), 3) for p in probes},
        alive={p.server.name: p.alive for p in probes},
        updated=int(time.time()),
    )
    return ReconcileResult(probes=probes, in_rotation=ips)


def cleanup_legacy_steering(cfg: FleetConfig, cf: Cloudflare) -> list[str]:
    """Delete the per-client nX.<domain> steering records an older awg-fleet
    published. All clients live on the bare domain now; leftover subdomains
    would keep routing stale traffic and clutter the zone. Returns the names
    dropped so the caller can log them."""
    pat = re.compile(rf"^n\d+\.{re.escape(cfg.domain)}$")
    dropped = []
    for rec in cf.list_zone_a_records(cfg.cf_zone_id):
        if pat.match(rec.get("name", "")):
            cf.delete_record(cfg.cf_zone_id, rec["id"])
            dropped.append(rec["name"])
    return sorted(set(dropped))


async def run_controller(state, cf: Cloudflare, on_pass=None) -> None:
    """Reload state each pass so panel/CLI changes apply without a restart."""
    steerer = Steerer()
    stats = StatsDB()
    interval = 30
    try:  # one-time migration; best effort, the zone may be temporarily down
        cleanup_legacy_steering(state.load(), cf)
    except Exception:
        pass
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
