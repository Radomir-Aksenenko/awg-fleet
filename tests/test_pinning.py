"""Client pinning: one shared domain, a personal port per client, traffic
always egressing from the client's own node — relayed there by whichever node
receives it, failed over only while that node is down."""

import pytest

from awgfleet.clients import add_client, allocate_port, move_client, pick_node
from awgfleet.controller import steering_targets
from awgfleet.keys import generate_keypair, generate_obfuscation
from awgfleet.models import FleetConfig, Server
from awgfleet.render import render_client_conf, render_steering_script


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


def test_assignment_spreads_by_count():
    cfg = _cfg()
    nodes = []
    for i in range(6):
        n = pick_node(cfg)
        add_client(cfg, f"u{i}", node_host=n)
        nodes.append(n)
    assert nodes.count("1.1.1.1") == 3 and nodes.count("2.2.2.2") == 3


def test_assignment_respects_capacity_weight():
    cfg = _cfg()
    cfg.servers[1].weight = 2.0  # b is twice the box
    nodes = []
    for i in range(6):
        n = pick_node(cfg)
        add_client(cfg, f"u{i}", node_host=n)
        nodes.append(n)
    assert nodes.count("2.2.2.2") == 4 and nodes.count("1.1.1.1") == 2


def test_assignment_skips_down_nodes_and_breaks_ties_by_load():
    cfg = _cfg()
    assert pick_node(cfg, alive_by_host={"1.1.1.1": False, "2.2.2.2": True}) == "2.2.2.2"
    assert pick_node(cfg, load_by_host={"1.1.1.1": 0.9, "2.2.2.2": 0.1}) == "2.2.2.2"


def test_pinned_client_gets_a_personal_port_on_the_shared_domain():
    cfg = _cfg()
    c = add_client(cfg, "phone", node_host="2.2.2.2")  # address 10.66.66.2
    assert c.port == cfg.steer_port_base + 2
    assert f"Endpoint = vpn.example.com:{c.port}" in render_client_conf(cfg, c)


def test_unpinned_client_stays_on_the_listen_port():
    cfg = _cfg()
    cfg.servers = []
    c = add_client(cfg, "laptop")
    assert c.port == 0
    assert "Endpoint = vpn.example.com:46441" in render_client_conf(cfg, c)


def test_port_allocation_is_unique_per_address():
    cfg = _cfg()
    ports = set()
    for i in range(20):
        c = add_client(cfg, f"u{i}", node_host="1.1.1.1")
        ports.add(c.port)
    assert len(ports) == 20
    assert allocate_port(cfg, "10.66.66.7/32") == cfg.steer_port_base + 7


def test_steering_script_answers_own_clients_and_relays_the_rest():
    cfg = _cfg()
    script = render_steering_script(cfg, "1.1.1.1", {40002: "1.1.1.1", 40003: "2.2.2.2"})
    # own client: answered locally on the real listen port
    assert "--dport 40002 -j REDIRECT --to-ports 46441" in script
    # someone else's client: relayed to their node, replies masqueraded back
    assert "--dport 40003 -j DNAT --to-destination 2.2.2.2:40003" in script
    assert "-d 2.2.2.2 --dport 40003 -j MASQUERADE" in script
    assert "-d 2.2.2.2 --dport 40003 -j ACCEPT" in script
    # chains are flushed before rules are re-added, so re-applying never stacks
    assert script.index("-F AWGF_STEER\n") < script.index("--dport 40002")


def test_targets_follow_the_pin_and_fail_over_only_when_it_dies():
    cfg = _cfg()
    a = add_client(cfg, "on-a", node_host="1.1.1.1")
    b = add_client(cfg, "on-b", node_host="2.2.2.2")
    score = lambda h: 0.0
    both = steering_targets(cfg, {"1.1.1.1", "2.2.2.2"}, score)
    assert both == {a.port: "1.1.1.1", b.port: "2.2.2.2"}
    # node a dies: only its client is repointed, the other stays home
    failed = steering_targets(cfg, {"2.2.2.2"}, score)
    assert failed == {a.port: "2.2.2.2", b.port: "2.2.2.2"}
    # node a recovers: the client is pointed home again (stable IP long-term)
    assert steering_targets(cfg, {"1.1.1.1", "2.2.2.2"}, score) == both


def test_moving_a_pinned_client_keeps_their_port():
    cfg = _cfg()
    c = add_client(cfg, "phone", node_host="1.1.1.1")
    port = c.port
    moved, reissue = move_client(cfg, "phone", "2.2.2.2")
    # the port is the identity: it survives the move, so the issued config
    # keeps working and only the steering target (egress IP) changes
    assert moved.node_host == "2.2.2.2" and moved.port == port
    assert reissue is False
    assert steering_targets(cfg, {"1.1.1.1", "2.2.2.2"}, lambda h: 0.0) == {port: "2.2.2.2"}


def test_pinning_a_legacy_client_mints_a_port_and_flags_reissue():
    cfg = _cfg()
    add_client(cfg, "old")  # legacy: no pin, no port
    moved, reissue = move_client(cfg, "old", "1.1.1.1")
    assert moved.port == cfg.steer_port_base + 2
    assert reissue is True  # their old config has no personal port in it


def test_moving_to_an_unknown_server_or_client_fails():
    cfg = _cfg()
    add_client(cfg, "phone", node_host="1.1.1.1")
    with pytest.raises(KeyError):
        move_client(cfg, "phone", "9.9.9.9")
    with pytest.raises(KeyError):
        move_client(cfg, "ghost", "1.1.1.1")


def test_legacy_clients_are_left_out_of_steering():
    cfg = _cfg()
    add_client(cfg, "old")  # no pin, no port
    assert steering_targets(cfg, {"1.1.1.1", "2.2.2.2"}, lambda h: 0.0) == {}


def test_vpn_blob_carries_the_personal_port():
    import base64, json, struct, zlib

    from awgfleet.clients import vpn_uri

    cfg = _cfg()
    c = add_client(cfg, "phone", node_host="2.2.2.2")
    b64 = vpn_uri(cfg, c).removeprefix("vpn://")
    b64 += "=" * (-len(b64) % 4)
    blob = base64.urlsafe_b64decode(b64)
    payload = json.loads(zlib.decompress(blob[4:]))
    awg = payload["containers"][0]["awg"]
    assert awg["port"] == str(c.port)
    assert f"Endpoint = vpn.example.com:{c.port}" in json.loads(awg["last_config"])["config"]
