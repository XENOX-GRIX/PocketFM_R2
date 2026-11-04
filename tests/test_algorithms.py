"""Deterministic workflow tests: no sleeping and exact boundary checks."""

import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from limiter.service import LimiterService

ALGORITHMS = ["token_bucket", "fixed_window", "sliding_window"]


def make_service(settings, clock, algorithm):
    service = LimiterService(settings, clock)
    service.switch(algorithm, 1)
    return service


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_limit_reject_and_user_isolation(settings, clock, algorithm):
    service = make_service(settings, clock, algorithm)
    assert [service.check("alice")["remaining"] for _ in range(3)] == [2, 1, 0]
    assert service.check("alice")["allowed"] is False
    assert service.check("bob")["allowed"] is True
    assert service.dashboard()["users"][0]["user_id"] == "alice"


def test_token_fractional_refill_and_capacity_cap(settings, clock):
    service = make_service(settings, clock, "token_bucket")
    for _ in range(3):
        assert service.check("alice")["allowed"]
    clock.advance(0.25)
    rejection = service.check("alice")
    assert not rejection["allowed"]
    assert rejection["retry_after_ms"] == 750
    clock.advance(0.75)
    assert service.check("alice")["allowed"]
    assert not service.check("alice")["allowed"]
    clock.advance(30)
    assert [service.check("alice")["allowed"] for _ in range(4)] == [True, True, True, False]


def test_fixed_window_resets_exactly_at_boundary(settings, clock):
    clock.now = 109.75
    service = make_service(settings, clock, "fixed_window")
    for _ in range(3):
        assert service.check("alice")["allowed"]
    assert service.check("alice")["retry_after_ms"] == 250
    clock.advance(0.25)
    assert [service.check("alice")["allowed"] for _ in range(4)] == [True, True, True, False]


def test_sliding_queue_expires_each_admitted_request(settings, clock):
    service = make_service(settings, clock, "sliding_window")
    for _ in range(3):
        assert service.check("alice")["allowed"]
        clock.advance(1)
    assert service.check("alice")["retry_after_ms"] == 7000
    clock.advance(6.5)
    assert service.check("alice")["retry_after_ms"] == 500
    clock.advance(0.5)
    assert service.check("alice")["allowed"]
    assert service.check("alice")["retry_after_ms"] == 1000
    clock.advance(1)
    assert service.check("alice")["allowed"]


def test_rolling_window_matches_independent_history_oracle(settings, clock):
    service = make_service(settings, clock, "sliding_window")
    admitted = []
    random_source = random.Random(42)
    for _ in range(500):
        clock.advance(random_source.choice([0, 0.25, 1, 2.5, 10]))
        in_window = [timestamp for timestamp in admitted if clock.now - 10 < timestamp <= clock.now]
        expected = len(in_window) < 3
        assert service.check("alice")["allowed"] == expected
        if expected:
            admitted.append(clock.now)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_concurrent_requests_do_not_over_admit(settings, clock, algorithm):
    service = make_service(settings, clock, algorithm)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: service.check("alice"), range(100)))
    assert sum(result["allowed"] for result in results) == 3
    assert service.dashboard()["metrics"]["rejected"] == 97

