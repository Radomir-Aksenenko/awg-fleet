from awgfleet.controller import decide_rotation
from awgfleet.health import Probe
from awgfleet.models import FleetConfig, Server


def _cfg() -> FleetConfig:
    return FleetConfig(domain="d", load_threshold=0.85)


def _srv(name: str, host: str) -> Server:
    return Server(name=name, host=host)


def test_down_node_is_excluded():
    probes = [
        Probe(_srv("a", "1.1.1.1"), alive=True, load=0.1),
        Probe(_srv("b", "2.2.2.2"), alive=False, load=None),
    ]
    assert decide_rotation(_cfg(), probes) == ["1.1.1.1"]


def test_unloaded_nodes_share_rotation():
    probes = [
        Probe(_srv("a", "1.1.1.1"), alive=True, load=0.1),
        Probe(_srv("b", "2.2.2.2"), alive=True, load=0.2),
    ]
    assert set(decide_rotation(_cfg(), probes)) == {"1.1.1.1", "2.2.2.2"}


def test_when_all_overloaded_keep_least_loaded():
    probes = [
        Probe(_srv("a", "1.1.1.1"), alive=True, load=0.95),
        Probe(_srv("b", "2.2.2.2"), alive=True, load=0.90),
    ]
    assert decide_rotation(_cfg(), probes) == ["2.2.2.2"]


def test_all_down_publishes_nothing():
    probes = [Probe(_srv("a", "1.1.1.1"), alive=False, load=None)]
    assert decide_rotation(_cfg(), probes) == []
