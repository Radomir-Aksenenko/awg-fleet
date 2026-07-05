import base64
import json
import struct
import zlib

from awgfleet.clients import add_client, vpn_uri
from awgfleet.keys import generate_keypair, generate_obfuscation
from awgfleet.models import FleetConfig


def _cfg() -> FleetConfig:
    priv, pub = generate_keypair()
    return FleetConfig(
        domain="vpn.example.com",
        cf_zone_id="z",
        listen_port=46441,
        server_private_key=priv,
        server_public_key=pub,
        obfuscation=generate_obfuscation(),
    )


def _decode(uri: str) -> dict:
    b64 = uri.removeprefix("vpn://")
    b64 += "=" * (-len(b64) % 4)
    blob = base64.urlsafe_b64decode(b64)
    length = struct.unpack(">I", blob[:4])[0]
    raw = zlib.decompress(blob[4:])
    assert len(raw) == length
    return json.loads(raw)


def test_vpn_uri_roundtrips_to_amnezia_json():
    cfg = _cfg()
    client = add_client(cfg, "iphone")
    uri = vpn_uri(cfg, client)
    assert uri.startswith("vpn://")

    payload = _decode(uri)
    assert payload["defaultContainer"] == "amnezia-awg2"
    awg = payload["containers"][0]["awg"]
    assert awg["protocol_version"] == "2"
    assert awg["port"] == "46441"

    last = json.loads(awg["last_config"])
    assert last["server_pub_key"] == cfg.server_public_key
    assert last["client_priv_key"] == client.private_key
    assert "Endpoint = vpn.example.com:46441" in last["config"]
    assert last["I1"]  # v2 CPS carried through
