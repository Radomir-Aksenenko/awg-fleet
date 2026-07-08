"""Install AmneziaWG on a node and push the shared config to it.

Every node in the fleet is a crypto-twin: identical server keys, identical peer
list. Provisioning is idempotent — re-running it just re-syncs the config and
restarts the interface. Tested against Ubuntu 22.04 / 24.04; other distros will
need the install block adjusted.
"""

from __future__ import annotations

from .models import FleetConfig, Server
from .render import (
    STEER_SCRIPT_PATH,
    render_server_conf,
    render_steering_script,
    render_steering_teardown,
)
from .ssh import run_ssh, upload_files_and_run

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
# The kernel module must match the RUNNING kernel. A newer-but-unbooted kernel is
# common (update installed, box not rebooted): DKMS then built for the wrong
# version. Build for the running kernel instead of forcing a prod reboot.
if ! modprobe amneziawg 2>/dev/null; then
  apt-get install -y -qq "linux-headers-$(uname -r)" >/dev/null 2>&1 || true
  AWGVER=$(dkms status 2>/dev/null | grep -oiE 'amneziawg/[0-9.]+' | head -1)
  [ -n "$AWGVER" ] && dkms install "$AWGVER" -k "$(uname -r)" >/dev/null 2>&1 || true
  dkms autoinstall -k "$(uname -r)" >/dev/null 2>&1 || true
  modprobe amneziawg 2>/dev/null || true
fi
if ! lsmod | grep -q '^amneziawg' && ! modprobe amneziawg 2>/dev/null; then
  echo "AWG-MODULE-FAILED: amneziawg kernel module unavailable for $(uname -r)" >&2
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
    """Upload the shared awg0.conf and apply it.

    If the interface is already up, `awg syncconf` merges the change in place:
    it adds or drops only the peers that differ and never touches the running
    tunnels, so adding a client is instant and does not kick anyone off. The MTU
    lives outside the peer set, so syncconf won't touch it; we set it live with
    `ip link set` (also non-disruptive) so an MTU change reaches the fleet
    without a restart. A fresh node with no interface yet gets the full
    `awg-quick up` (routes + PostUp).

    The client-pinning script rides along and is applied in the same pass with
    static pins (client -> its node); the controller overlays failover targets
    on its own passes when a pinned node is down."""
    conf = render_server_conf(cfg)
    steer = render_steering_script(
        cfg, server.host, {c.port: c.node_host for c in cfg.clients if c.port and c.node_host}
    )
    apply = (
        f"chmod 600 {REMOTE_CONF}; "
        "if ip link show awg0 >/dev/null 2>&1; then "
        "awg-quick strip awg0 > /tmp/awg0.sync 2>/dev/null "
        "&& awg syncconf awg0 /tmp/awg0.sync; rm -f /tmp/awg0.sync; "
        f"ip link set dev awg0 mtu {cfg.mtu} 2>/dev/null || true; "
        "else systemctl enable awg-quick@awg0 >/dev/null 2>&1 || true; awg-quick up awg0; fi; "
        f"sh {STEER_SCRIPT_PATH}"
    )
    await upload_files_and_run(
        server, {REMOTE_CONF: conf, STEER_SCRIPT_PATH: steer}, apply, timeout=90.0
    )


async def provision_server(server: Server, cfg: FleetConfig) -> None:
    await install_amneziawg(server)
    await push_config(server, cfg)


async def teardown_server(server: Server) -> None:
    await run_ssh(
        server,
        "awg-quick down awg0 >/dev/null 2>&1 || true; "
        "systemctl disable awg-quick@awg0 >/dev/null 2>&1 || true; "
        f"{render_steering_teardown()}; "
        f"rm -f {REMOTE_CONF} {STEER_SCRIPT_PATH}",
        timeout=60.0,
    )
