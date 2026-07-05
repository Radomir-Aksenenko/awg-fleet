import base64
import json
import struct
import zlib

from awgfleet.clients import _amnezia_blob, add_client, vpn_qr_chunks, vpn_uri
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


def _reassemble_qr_series(chunks: list[str]) -> bytes:
    """Mirror the AmneziaVPN app's QR reassembly: decode each chunk, check the
    magic, order by index, and concatenate the slices back into one blob."""
    pieces: dict[int, bytes] = {}
    total = None
    for c in chunks:
        c += "=" * (-len(c) % 4)
        frame = base64.urlsafe_b64decode(c)
        magic = struct.unpack(">h", frame[:2])[0]
        assert magic == 1984  # qrCodeUtils::qrMagicCode
        count = frame[2]
        index = frame[3]
        length = struct.unpack(">I", frame[4:8])[0]
        slice_ = frame[8:]
        assert len(slice_) == length
        total = count if total is None else total
        assert count == total
        pieces[index] = slice_
    return b"".join(pieces[i] for i in range(total))


def test_vpn_qr_series_reassembles_to_the_same_blob():
    cfg = _cfg()
    # a few clients so at least one config crosses the 850-byte chunk boundary
    for i in range(6):
        add_client(cfg, f"user{i}")
    client = cfg.clients[-1]

    chunks = vpn_qr_chunks(cfg, client)
    assert len(chunks) >= 1
    # every slice but the last is a full 850-byte chunk
    blob = _amnezia_blob(cfg, client)
    assert _reassemble_qr_series(chunks) == blob

    # and the reassembled blob is a valid qCompress frame the app can inflate
    length = struct.unpack(">I", blob[:4])[0]
    raw = zlib.decompress(blob[4:])
    assert len(raw) == length
    assert json.loads(raw)["defaultContainer"] == "amnezia-awg2"


def test_vpn_qr_series_splits_for_scannability():
    import math

    cfg = _cfg()
    client = add_client(cfg, "phone")
    blob = _amnezia_blob(cfg, client)
    chunks = vpn_qr_chunks(cfg, client)

    # balanced split targeting ~600 bytes/code: a normal config yields 2 codes
    assert len(chunks) == max(1, math.ceil(len(blob) / 600))
    assert len(chunks) >= 2  # a real AWG v2 config is big enough to split

    # no single code carries more than Amnezia's own 850-byte ceiling
    for c in chunks:
        c += "=" * (-len(c) % 4)
        slice_len = struct.unpack(">I", base64.urlsafe_b64decode(c)[4:8])[0]
        assert slice_len <= 850
