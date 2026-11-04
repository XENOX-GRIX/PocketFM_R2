"""Streamlit -> HTTP adapter -> service -> UI, with WSGI and live transports."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError

import pytest
from streamlit.testing.v1 import AppTest

from conftest import ROOT

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("algorithm", ["token_bucket", "fixed_window", "sliding_window"])
@pytest.mark.live
def test_live_http_workflow(live_backend, algorithm):
    _, client, _ = live_backend
    selected = client.put("/admin/algorithm", json={"algorithm": algorithm, "expected_version": 1})
    assert selected.status_code == 200
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post("/v1/rate-limit/check", json={"user_id": "alice"}), range(12)))
    assert sorted(response.status_code for response in responses) == [200] * 3 + [429] * 9
    assert client.post("/v1/rate-limit/check", json={"user_id": "bob"}).status_code == 200
    data = client.get("/admin/dashboard").json()
    assert data["users"][0]["rejection_count"] == 9
    assert data["users"][0]["algorithm"] == algorithm
    assert data["metrics"]["allowed"] == 4
    other = "fixed_window" if algorithm != "fixed_window" else "sliding_window"
    client.put("/admin/algorithm", json={"algorithm": other, "expected_version": selected.json()["policy_version"]})
    response = client.post("/v1/rate-limit/check", json={"user_id": "alice"})
    assert response.status_code == 200
    assert response.json()["algorithm"] == other
    assert client.get("/admin/dashboard").json()["users"][0]["rejection_count"] == 9


@pytest.mark.parametrize("algorithm", ["token_bucket", "fixed_window", "sliding_window"])
def test_streamlit_switch_burst_and_rejection_table(inprocess_backend, algorithm):
    client, config_path = inprocess_backend
    original = config_path.read_text()
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=15).run()
    assert not app.exception
    app.selectbox(key="algorithm").select(algorithm)
    next(button for button in app.button if button.label == "Apply algorithm").click().run()
    assert not app.exception
    assert client.get("/admin/config").get_json()["active_algorithm"] == algorithm
    app.text_input(key="user_id").set_value("ui-user")
    app.number_input(key="burst_count").set_value(5)
    next(button for button in app.button if button.label == "Send requests").click().run()
    assert not app.exception
    assert any("3 allowed · 2 rate-limited" in notice.value for notice in app.info)
    rows = [table.value for table in app.dataframe if "rejection_count" in table.value.columns]
    assert len(rows) == 1
    assert rows[0].iloc[0]["user_id"] == "ui-user"
    assert rows[0].iloc[0]["rejection_count"] == 2
    app.text_input(key="search").set_value("missing-user").run()
    assert not app.exception
    assert any("No rate-limited users" in notice.value for notice in app.info)
    assert config_path.read_text() == original
    assert json.loads(original)["default_algorithm"] == "token_bucket"


def test_streamlit_reports_backend_connection_failure(monkeypatch):
    def offline(*args, **kwargs):
        raise URLError("Test backend is unavailable")

    monkeypatch.setattr("dashboard_client.urlopen", offline)
    app = AppTest.from_file(str(ROOT / "dashboard.py")).run()
    assert not app.exception
    assert "Cannot reach the backend" in app.error[0].value


def test_streamlit_stale_admin_form_cannot_overwrite_new_selection(inprocess_backend):
    client, _ = inprocess_backend
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=15).run()
    client.put("/admin/algorithm", json={"algorithm": "fixed_window", "expected_version": 1})
    app.selectbox(key="algorithm").select("sliding_window")
    next(button for button in app.button if button.label == "Apply algorithm").click().run()
    assert not app.exception
    assert any("HTTP 409" in error.value for error in app.error)
    assert client.get("/admin/config").get_json()["active_algorithm"] == "fixed_window"


def test_streamlit_scrolls_latest_thousand_distinct_users_for_entire_session(inprocess_backend, clock):
    client, _ = inprocess_backend
    for index in range(1002):
        for _ in range(4):
            response = client.post("/v1/rate-limit/check", json={"user_id": f"user-{index:04d}"})
        assert response.status_code == 429
    # Promote a user on another rejection, with exactly the same timestamp.
    client.post("/v1/rate-limit/check", json={"user_id": "user-0002"})
    clock.advance(3601)  # Older than even the longest selectable period / event retention.
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=15).run()
    assert not app.exception
    assert app.selectbox(key="period").value is None
    assert not any(element.label == "Page" for element in app.number_input)
    table = app.dataframe[0]
    assert len(table.value) == 1000
    assert table.value.iloc[0]["user_id"] == "user-0002"
    assert table.value.iloc[1]["user_id"] == "user-1001"
    assert "user-0000" not in set(table.value["user_id"])
    assert "user-0001" not in set(table.value["user_id"])
    app.text_input(key="search").set_value("user-0003").run()
    assert list(app.dataframe[0].value["user_id"]) == ["user-0003"]


def test_switch_clears_old_burst_and_labels_history_then_new_requests_use_active_policy(inprocess_backend):
    client, _ = inprocess_backend
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=15).run()
    app.number_input(key="burst_count").set_value(5)
    next(button for button in app.button if button.label == "Send requests").click().run()
    assert set(app.dataframe[0].value["algorithm"]) == {"token_bucket"}
    app.selectbox(key="algorithm").select("fixed_window")
    next(button for button in app.button if button.label == "Apply algorithm").click().run()
    assert not app.exception
    assert not any("Last burst:" in notice.value for notice in app.info)
    assert len(app.dataframe) == 1
    history = app.dataframe[0].value
    assert history.iloc[0]["algorithm"] == "token_bucket"
    assert history.iloc[0]["policy_status"] == "Earlier policy"
    assert json.loads(app.dataframe[0].proto.columns)["algorithm"]["label"] == "Last rejection algorithm"
    next(button for button in app.button if button.label == "Send requests").click().run()
    assert not app.exception
    assert set(app.dataframe[0].value["algorithm"]) == {"fixed_window"}
    assert set(app.dataframe[0].value["policy_version"]) == {2}
    assert app.dataframe[1].value.iloc[0]["policy_status"] == "Current policy"


def test_monitoring_detects_external_switch_and_refreshes_entire_dashboard(inprocess_backend, monkeypatch):
    from dashboard_client import BackendClient

    client, _ = inprocess_backend
    original = BackendClient.dashboard
    switched = False

    def change_before_snapshot(self, *args, **kwargs):
        nonlocal switched
        if not switched:
            switched = True
            client.put("/admin/algorithm", json={"algorithm": "sliding_window", "expected_version": 1})
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BackendClient, "dashboard", change_before_snapshot)
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=15).run()
    assert not app.exception
    assert app.sidebar.success[0].value == "Rolling timestamp queue"
    assert app.selectbox(key="algorithm").value == "sliding_window"
    assert any("Rolling timestamp queue" in caption.value and "policy v2" in caption.value for caption in app.caption)


def test_streamlit_handles_backend_that_has_not_been_restarted(inprocess_backend, monkeypatch):
    from dashboard_client import BackendClient

    original = BackendClient.configuration

    def old_configuration(self):
        config = original(self)
        del config["session_id"]
        return config

    monkeypatch.setattr(BackendClient, "configuration", old_configuration)
    app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=15).run()
    assert not app.exception
    assert "Restart the backend" in app.warning[0].value

