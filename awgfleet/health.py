"""Liveness and load probes used by the steering controller."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .models import Server
from .ssh import run_ssh


@dataclass
class Probe:
    server: Server
    alive: bool
    load: float | None  # 1-min loadavg normalized by core count, None if unknown


async def tcp_alive(host: str, port: int, timeout: float = 3.0) -> bool:
    """Cheap liveness signal. AmneziaWG is UDP and can't be hand-shaken without
    keys, so we treat a reachable SSH/health port as 'node is up'."""
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


async def server_load(server: Server) -> float | None:
    try:
        out = await run_ssh(server, "cat /proc/loadavg; nproc", timeout=15.0)
    except Exception:
        return None
    lines = out.strip().splitlines()
    if len(lines) < 2:
        return None
    try:
        load1 = float(lines[0].split()[0])
        cores = max(int(lines[-1].strip()), 1)
        return load1 / cores
    except Exception:
        return None


async def probe(server: Server) -> Probe:
    alive = await tcp_alive(server.host, server.ssh_port)
    load = await server_load(server) if alive else None
    return Probe(server=server, alive=alive, load=load)
