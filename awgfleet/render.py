"""Render AmneziaWG config text for the shared server identity and for clients.

Every node gets the *same* server config (same keys, same peers), which is what
lets one client hand-shake with any node under a single endpoint. The server
config is MTU-safe by construction: MTU is pinned low and PostUp clamps TCP MSS
to the path, so large packets never black-hole the way they do on resellers
that quietly deliver a sub-1500 path.
"""

from __future__ import annotations

import ipaddress

from .models import Client, FleetConfig

def _post_up(port: int) -> str:
    """Open the listen port in INPUT (nodes with a default-DROP policy would
    otherwise silently eat every handshake), then set up NAT, forwarding and an
    MSS clamp keyed off the node's real default-route interface."""
    return (
        f"iptables -I INPUT -p udp --dport {port} -j ACCEPT; "
        "DEV=$(ip route show default | awk '{print $5; exit}'); "
        "iptables -A FORWARD -i %i -j ACCEPT; "
        "iptables -A FORWARD -o %i -j ACCEPT; "
        "iptables -t nat -A POSTROUTING -o $DEV -j MASQUERADE; "
        "iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN "
        "-j TCPMSS --clamp-mss-to-pmtu"
    )


def _post_down(port: int) -> str:
    return _post_up(port).replace("-I ", "-D ").replace("-A ", "-D ")


def server_tunnel_address(cfg: FleetConfig) -> str:
    """The gateway address inside the tunnel, e.g. 10.8.0.1 for 10.8.0.0/24."""
    net = ipaddress.ip_network(cfg.subnet, strict=False)
    return f"{next(net.hosts())}/{net.prefixlen}"


def _obfuscation_lines(cfg: FleetConfig) -> list[str]:
    o = cfg.obfuscation
    return [f"{k} = {o[k]}" for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4") if k in o]


def render_server_conf(cfg: FleetConfig) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {cfg.server_private_key}",
        f"Address = {server_tunnel_address(cfg)}",
        f"ListenPort = {cfg.listen_port}",
        f"MTU = {cfg.mtu}",
        f"PostUp = {_post_up(cfg.listen_port)}",
        f"PostDown = {_post_down(cfg.listen_port)}",
        *_obfuscation_lines(cfg),
    ]
    for c in cfg.clients:
        lines += ["", "[Peer]", f"PublicKey = {c.public_key}"]
        if c.preshared_key:
            lines.append(f"PresharedKey = {c.preshared_key}")
        lines.append(f"AllowedIPs = {c.address}")
    return "\n".join(lines) + "\n"


def render_client_conf(cfg: FleetConfig, client: Client) -> str:
    """A client config whose Endpoint is the fleet domain, not any single node."""
    lines = [
        "[Interface]",
        f"PrivateKey = {client.private_key}",
        f"Address = {client.address}",
        f"DNS = {cfg.dns}",
        f"MTU = {cfg.mtu}",
        *_obfuscation_lines(cfg),
        "",
        "[Peer]",
        f"PublicKey = {cfg.server_public_key}",
    ]
    if client.preshared_key:
        lines.append(f"PresharedKey = {client.preshared_key}")
    lines += [
        "AllowedIPs = 0.0.0.0/0, ::/0",
        f"Endpoint = {cfg.domain}:{cfg.listen_port}",
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines) + "\n"
