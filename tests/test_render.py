import pytest

from awgfleet.clients import add_client, allocate_address, remove_client
from awgfleet.keys import generate_keypair, generate_obfuscation
from awgfleet.models import FleetConfig
from awgfleet.render import render_client_conf, render_server_conf, server_tunnel_address


def make_cfg() -> FleetConfig:
    priv, pub = generate_keypair()
    return FleetConfig(
        domain="vpn.example.com",
        cf_zone_id="zone",
        server_private_key=priv,
        server_public_key=pub,
        obfuscation=generate_obfuscation(),
    )


def test_server_gateway_address():
    assert server_tunnel_address(make_cfg()) == "10.8.0.1/24"


def test_addresses_are_allocated_in_order():
    cfg = make_cfg()
    a = add_client(cfg, "iphone")
    b = add_client(cfg, "laptop")
    assert a.address == "10.8.0.2/32"
    assert b.address == "10.8.0.3/32"


def test_removing_a_client_frees_its_address():
    cfg = make_cfg()
    add_client(cfg, "iphone")
    remove_client(cfg, "iphone")
    assert allocate_address(cfg) == "10.8.0.2/32"


def test_server_conf_carries_every_peer():
    cfg = make_cfg()
    a = add_client(cfg, "a")
    b = add_client(cfg, "b")
    conf = render_server_conf(cfg)
    assert a.public_key in conf and b.public_key in conf
    assert "ListenPort = 51820" in conf
    assert "MTU = 1280" in conf


def test_client_conf_points_at_the_domain_not_a_node():
    cfg = make_cfg()
    client = add_client(cfg, "iphone")
    conf = render_client_conf(cfg, client)
    assert "Endpoint = vpn.example.com:51820" in conf
    assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf


def test_duplicate_client_rejected():
    cfg = make_cfg()
    add_client(cfg, "iphone")
    with pytest.raises(ValueError):
        add_client(cfg, "iphone")
