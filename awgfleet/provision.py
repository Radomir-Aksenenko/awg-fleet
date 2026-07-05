"""Install AmneziaWG on a node and push the shared config to it.

Every node in the fleet is a crypto-twin: identical server keys, identical peer
list. Provisioning is idempotent — re-running it just re-syncs the config and
restarts the interface. Tested against Ubuntu 22.04 / 24.04; other distros will
need the install block adjusted.
"""

from __future__ import annotations

from .models import FleetConfig, Server
from .render import render_server_conf
from .ssh import run_ssh, upload_text

REMOTE_CONF = "/etc/amnezia/amneziawg/awg0.conf"

_INSTALL = r"""
export DEBIAN_FRONTEND=noninteractive
if ! command -v awg-quick >/dev/null 2>&1; then
  # A single broken third-party repo must not abort the whole install, so every
  # apt-get update is tolerated; the real check is whether awg-quick shows up.
  apt-get update -qq 2>/dev/null || true
  apt-get install -y -qq software-properties-common iproute2 iptables curl >/dev/null 2>&1 || true
  add-apt-repository -y ppa:amnezia/ppa >/dev/null 2>&1 || true
  apt-get update -qq 2>/dev/null || true
  apt-get install -y -qq amneziawg amneziawg-tools >/dev/null 2>&1 || true
fi
if ! command -v awg-quick >/dev/null 2>&1; then
  echo "AWG-INSTALL-FAILED: amneziawg-tools not available (check distro / apt sources)" >&2
  exit 1
fi
mkdir -p /etc/amnezia/amneziawg
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true
grep -q '^net.ipv4.ip_forward=1' /etc/sysctl.conf || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf
echo "install-ok"
"""


async def install_amneziawg(server: Server) -> None:
    await run_ssh(server, _INSTALL, timeout=600.0)


async def push_config(server: Server, cfg: FleetConfig) -> None:
    """Upload the shared awg0.conf and (re)start the interface."""
    conf = render_server_conf(cfg)
    await run_ssh(server, "mkdir -p /etc/amnezia/amneziawg")
    await upload_text(server, REMOTE_CONF, conf)
    await run_ssh(server, f"chmod 600 {REMOTE_CONF}")
    # awg-quick has no reload; down/up is the supported way to apply changes.
    await run_ssh(
        server,
        "systemctl enable awg-quick@awg0 >/dev/null 2>&1 || true; "
        "awg-quick down awg0 >/dev/null 2>&1 || true; "
        "awg-quick up awg0",
        timeout=120.0,
    )


async def provision_server(server: Server, cfg: FleetConfig) -> None:
    await install_amneziawg(server)
    await push_config(server, cfg)


async def teardown_server(server: Server) -> None:
    await run_ssh(
        server,
        "awg-quick down awg0 >/dev/null 2>&1 || true; "
        "systemctl disable awg-quick@awg0 >/dev/null 2>&1 || true; "
        f"rm -f {REMOTE_CONF}",
        timeout=60.0,
    )
