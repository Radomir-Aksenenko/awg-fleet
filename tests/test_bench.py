"""Capacity benchmark: weight derived from measured hardware/network, weekly
schedule anchored to Monday 00:00 Krasnoyarsk."""

from datetime import datetime, timezone

from awgfleet.bench import (
    apply_bench,
    bench_due,
    compute_weight,
    last_bench_slot,
    parse_bench,
)
from awgfleet.models import Server


def test_parse_bench_output():
    out = "CORES=4\nDOWN=118750000.000\nUP=25000000.000\n"
    cores, down, up = parse_bench(out)
    assert cores == 4
    assert down == 950.0  # bytes/s -> Mbit/s
    assert up == 200.0


def test_parse_bench_tolerates_garbage():
    cores, down, up = parse_bench("curl: (28) timed out\nDOWN=0\n")
    assert cores == 1 and down == 0.0 and up == 0.0


def test_weight_baseline_is_a_plain_100mbit_single_core():
    assert compute_weight(1, 100.0, 100.0) == 1.0


def test_weight_scales_with_pipe_and_cores():
    small = compute_weight(1, 100.0, 100.0)
    big = compute_weight(4, 950.0, 200.0)  # gigabit 4-core
    assert big > small * 10
    # cores help sublinearly and cap out: 64 cores is not 64x
    assert compute_weight(64, 100.0, 100.0) == compute_weight(6, 100.0, 100.0) == 2.0


def test_weight_is_clamped():
    assert compute_weight(1, 0.5, 0.0) == 0.1  # a dead pipe never zeroes placement
    assert compute_weight(64, 100000.0, 100000.0) == 100.0


def test_slot_is_monday_midnight_krasnoyarsk():
    # Wed 2026-07-08 12:00 UTC -> Wed 19:00 KRAT -> slot Mon 2026-07-06 00:00+07
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    assert last_bench_slot(now) == "2026-07-05T17:00:00+00:00"
    # Sunday 16:59 UTC is still Sunday 23:59 KRAT -> same (previous) Monday
    now = datetime(2026, 7, 12, 16, 59, tzinfo=timezone.utc)
    assert last_bench_slot(now) == "2026-07-05T17:00:00+00:00"
    # Sunday 17:00 UTC is Monday 00:00 KRAT -> the slot rolls over
    now = datetime(2026, 7, 12, 17, 0, tzinfo=timezone.utc)
    assert last_bench_slot(now) == "2026-07-12T17:00:00+00:00"


def test_bench_due_compares_against_the_slot():
    slot = "2026-07-05T17:00:00+00:00"
    never = Server(name="a", host="1.1.1.1")
    fresh = Server(name="b", host="2.2.2.2", bench={"at": "2026-07-06T03:00:00+00:00"})
    stale = Server(name="c", host="3.3.3.3", bench={"at": "2026-06-29T17:05:00+00:00"})
    assert bench_due(never, slot)
    assert not bench_due(fresh, slot)
    assert bench_due(stale, slot)


def test_apply_bench_sets_weight_from_the_measurement():
    s = Server(name="a", host="1.1.1.1")
    apply_bench(s, {"cores": 2, "down_mbps": 500.0, "up_mbps": 300.0, "at": "x"})
    assert s.bench["down_mbps"] == 500.0
    assert s.weight == compute_weight(2, 500.0, 300.0) == 5.28
