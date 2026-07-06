from awgfleet.clients import add_client, pick_home_host
from awgfleet.keys import generate_keypair, generate_obfuscation
from awgfleet.models import FleetConfig, Server
from awgfleet.render import client_endpoint_host, render_client_conf


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


def test_home_assignment_spreads_by_count():
    cfg = _cfg()
    # no live metrics: pure round-robin by assigned count
    homes = []
    for i in range(6):
        h = pick_home_host(cfg)
        add_client(cfg, f"u{i}", home_host=h)
        homes.append(h)
    assert homes.count("1.1.1.1") == 3 and homes.count("2.2.2.2") == 3


def test_home_assignment_prefers_lighter_node_on_tie():
    cfg = _cfg()
    # equal counts (0/0) -> load breaks the tie
    assert pick_home_host(cfg, load_by_host={"1.1.1.1": 0.9, "2.2.2.2": 0.1}) == "2.2.2.2"


def test_home_assignment_skips_down_nodes():
    cfg = _cfg()
    got = pick_home_host(cfg, alive_by_host={"1.1.1.1": False, "2.2.2.2": True})
    assert got == "2.2.2.2"


def test_assigned_client_uses_its_own_subdomain():
    cfg = _cfg()
    c = add_client(cfg, "phone", home_host="2.2.2.2")  # address 10.66.66.2
    assert client_endpoint_host(cfg, c) == "n2.vpn.example.com"
    conf = render_client_conf(cfg, c)
    assert "Endpoint = n2.vpn.example.com:46441" in conf


def test_legacy_client_stays_on_the_domain():
    cfg = _cfg()
    c = add_client(cfg, "laptop")  # no home_host
    assert client_endpoint_host(cfg, c) == "vpn.example.com"
    assert "Endpoint = vpn.example.com:46441" in render_client_conf(cfg, c)
