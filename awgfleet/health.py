"""Liveness, load and per-peer counters, gathered in one SSH round-trip per node."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .models import Server
from .ssh import run_ssh

_PROBE_CMD = "cat /proc/loadavg; nproc; echo '@AWG@'; awg show awg0 dump 2>/dev/null"


@dataclass
class Probe:
    server: Server
    alive: bool
    load: float | None  # 1-min loadavg normalized by core count
    peers: dict = field(default_factory=dict)  # pubkey -> {rx,tx,handshake}
    rx: int = 0  # node-wide cumulative
    tx: int = 0


async def tcp_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    """AmneziaWG is UDP and can't be hand-shaken without keys, so a reachable
    SSH/health port stands in for 'node is up'."""
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _parse(out: str) -> tuple[float | None, dict, int, int]:
    load = None
    peers: dict = {}
    nrx = ntx = 0
    head, _, dump = out.partition("@AWG@")
    hl = head.strip().splitlines()
    try:
        load1 = float(hl[0].split()[0])
        cores = max(int(hl[1].strip()), 1)
        load = load1 / cores
    except (IndexError, ValueError):
        pass
    for line in dump.strip().splitlines()[1:]:  # first line is the interface itself
        f = line.split("\t")
        if len(f) < 8:
            continue
        try:
            rx, tx, hs = int(f[5]), int(f[6]), int(f[4])
        except ValueError:
            continue
        peers[f[0]] = {"rx": rx, "tx": tx, "handshake": hs}
        nrx += rx
        ntx += tx
    return load, peers, nrx, ntx


async def probe(server: Server) -> Probe:
    if not await tcp_alive(server.host, server.ssh_port):
        return Probe(server, False, None)
    try:
        out = await run_ssh(server, _PROBE_CMD, timeout=15.0)
    except Exception:
        return Probe(server, True, None)  # reachable, but stats/ssh hiccuped
    load, peers, nrx, ntx = _parse(out)
    return Probe(server, True, load, peers, nrx, ntx)
