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


def vpn_link(conf_text: str) -> str:
    """Base64 config URI. The AmneziaWG client and several wg tools accept this;
    the guaranteed-portable path is still the .conf / QR below."""
    return "vpn://" + base64.b64encode(conf_text.encode()).decode()


def qr_png(conf_text: str) -> bytes:
    import qrcode

    img = qrcode.make(conf_text)
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
    with open(base + ".link", "w", encoding="utf-8") as f:
        f.write(vpn_link(conf) + "\n")
    return {"conf": base + ".conf", "qr": base + ".png", "link": base + ".link"}
