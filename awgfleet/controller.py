"""The steering loop: probe every node, decide who is in rotation, reconcile DNS.

Rotation policy:
  * a node must be alive to be in rotation (down node -> instant failover);
  * among alive nodes, those under `load_threshold` are in rotation;
  * if every alive node is overloaded, keep the single least-loaded one so the
    fleet never goes dark;
  * DNS round-robin spreads new handshakes across whoever is in rotation.

Note: WireGuard roams, so an *already connected* client keeps its node until it
reconnects. Steering acts on new/renewed handshakes, which is what you want for
draining an overloaded or dying node.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .cloudflare import Cloudflare
from .health import Probe, probe
from .models import FleetConfig


@dataclass
class ReconcileResult:
    probes: list[Probe]
    in_rotation: list[str]  # IPs published to DNS


def decide_rotation(cfg: FleetConfig, probes: list[Probe]) -> list[str]:
    alive = [p for p in probes if p.alive]
    if not alive:
        return []
    unloaded = [p for p in alive if p.load is None or p.load < cfg.load_threshold]
    chosen = unloaded or [min(alive, key=lambda p: p.load if p.load is not None else 0.0)]
    return [p.server.host for p in chosen]


async def reconcile_once(cfg: FleetConfig, cf: Cloudflare) -> ReconcileResult:
    active = [s for s in cfg.servers if s.enabled]
    probes = await asyncio.gather(*(probe(s) for s in active))
    ips = decide_rotation(cfg, list(probes))
    if ips:  # never publish an empty set; a stale record beats no record
        cf.reconcile_a_records(cfg.cf_zone_id, cfg.domain, ips, ttl=60)
    return ReconcileResult(probes=list(probes), in_rotation=ips)


async def run_controller(state, cf: Cloudflare, on_pass=None) -> None:
    """Reload state each pass so changes made via the CLI or web panel (a node
    added, a client revoked) are picked up without a restart."""
    interval = 30
    while True:
        try:
            cfg = state.load()
            interval = cfg.health_interval
            result = await reconcile_once(cfg, cf)
            if on_pass:
                on_pass(result)
        except Exception as exc:  # keep the loop alive across transient API/SSH errors
            if on_pass:
                on_pass(exc)
        await asyncio.sleep(interval)
