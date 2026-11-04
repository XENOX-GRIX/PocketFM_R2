from concurrent.futures import ThreadPoolExecutor

import pytest

from limiter.config import ConfigError, Settings, load_settings
from limiter.service import CapacityExceeded, LimiterService, PolicyConflict


def changed_settings(settings, **storage):
    values = settings.to_dict()
    values["storage"].update(storage)
    return Settings.from_dict(values)


def test_switch_resets_usage_keeps_history_and_same_selection_is_noop(settings, clock):
    service = LimiterService(settings, clock)
    for _ in range(4):
        service.check("alice")
    assert service.switch("token_bucket", 1)["policy_version"] == 1
    assert not service.check("alice")["allowed"]
    assert service.switch("sliding_window", 1)["policy_version"] == 2
    assert service.check("alice")["remaining"] == 2
    row = service.dashboard()["users"][0]
    assert row["algorithm"] == "token_bucket"
    assert row["policy_version"] == 1
    with pytest.raises(PolicyConflict):
        service.switch("fixed_window", 1)


def test_simultaneous_admin_changes_have_one_winner(settings, clock):
    service = LimiterService(settings, clock)

    def change(algorithm):
        try:
            service.switch(algorithm, 1)
            return True
        except PolicyConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(change, ["fixed_window", "sliding_window"])) == 1


def test_capacity_does_not_evict_active_users_and_idle_state_expires(settings, clock):
    service = LimiterService(changed_settings(settings, max_users=1), clock)
    for _ in range(4):
        service.check("alice")
    with pytest.raises(CapacityExceeded):
        service.check("bob")
    assert not service.check("alice")["allowed"]
    clock.advance(60)
    assert service.check("bob")["allowed"]
    assert service.dashboard()["metrics"]["tracked_users"] == 1


def test_history_cap_retention_search_and_pagination(settings, clock):
    service = LimiterService(changed_settings(settings, max_events=3), clock)
    for user in ["alice", "bob", "carol", "dave"]:
        for _ in range(4):
            service.check(user)
        clock.advance(1)
    snapshot = service.dashboard(lookback_seconds=60, limit=1, offset=1)
    assert snapshot["metrics"]["retained_events"] == 3
    assert snapshot["metrics"]["rejected"] == 4
    assert snapshot["total_users"] == 3
    assert snapshot["users"][0]["user_id"] == "carol"
    assert service.dashboard(search="bo")["users"][0]["user_id"] == "bob"
    assert service.dashboard(lookback_seconds=1)["total_users"] == 1
    clock.advance(60)
    assert not service.dashboard(lookback_seconds=60)["users"]


def test_new_instance_restores_static_default(settings, clock):
    service = LimiterService(settings, clock)
    service.switch("fixed_window", 1)
    service.check("alice")
    restarted = LimiterService(settings, clock)
    assert restarted.configuration()["active_algorithm"] == "token_bucket"
    assert restarted.dashboard()["metrics"]["tracked_users"] == 0


@pytest.mark.parametrize("section,key,value", [
    ("token_bucket", "capacity", 0),
    ("token_bucket", "capacity", True),
    ("token_bucket", "refill_per_second", float("nan")),
    ("token_bucket", "refill_per_second", float("inf")),
    ("fixed_window", "window_seconds", -1.0),
    ("fixed_window", "window_seconds", 1e-300),
    ("sliding_window", "limit", 1.5),
    ("storage", "idle_ttl_seconds", 1.0),
    ("storage", "max_users", 0),
    ("storage", "max_session_users", 1001),
])
def test_invalid_config_fails_at_startup(settings, section, key, value):
    values = settings.to_dict()
    values[section][key] = value
    with pytest.raises(ConfigError):
        Settings.from_dict(values)


def test_session_users_survive_event_expiry_switches_and_heavy_repeat_traffic(settings, clock):
    service = LimiterService(changed_settings(settings, max_events=2), clock)
    for user in ["alice", "bob"]:
        for _ in range(4):
            service.check(user)
    for _ in range(10):
        service.check("bob")
    assert service.dashboard()["total_users"] == 2
    assert service.dashboard(lookback_seconds=60)["total_users"] == 1
    clock.advance(61)
    service.switch("fixed_window", 1)
    snapshot = service.dashboard()
    assert snapshot["metrics"]["retained_events"] == 0
    assert [row["user_id"] for row in snapshot["users"]] == ["bob", "alice"]
    assert all(row["policy_status"] == "Earlier policy" for row in snapshot["users"])
    assert snapshot["users"][0]["rejection_count"] == 11
    assert LimiterService(settings, clock).dashboard()["users"] == []
    assert LimiterService(settings, clock).configuration()["session_id"] != service.configuration()["session_id"]


def test_session_users_are_bounded_and_ordered_by_latest_rejection_even_with_timestamp_ties(settings, clock):
    service = LimiterService(changed_settings(settings, max_session_users=3), clock)
    for user in ["alice", "bob", "carol", "dave"]:
        for _ in range(4):
            service.check(user)
    assert [row["user_id"] for row in service.dashboard()["users"]] == ["dave", "carol", "bob"]
    service.check("bob")
    assert [row["user_id"] for row in service.dashboard()["users"]] == ["bob", "dave", "carol"]
    service.check("alice")
    assert [row["user_id"] for row in service.dashboard()["users"]] == ["alice", "bob", "dave"]
    assert service.dashboard()["users"][0]["rejection_count"] == 1
    assert [row["user_id"] for row in service.dashboard(search="bo")["users"]] == ["bob"]


@pytest.mark.parametrize("sequence", [
    ["token_bucket", "fixed_window", "sliding_window", "token_bucket"],
    ["sliding_window", "fixed_window", "token_bucket", "sliding_window"],
])
def test_only_selected_algorithm_executes_and_old_history_keeps_original_policy(settings, clock, monkeypatch, sequence):
    service = LimiterService(settings, clock)
    calls = []
    for name, algorithm in service.algorithms.items():
        original = algorithm.check

        def traced(state, now, name=name, original=original):
            calls.append(name)
            return original(state, now)

        monkeypatch.setattr(algorithm, "check", traced)
    previous_user = None
    for index, name in enumerate(sequence):
        config = service.switch(name, service.configuration()["policy_version"])
        calls.clear()
        user_id = f"user-{index}"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: service.check(user_id), range(12)))
        assert calls == [name] * 12
        assert {result["algorithm"] for result in results} == {name}
        assert {result["policy_version"] for result in results} == {config["policy_version"]}
        assert sum(result["allowed"] for result in results) == 3
        rows = {row["user_id"]: row for row in service.dashboard()["users"]}
        assert rows[user_id]["algorithm"] == name
        assert rows[user_id]["policy_status"] == "Current policy"
        if previous_user:
            assert rows[previous_user]["policy_status"] == "Earlier policy"
        previous_user = user_id


def test_bundled_config_is_valid():
    assert load_settings().default_algorithm == "token_bucket"


@pytest.mark.parametrize("change", ["unknown_algorithm", "missing_field", "extra_field", "nested_extra"])
def test_config_schema_rejects_typographical_errors(settings, change):
    values = settings.to_dict()
    if change == "unknown_algorithm":
        values["default_algorithm"] = "other"
    elif change == "missing_field":
        del values["storage"]
    elif change == "extra_field":
        values["limits"] = {}
    else:
        values["token_bucket"]["capcity"] = 3
    with pytest.raises(ConfigError):
        Settings.from_dict(values)

