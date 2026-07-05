"""WireGuard / AmneziaWG key material and obfuscation parameters."""

from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_b64, public_key_b64), Curve25519 as WireGuard expects."""
    priv = X25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()


def public_from_private(private_key_b64: str) -> str:
    raw = base64.b64decode(private_key_b64)
    priv = X25519PrivateKey.from_private_bytes(raw)
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub_raw).decode()


def generate_preshared_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def generate_obfuscation() -> dict:
    """AmneziaWG 2.x obfuscation: junk packets (Jc/Jmin/Jmax), init/response
    padding (S1/S2) and four distinct magic headers (H1..H4).

    Padding is kept modest so the obfuscated handshake still fits a small path
    MTU. S1 must differ from S2, and the four headers must be unique.
    """
    s1 = secrets.randbelow(120) + 15
    s2 = secrets.randbelow(120) + 15
    while s2 == s1:
        s2 = secrets.randbelow(120) + 15

    headers: set[int] = set()
    while len(headers) < 4:
        headers.add(secrets.randbelow(2_000_000_000) + 5)
    h1, h2, h3, h4 = sorted(headers)

    return {
        "Jc": secrets.randbelow(8) + 3,  # 3..10 junk packets
        "Jmin": 8,
        "Jmax": 80,
        "S1": s1,
        "S2": s2,
        "H1": h1,
        "H2": h2,
        "H3": h3,
        "H4": h4,
    }
