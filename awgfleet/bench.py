"""Node capability benchmark: measure the hardware and the pipe, derive the
placement weight from them.

The weight is what client placement divides by (clients-per-unit-of-weight),
so it must mean "how many people can this box actually carry". That is mostly
the network pipe and somewhat the CPU (WireGuard crypto scales with cores), so:

    net    = 0.7 * download_mbps + 0.3 * upload_mbps   # VPN users mostly pull
    cpu    = 1 + 0.2 * (cores - 1), capped at 2.0      # cores help, sublinearly
    weight = net / 100 * cpu                           # 100 Mbit, 1 core = 1.0

A plain 100 Mbit single-core VPS is the 1.0 baseline; a gigabit 4-core box
lands around 16 and takes ~16x the clients. The result is clamped to
[0.1, 100] and rounded so the panel shows something readable.

Speed is measured against speed.cloudflare.com with curl (already installed by
provisioning): ~15s of download and ~15s of upload, once a week per node —
negligible traffic. If the benchmark fails (no curl, endpoint blocked), the
node keeps its previous weight rather than being zeroed out.

Schedule: every Monday 00:00 Asia/Krasnoyarsk (UTC+7, no DST since 2014), plus
once at server-add time. The controller compares each node's last-benchmark
stamp against the most recent slot, so a node that was down on Monday is
benched as soon as it is seen alive again, and a controller restart never
re-runs a bench that already happened this week.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import Server
from .ssh import run_ssh

KRAT = timezone(timedelta(hours=7))  # Asia/Krasnoyarsk

_DOWN_URL = "https://speed.cloudflare.com/__down?bytes=500000000"
_UP_URL = "https://speed.cloudflare.com/__up"

# One SSH round-trip: cores, then measured download/upload in bytes per second.
# Each curl is capped by --max-time and tolerated on failure (prints 0), so a
# blocked speedtest endpoint degrades to "keep the old weight", not an error.
_BENCH_CMD = (
    "echo CORES=$(nproc); "
    f"D=$(curl -o /dev/null -s -w '%{{speed_download}}' --max-time 15 '{_DOWN_URL}' || echo 0); "
    'echo "DOWN=$D"; '
    "U=$(head -c 25000000 /dev/zero | "
    f"curl -o /dev/null -s -X POST --data-binary @- -w '%{{speed_upload}}' --max-time 15 '{_UP_URL}' || echo 0); "
    'echo "UP=$U"; true'
)


def parse_bench(out: str) -> tuple[int, float, float]:
    """(cores, download_mbps, upload_mbps) from the benchmark command output."""

    def _grab(key: str) -> float:
        m = re.search(rf"^{key}=([\d.]+)", out, re.MULTILINE)
        return float(m.group(1)) if m else 0.0

    cores = int(_grab("CORES")) or 1
    down = _grab("DOWN") * 8 / 1_000_000  # bytes/s -> Mbit/s
    up = _grab("UP") * 8 / 1_000_000
    return cores, round(down, 1), round(up, 1)


def compute_weight(cores: int, down_mbps: float, up_mbps: float) -> float:
    net = 0.7 * down_mbps + 0.3 * up_mbps
    cpu = min(1 + 0.2 * (max(cores, 1) - 1), 2.0)
    return round(min(max(net / 100 * cpu, 0.1), 100.0), 2)


def last_bench_slot(now: datetime | None = None) -> str:
    """The most recent Monday 00:00 Krasnoyarsk, as a UTC ISO stamp. Nodes whose
    last benchmark predates this are due."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(KRAT)
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.astimezone(timezone.utc).isoformat(timespec="seconds")


def bench_due(server: Server, slot: str | None = None) -> bool:
    return (server.bench or {}).get("at", "") < (slot or last_bench_slot())


async def benchmark_server(server: Server) -> dict | None:
    """Measure a node. Returns the bench record to store on the server, or
    None when the measurement is unusable (keep the previous weight then)."""
    out = await run_ssh(server, _BENCH_CMD, timeout=60.0)
    cores, down, up = parse_bench(out)
    if down <= 0 and up <= 0:  # curl missing or the endpoint unreachable
        return None
    return {
        "cores": cores,
        "down_mbps": down,
        "up_mbps": up,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def apply_bench(server: Server, bench: dict) -> None:
    server.bench = bench
    server.weight = compute_weight(bench["cores"], bench["down_mbps"], bench["up_mbps"])
