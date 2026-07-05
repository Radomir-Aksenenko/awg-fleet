"""Client lifecycle: allocate an address, mint keys, emit .conf / QR / link."""

from __future__ import annotations

import base64
import io
import ipaddress

from .keys import generate_keypair, generate_preshared_key
from .models import Client, FleetConfig
from .render import render_client_conf


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


def add_client(cfg: FleetConfig, name: str, created_at: str = "", use_psk: bool = True) -> Client:
    if any(c.name == name for c in cfg.clients):
        raise ValueError(f"client {name!r} already exists")
    priv, pub = generate_keypair()
    client = Client(
        name=name,
        private_key=priv,
        public_key=pub,
        address=allocate_address(cfg),
        preshared_key=generate_preshared_key() if use_psk else None,
        created_at=created_at,
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


def vpn_uri(cfg: FleetConfig, client: Client) -> str:
    """The Amnezia native `vpn://` URI for the AmneziaVPN app (as opposed to the
    plain .conf the AmneziaWG app reads). Payload is the Amnezia container JSON,
    qCompress-framed (4-byte big-endian length + zlib) and base64url-encoded,
    matching what the desktop client emits."""
    import json
    import struct
    import zlib

    conf = render_client_conf(cfg, client)
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
        "hostName": cfg.domain,
        "mtu": str(cfg.mtu),
        "persistent_keep_alive": "25",
        "port": cfg.listen_port,
        "psk_key": client.preshared_key or "",
        "server_pub_key": cfg.server_public_key,
    }
    awg = {
        **awg_params,
        "last_config": json.dumps(last_config, separators=(",", ":")),
        "port": str(cfg.listen_port),
        "protocol_version": "2",
        "subnet_address": str(net.network_address),
        "transport_proto": "udp",
    }
    payload = {
        "containers": [{"awg": awg, "container": "amnezia-awg2"}],
        "defaultContainer": "amnezia-awg2",
        "description": f"AWG {cfg.domain}",
        "dns1": dns[0],
        "dns2": dns[1],
        "hostName": cfg.domain,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    blob = struct.pack(">I", len(raw)) + zlib.compress(raw, 9)
    return "vpn://" + base64.urlsafe_b64encode(blob).decode().rstrip("=")


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
