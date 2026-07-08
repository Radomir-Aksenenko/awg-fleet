"""Client lifecycle: allocate an address, mint keys, emit .conf / QR / link."""

from __future__ import annotations

import base64
import io
import ipaddress

from .keys import generate_keypair, generate_preshared_key
from .models import Client, FleetConfig
from .render import client_endpoint_port, render_client_conf


def pick_node(
    cfg: FleetConfig,
    load_by_host: dict[str, float] | None = None,
    alive_by_host: dict[str, bool] | None = None,
) -> str:
    """The node a new client gets pinned to: fewest assigned clients per unit of
    capacity weight, ties broken by live load. A box with weight 2.0 (double the
    bandwidth/cores) therefore fills with twice the clients before the next one
    ranks equal. Down nodes are skipped when we know their state."""
    load_by_host = load_by_host or {}
    alive_by_host = alive_by_host or {}
    counts: dict[str, int] = {}
    for c in cfg.clients:
        if c.node_host:
            counts[c.node_host] = counts.get(c.node_host, 0) + 1
    weights = {s.host: max(s.weight, 0.01) for s in cfg.servers}
    candidates = [
        s.host for s in cfg.servers if s.enabled and alive_by_host.get(s.host, True)
    ]
    if not candidates:  # nothing known-alive -> fall back to any enabled node
        candidates = [s.host for s in cfg.servers if s.enabled]
    if not candidates:
        return ""
    return min(
        candidates,
        key=lambda h: (counts.get(h, 0) / weights.get(h, 1.0), load_by_host.get(h, 0.0)),
    )


def allocate_port(cfg: FleetConfig, address: str) -> int:
    """The client's personal endpoint port, derived from their tunnel address
    offset so it is unique for as long as the address is. The port is the only
    per-client bit in the endpoint (the domain is shared), and it is what lets
    any node recognize the client and steer them to their pinned node."""
    ip = ipaddress.ip_interface(address).ip
    net = ipaddress.ip_network(cfg.subnet, strict=False)
    return cfg.steer_port_base + int(ip) - int(net.network_address)


def allocate_address(cfg: FleetConfig) -> str:
    """Lowest free /32 in the subnet, skipping the gateway (first host)."""
    net = ipaddress.ip_network(cfg.subnet, strict=False)
    hosts = net.hosts()
    gateway = next(hosts)  # reserved for the servers
    taken = {ipaddress.ip_interface(c.address).ip for c in cfg.clients}
    taken.add(gateway)
    for host in net.hosts():
        if host not in taken:
            return f"{host}/32"
    raise RuntimeError("subnet exhausted; widen `subnet` in state.json")


def add_client(
    cfg: FleetConfig,
    name: str,
    created_at: str = "",
    use_psk: bool = True,
    node_host: str = "",
) -> Client:
    if any(c.name == name for c in cfg.clients):
        raise ValueError(f"client {name!r} already exists")
    priv, pub = generate_keypair()
    address = allocate_address(cfg)
    client = Client(
        name=name,
        private_key=priv,
        public_key=pub,
        address=address,
        preshared_key=generate_preshared_key() if use_psk else None,
        created_at=created_at,
        node_host=node_host,
        # the personal port only means something with a pin behind it; an
        # unpinned client stays on the plain listen_port like a legacy one
        port=allocate_port(cfg, address) if node_host else 0,
    )
    cfg.clients.append(client)
    return client


def remove_client(cfg: FleetConfig, name: str) -> Client:
    for i, c in enumerate(cfg.clients):
        if c.name == name:
            return cfg.clients.pop(i)
    raise KeyError(f"no client named {name!r}")


_AWG_KEYS = (
    "H1", "H2", "H3", "H4", "S1", "S2", "S3", "S4",
    "Jc", "Jmin", "Jmax", "I1", "I2", "I3", "I4", "I5",
)


def _amnezia_blob(cfg: FleetConfig, client: Client) -> bytes:
    """The qCompress-framed Amnezia container JSON (4-byte big-endian length +
    zlib), shared by the `vpn://` URI and the QR series. This is the exact byte
    string the desktop client feeds into both its URL and its QR encoders."""
    import json
    import struct
    import zlib

    conf = render_client_conf(cfg, client)
    endpoint_host = cfg.domain
    endpoint_port = client_endpoint_port(cfg, client)
    client_ip = str(ipaddress.ip_interface(client.address).ip)
    net = ipaddress.ip_network(cfg.subnet, strict=False)
    dns = [d.strip() for d in cfg.dns.split(",")] + ["8.8.8.8"]
    awg_params = {k: str(cfg.obfuscation[k]) for k in _AWG_KEYS if k in cfg.obfuscation}

    last_config = {
        **awg_params,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "clientId": "",
        "client_ip": client_ip,
        "client_priv_key": client.private_key,
        "client_pub_key": "",
        "config": conf,
        "hostName": endpoint_host,
        "mtu": str(cfg.mtu),
        "persistent_keep_alive": "25",
        "port": endpoint_port,
        "psk_key": client.preshared_key or "",
        "server_pub_key": cfg.server_public_key,
    }
    awg = {
        **awg_params,
        "last_config": json.dumps(last_config, separators=(",", ":")),
        "port": str(endpoint_port),
        "protocol_version": "2",
        "subnet_address": str(net.network_address),
        "transport_proto": "udp",
    }
    payload = {
        "containers": [{"awg": awg, "container": "amnezia-awg2"}],
        "defaultContainer": "amnezia-awg2",
        "description": cfg.label or f"AWG {cfg.domain}",
        "dns1": dns[0],
        "dns2": dns[1],
        "hostName": endpoint_host,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + zlib.compress(raw, 9)


def vpn_uri(cfg: FleetConfig, client: Client) -> str:
    """The Amnezia native `vpn://` URI for the AmneziaVPN app (as opposed to the
    plain .conf the AmneziaWG app reads): the compressed blob, base64url-encoded."""
    return "vpn://" + base64.urlsafe_b64encode(_amnezia_blob(cfg, client)).decode().rstrip("=")


# AmneziaVPN's multi-QR import format (client/core/utils/qrCodeUtils.cpp +
# importController.cpp). The app splits the *compressed bytes* (not the vpn://
# text) into slices; each QR carries a header the reader uses to reassemble:
# a qint16 magic (1984), a quint8 total count, a quint8 index, then the slice
# as a Qt QDataStream QByteArray (quint32 length prefix), serialized big-endian
# and base64url-encoded. The reader keys slices by index and concatenates, so
# slice size is free — Amnezia's own encoder caps it at 850, but a code that
# full is dense and scans badly on a phone. We aim for ~600 bytes per code so a
# typical config splits into two comfortably sparse codes; bigger configs get
# more. Never exceed 850 (Amnezia's ceiling) and never emit fewer than one.
_QR_MAGIC = 1984
_QR_TARGET = 600  # desired bytes per code (well under Amnezia's 850 max)


def vpn_qr_chunks(cfg: FleetConfig, client: Client) -> list[str]:
    """The base64url payloads for an AmneziaVPN QR series (one string per code).

    Split into balanced slices sized for reliable phone scanning; the AmneziaVPN
    app reassembles them by index regardless of how we chunked."""
    import math
    import struct

    data = _amnezia_blob(cfg, client)
    total = max(1, math.ceil(len(data) / _QR_TARGET))
    size = math.ceil(len(data) / total)  # balanced, so no single code is dense
    chunks = []
    for idx in range(total):
        piece = data[idx * size : (idx + 1) * size]
        frame = (
            struct.pack(">h", _QR_MAGIC)  # qint16 magic, big-endian
            + struct.pack(">B", total)  # quint8 chunk count
            + struct.pack(">B", idx)  # quint8 chunk index
            + struct.pack(">I", len(piece))  # QByteArray length prefix (quint32 BE)
            + piece
        )
        chunks.append(base64.urlsafe_b64encode(frame).decode().rstrip("="))
    return chunks


def vpn_qr_series(cfg: FleetConfig, client: Client) -> list[bytes]:
    """PNGs for the AmneziaVPN QR series: scan them in turn in the app to import."""
    return [qr_png(c, low_ec=True) for c in vpn_qr_chunks(cfg, client)]


def qr_png(text: str, low_ec: bool = False) -> bytes:
    """QR as PNG. `low_ec` uses the loosest error correction so a long vpn://
    URI still fits inside a single scannable code."""
    import qrcode

    if low_ec:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image()
    else:
        img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def write_client_bundle(cfg: FleetConfig, client: Client, out_dir: str = ".") -> dict:
    """Write <name>.conf, <name>.png (QR) and <name>.link into out_dir."""
    import os

    os.makedirs(out_dir, exist_ok=True)
    conf = render_client_conf(cfg, client)
    base = os.path.join(out_dir, client.name)
    with open(base + ".conf", "w", encoding="utf-8") as f:
        f.write(conf)
    with open(base + ".png", "wb") as f:
        f.write(qr_png(conf))
    with open(base + ".vpn", "w", encoding="utf-8") as f:
        f.write(vpn_uri(cfg, client) + "\n")
    return {"conf": base + ".conf", "qr": base + ".png", "vpn": base + ".vpn"}
