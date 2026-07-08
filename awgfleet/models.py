"""Data model for a fleet: the shared server identity, the nodes, the clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Server:
    """One AmneziaWG node. All nodes in a fleet share the same server keys,
    so a client can hand-shake with any of them under one endpoint."""

    name: str
    host: str  # public IP (what goes into the DNS rotation)
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_password: Optional[str] = None
    ssh_key_path: Optional[str] = None
    region: str = ""
    enabled: bool = True
    # Steering preference: among healthy nodes the highest priority wins the
    # primary slot regardless of load. Lets you pin the fleet to the node with
    # the best reachability (e.g. one IP is throttled on some mobile carrier)
    # while keeping the others as failover.
    priority: int = 0
    # Relative capacity for client placement: a box with double the bandwidth /
    # cores gets weight 2.0 and is assigned twice the clients.
    weight: float = 1.0


@dataclass
class Client:
    """One VPN peer. Its keypair is the same across every node; its tunnel
    address is allocated once and mirrored to all nodes."""

    name: str
    private_key: str
    public_key: str
    address: str  # e.g. "10.8.0.2/32"
    preshared_key: Optional[str] = None
    created_at: str = ""
    # The node this client is pinned to: their traffic always egresses here (one
    # stable public IP for IP-bound sessions), whichever node DNS hands them.
    # Empty = legacy client that just follows the shared domain record.
    node_host: str = ""
    # Personal endpoint port on the shared domain. DNS cannot tell clients
    # apart, but every node can: the port identifies the client, so any node
    # answers a client pinned to it and relays everyone else to their node.
    # 0 = legacy client on the plain listen_port.
    port: int = 0


@dataclass
class FleetConfig:
    """The single source of truth, persisted as state.json."""

    domain: str  # the one endpoint clients use, e.g. "vpn.example.com"
    label: str = ""  # name the imported VPN shows in the app (falls back to "AWG <domain>")
    cf_zone_id: str = ""
    listen_port: int = 51820
    server_private_key: str = ""
    server_public_key: str = ""
    obfuscation: dict = field(default_factory=dict)  # AmneziaWG Jc/S/H params
    subnet: str = "10.66.66.0/24"  # off the 10.8.x defaults that OpenVPN/Amnezia grab
    dns: str = "1.1.1.1"
    mtu: int = 1200  # low by design: outer packet stays ~1260, fits mobile/CGNAT path MTUs
    load_threshold: float = 0.85  # normalized loadavg above which a node leaves rotation
    health_interval: int = 30  # seconds between reconcile passes
    steer_port_base: int = 40000  # client ports = base + offset of their tunnel address
    servers: list = field(default_factory=list)  # list[Server]
    clients: list = field(default_factory=list)  # list[Client]
