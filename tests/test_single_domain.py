"""Every client lives on the one bare domain; balancing is done by moving its
A record, so nothing here (config, vpn:// blob, state) may ever grow a
per-client hostname again."""

import json

from awgfleet.clients import add_client
from awgfleet.controller import cleanup_legacy_steering
from awgfleet.keys import generate_keypair, generate_obfuscation
from awgfleet.models import FleetConfig, Server
from awgfleet.render import render_client_conf
from awgfleet.state import State


def _cfg() -> FleetConfig:
    priv, pub = generate_keypair()
    cfg = FleetConfig(
        domain="vpn.example.com",
        cf_zone_id="z",
        listen_port=46441,
        server_private_key=priv,
        server_public_key=pub,
        obfuscation=generate_obfuscation(),
    )
    cfg.servers = [Server(name="a", host="1.1.1.1"), Server(name="b", host="2.2.2.2")]
    return cfg


def test_every_client_points_at_the_bare_domain():
    cfg = _cfg()
    for i in range(5):
        c = add_client(cfg, f"u{i}")
        assert "Endpoint = vpn.example.com:46441" in render_client_conf(cfg, c)


def test_state_written_by_the_subdomain_era_still_loads(tmp_path):
    # an old state.json carries home_host on clients; loading must not explode
    # and the client must come out as a plain bare-domain client
    old = {
        "domain": "vpn.example.com",
        "cf_zone_id": "z",
        "servers": [{"name": "a", "host": "1.1.1.1", "priority": 1}],
        "clients": [
            {
                "name": "phone",
                "private_key": "k",
                "public_key": "p",
                "address": "10.66.66.2/32",
                "home_host": "1.1.1.1",
            }
        ],
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    cfg = State(str(path)).load()
    assert cfg.clients[0].name == "phone"
    assert not hasattr(cfg.clients[0], "home_host")
    assert cfg.servers[0].priority == 1  # known fields still round-trip
    assert "Endpoint = vpn.example.com:51820" in render_client_conf(cfg, cfg.clients[0])


class _FakeCF:
    def __init__(self, records):
        self.records = records
        self.deleted = []

    def list_zone_a_records(self, zone_id):
        return self.records

    def delete_record(self, zone_id, record_id):
        self.deleted.append(record_id)


def test_cleanup_drops_only_legacy_client_subdomains():
    cfg = _cfg()
    cf = _FakeCF(
        [
            {"id": "1", "name": "n2.vpn.example.com"},  # legacy per-client record
            {"id": "2", "name": "n17.vpn.example.com"},  # legacy per-client record
            {"id": "3", "name": "vpn.example.com"},  # the fleet domain itself
            {"id": "4", "name": "mail.example.com"},  # unrelated zone record
            {"id": "5", "name": "n2.other.example.com"},  # not our domain
        ]
    )
    dropped = cleanup_legacy_steering(cfg, cf)
    assert dropped == ["n17.vpn.example.com", "n2.vpn.example.com"]
    assert cf.deleted == ["1", "2"]
