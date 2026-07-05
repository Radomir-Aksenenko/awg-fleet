from awgfleet.controller import Steerer
from awgfleet.health import Probe
from awgfleet.models import FleetConfig, Server


def _cfg() -> FleetConfig:
    return FleetConfig(domain="d", load_threshold=0.85)


def _srv(name: str, host: str) -> Server:
    return Server(name=name, host=host)


def _settle(steerer, cfg, probes, passes=3):
    out = []
    for _ in range(passes):
        out = steerer.decide(cfg, probes)
    return out


def test_healthy_nodes_join_rotation():
    probes = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert set(_settle(Steerer(), _cfg(), probes)) == {"1.1.1.1", "2.2.2.2"}


def test_down_node_leaves_only_after_two_misses():
    s, cfg = Steerer(), _cfg()
    up = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    _settle(s, cfg, up, 3)
    down = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), False, None)]
    assert "2.2.2.2" in s.decide(cfg, down)  # one miss tolerated
    assert s.decide(cfg, down) == ["1.1.1.1"]  # second miss drops it


def test_never_goes_dark():
    probes = [Probe(_srv("a", "1.1.1.1"), True, 0.99)]  # overloaded, but the only node
    assert _settle(Steerer(), _cfg(), probes) == ["1.1.1.1"]


def test_two_node_fleet_keeps_both_despite_gap():
    # draining would leave a single point of failure, so a 2-node fleet holds both
    probes = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.6)]
    assert set(_settle(Steerer(), _cfg(), probes, passes=5)) == {"1.1.1.1", "2.2.2.2"}


def test_drains_heaviest_with_three_nodes():
    probes = [
        Probe(_srv("a", "1.1.1.1"), True, 0.1),
        Probe(_srv("b", "2.2.2.2"), True, 0.1),
        Probe(_srv("c", "3.3.3.3"), True, 0.6),
    ]
    out = _settle(Steerer(), _cfg(), probes, passes=5)
    assert "3.3.3.3" not in out and {"1.1.1.1", "2.2.2.2"} <= set(out)


def test_all_down_publishes_nothing():
    probes = [Probe(_srv("a", "1.1.1.1"), False, None)]
    assert _settle(Steerer(), _cfg(), probes) == []
