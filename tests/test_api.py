import pytest
from flask import abort

from limiter.api import create_app
from limiter.config import Settings


@pytest.fixture
def client(settings, clock):
    with create_app(settings, clock).test_client() as client:
        yield client


@pytest.mark.parametrize("algorithm", ["token_bucket", "fixed_window", "sliding_window"])
def test_check_reject_wait_recover_and_observe(client, clock, algorithm):
    response = client.put("/admin/algorithm", json={"algorithm": algorithm, "expected_version": 1})
    assert response.status_code == 200
    for remaining in [2, 1, 0]:
        response = client.post("/v1/rate-limit/check", json={"user_id": "alice"})
        assert response.status_code == 200
        assert response.get_json()["remaining"] == remaining
    response = client.post("/v1/rate-limit/check", json={"user_id": "alice"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["algorithm"] == algorithm
    snapshot = client.get("/admin/dashboard").get_json()
    assert snapshot["users"][0]["user_id"] == "alice"
    assert snapshot["metrics"]["allowed"] == 3
    clock.advance(response.get_json()["retry_after_ms"] / 1000)
    assert client.post("/v1/rate-limit/check", json={"user_id": "alice"}).status_code == 200


@pytest.mark.parametrize("payload", [{}, {"user_id": ""}, {"user_id": "  "}, {"user_id": 5},
                                     {"user_id": "a\nb"}, {"user_id": "x" * 129},
                                     {"user_id": "alice", "limit": 1000}])
def test_invalid_requests_do_not_consume_allowance(client, payload):
    assert client.post("/v1/rate-limit/check", json=payload).status_code == 422
    assert client.get("/admin/dashboard").get_json()["metrics"]["tracked_users"] == 0


def test_trimmed_identity_uses_same_allowance(client):
    client.post("/v1/rate-limit/check", json={"user_id": " alice "})
    assert client.post("/v1/rate-limit/check", json={"user_id": "alice"}).get_json()["remaining"] == 1


def test_admin_validation_and_stale_updates(client):
    assert client.put("/admin/algorithm", json={"algorithm": "unknown", "expected_version": 1}).status_code == 422
    assert client.put("/admin/algorithm", json={"algorithm": "fixed_window", "expected_version": 1}).status_code == 200
    assert client.put("/admin/algorithm", json={"algorithm": "sliding_window", "expected_version": 1}).status_code == 409
    assert client.get("/admin/config").get_json()["active_algorithm"] == "fixed_window"
    for query in ["limit=0", "offset=-1", "lookback_seconds=nan", "lookback_seconds=inf"]:
        assert client.get("/admin/dashboard?" + query).status_code == 422


def test_session_dashboard_default_and_thousand_user_cap(client, clock):
    for _ in range(4):
        client.post("/v1/rate-limit/check", json={"user_id": "alice"})
    clock.advance(61)
    snapshot = client.get("/admin/dashboard?limit=1000").get_json()
    assert snapshot["scope"] == "session"
    assert snapshot["users"][0]["user_id"] == "alice"
    assert client.get("/admin/dashboard?lookback_seconds=60").get_json()["users"] == []
    assert client.get("/admin/dashboard?limit=1001").status_code == 422


def test_request_cannot_choose_an_inactive_algorithm(client):
    response = client.post("/v1/rate-limit/check", json={"user_id": "alice", "algorithm": "sliding_window"})
    assert response.status_code == 422
    assert client.get("/admin/config").get_json()["active_algorithm"] == "token_bucket"


def test_authentication_can_be_added_without_changing_limiter(client):
    def deny():
        abort(403, description="Admin required")

    client.application.config["ADMIN_GUARD"] = deny
    assert client.get("/admin/config").status_code == 403
    assert client.get("/admin/dashboard").status_code == 403
    assert client.put("/admin/algorithm", json={"algorithm": "fixed_window", "expected_version": 1}).status_code == 403
    client.application.config["IDENTITY_RESOLVER"] = lambda: "authenticated-user"
    assert client.post("/v1/rate-limit/check").get_json()["user_id"] == "authenticated-user"


def test_memory_capacity_returns_unavailable_not_rate_limited(settings, clock):
    values = settings.to_dict()
    values["storage"]["max_users"] = 1
    with create_app(Settings.from_dict(values), clock).test_client() as client:
        assert client.post("/v1/rate-limit/check", json={"user_id": "alice"}).status_code == 200
        response = client.post("/v1/rate-limit/check", json={"user_id": "bob"})
        assert response.status_code == 503
        assert "Retry-After" not in response.headers

