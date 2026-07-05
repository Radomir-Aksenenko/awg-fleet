"""Steering loop: probe nodes, collect metrics, reconcile DNS with a smart policy.

Rotation is not a plain in/out flip. Load is smoothed with an EWMA so a single
spike doesn't yank a node; joining and leaving use a hysteresis band around the
threshold so membership doesn't flap; a node has to miss two probes in a row to
be treated as down (fast enough for failover, calm enough to ignore a blip);
and when two live nodes drift far apart in load, the heavier one is drained from
rotation so new handshakes land on the lighter one. The fleet never goes dark:
if everything is loaded, the least-loaded node stays in.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .cloudflare import Cloudflare
from .health import Probe, probe
from .models import FleetConfig
from .stats import StatsDB

EWMA_ALPHA = 0.4
DOWN_STREAK_OUT = 2  # consecutive failed probes before a node leaves
UP_STREAK_IN = 2  # consecutive healthy probes before a node may join
DRAIN_GAP = 0.35  # load spread that triggers draining the heaviest node


class Steerer:
    """Carries the smoothed load and streak state across passes."""

    def __init__(self):
        self.ewma: dict[str, float] = {}
        self.up: dict[str, int] = {}
        self.down: dict[str, int] = {}
        self.in_rot: set[str] = set()

    def decide(self, cfg: FleetConfig, probes: list[Probe]) -> list[str]:
        thr = cfg.load_threshold
        for p in probes:
            h = p.server.host
            if p.alive:
                self.up[h] = self.up.get(h, 0) + 1
                self.down[h] = 0
                load = p.load if p.load is not None else 0.0
                self.ewma[h] = EWMA_ALPHA * load + (1 - EWMA_ALPHA) * self.ewma.get(h, load)
            else:
                self.down[h] = self.down.get(h, 0) + 1
                self.up[h] = 0

        rot = set(self.in_rot)
        # leave: two missed probes, or sustained overload (upper hysteresis edge)
        for h in list(rot):
            if self.down.get(h, 0) >= DOWN_STREAK_OUT or self.ewma.get(h, 1.0) >= thr * 1.1:
                rot.discard(h)
        # join: stable-healthy and comfortably under threshold (lower hysteresis edge)
        for p in probes:
            h = p.server.host
            if p.alive and self.up.get(h, 0) >= UP_STREAK_IN and self.ewma.get(h, 0.0) < thr * 0.9:
                rot.add(h)
        # never dark
        if not rot:
            alive = [p for p in probes if p.alive]
            if alive:
                rot = {min(alive, key=lambda p: self.ewma.get(p.server.host, 0.0)).server.host}
        # drain the heaviest when the spread is wide
        if len(rot) >= 2:
            loads = {h: self.ewma.get(h, 0.0) for h in rot}
            if max(loads.values()) - min(loads.values()) > DRAIN_GAP:
                rot.discard(max(loads, key=loads.get))

        self.in_rot = rot
        return sorted(rot)


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
