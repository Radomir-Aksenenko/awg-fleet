from awgfleet.controller import Steerer
from awgfleet.health import Probe
from awgfleet.models import FleetConfig, Server


def _cfg(threshold: float = 0.85) -> FleetConfig:
    return FleetConfig(domain="d", load_threshold=threshold)


def _srv(name: str, host: str, priority: int = 0) -> Server:
    return Server(name=name, host=host, priority=priority)


def _settle(steerer, cfg, probes, passes=3):
    out = []
    for _ in range(passes):
        out = steerer.decide(cfg, probes)
    return out


def test_lightest_node_becomes_primary():
    # active-passive: exactly one IP in DNS, the lighter node
    probes = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert _settle(Steerer(), _cfg(), probes) == ["1.1.1.1"]


def test_only_one_ip_is_published():
    probes = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.6)]
    out = _settle(Steerer(), _cfg(), probes, passes=5)
    assert out == ["1.1.1.1"]  # standby held in reserve, not round-robined


def test_three_nodes_still_single_primary():
    probes = [
        Probe(_srv("a", "1.1.1.1"), True, 0.1),
        Probe(_srv("b", "2.2.2.2"), True, 0.1),
        Probe(_srv("c", "3.3.3.3"), True, 0.6),
    ]
    out = _settle(Steerer(), _cfg(), probes, passes=5)
    assert len(out) == 1 and out[0] in {"1.1.1.1", "2.2.2.2"}  # lightest, never the heavy node


def test_primary_is_sticky_under_small_load_changes():
    s, cfg = Steerer(), _cfg()
    p = [Probe(_srv("a", "1.1.1.1"), True, 0.2), Probe(_srv("b", "2.2.2.2"), True, 0.1)]
    assert _settle(s, cfg, p, 3) == ["2.2.2.2"]  # b lighter -> primary
    # a is now the lighter one, but b is nowhere near overloaded: do NOT bounce
    p2 = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert _settle(s, cfg, p2, 3) == ["2.2.2.2"]


def test_primary_sheds_when_overloaded():
    s, cfg = Steerer(), _cfg(threshold=0.3)
    p = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert _settle(s, cfg, p, 3) == ["1.1.1.1"]  # a lighter -> primary
    # a spikes well past threshold and b is far lighter -> hand off to b
    p2 = [Probe(_srv("a", "1.1.1.1"), True, 0.99), Probe(_srv("b", "2.2.2.2"), True, 0.05)]
    assert _settle(s, cfg, p2, 6) == ["2.2.2.2"]


def test_primary_fails_over_after_two_misses():
    s, cfg = Steerer(), _cfg()
    up = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert _settle(s, cfg, up, 3) == ["1.1.1.1"]  # a is primary
    down = [Probe(_srv("a", "1.1.1.1"), False, None), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert s.decide(cfg, down) == ["1.1.1.1"]  # one miss tolerated, primary held
    assert s.decide(cfg, down) == ["2.2.2.2"]  # second miss -> fail over to the standby


def test_priority_node_wins_even_if_heavier():
    probes = [
        Probe(_srv("a", "1.1.1.1"), True, 0.05),
        Probe(_srv("b", "2.2.2.2", priority=1), True, 0.3),
    ]
    assert _settle(Steerer(), _cfg(), probes, passes=4) == ["2.2.2.2"]


def test_priority_node_preempts_running_primary():
    s, cfg = Steerer(), _cfg()
    plain = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2"), True, 0.2)]
    assert _settle(s, cfg, plain, 3) == ["1.1.1.1"]
    # b gains priority (e.g. operator pins it in state.json): it takes over
    pinned = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2", priority=1), True, 0.2)]
    assert _settle(s, cfg, pinned, 3) == ["2.2.2.2"]


def test_overloaded_priority_node_does_not_preempt():
    s, cfg = Steerer(), _cfg(threshold=0.3)
    probes = [
        Probe(_srv("a", "1.1.1.1"), True, 0.1),
        Probe(_srv("b", "2.2.2.2", priority=1), True, 0.9),  # preferred but slammed
    ]
    assert _settle(s, cfg, probes, passes=5) == ["1.1.1.1"]


def test_failover_prefers_priority_node():
    s, cfg = Steerer(), _cfg()
    up = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2", priority=1), True, 0.2)]
    assert _settle(s, cfg, up, 3) == ["2.2.2.2"]
    down_b = [Probe(_srv("a", "1.1.1.1"), True, 0.1), Probe(_srv("b", "2.2.2.2", priority=1), False, None)]
    s.decide(cfg, down_b)
    assert s.decide(cfg, down_b) == ["1.1.1.1"]  # fails over to the plain node
    # b comes back healthy -> preempts its way back in after proving itself
    assert _settle(s, cfg, up, 3) == ["2.2.2.2"]


def test_never_goes_dark():
    probes = [Probe(_srv("a", "1.1.1.1"), True, 0.99)]  # overloaded, but the only node
    assert _settle(Steerer(), _cfg(), probes) == ["1.1.1.1"]


def test_all_down_publishes_nothing():
    probes = [Probe(_srv("a", "1.1.1.1"), False, None)]
    assert _settle(Steerer(), _cfg(), probes) == []
