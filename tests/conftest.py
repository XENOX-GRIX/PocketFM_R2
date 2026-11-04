import json
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from limiter.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings():
    return Settings.from_dict({
        "default_algorithm": "token_bucket",
        "token_bucket": {"capacity": 3, "refill_per_second": 1.0},
        "fixed_window": {"limit": 3, "window_seconds": 10.0},
        "sliding_window": {"limit": 3, "window_seconds": 10.0},
        "storage": {"max_users": 100, "idle_ttl_seconds": 60.0,
                    "max_events": 100, "event_retention_seconds": 60.0},
    })


@pytest.fixture
def scenario_config(tmp_path, settings):
    # Slow refill/long windows keep UI burst assertions independent of machine speed.
    values = settings.to_dict()
    values["token_bucket"]["refill_per_second"] = 0.01
    for name in ("fixed_window", "sliding_window"):
        values[name]["window_seconds"] = 3600.0
    values["storage"]["idle_ttl_seconds"] = 7200.0
    values["storage"]["max_users"] = 2000
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values))
    return config


@pytest.fixture
def inprocess_backend(scenario_config, clock, monkeypatch):
    """Run the actual Flask routes over WSGI; replace only the network transport."""
    from limiter.api import create_app
    app = create_app(load_settings(scenario_config), clock)
    app.testing = True
    client = app.test_client()

    def urlopen_wsgi(req, timeout=5):
        url = urlsplit(req.full_url)
        response = client.open(url.path + ("?" + url.query if url.query else ""),
                               method=req.get_method(), data=req.data, headers=dict(req.header_items()))
        body = io.BytesIO(response.data)
        if response.status_code >= 400:
            raise HTTPError(req.full_url, response.status_code, response.status, response.headers, body)
        return body

    monkeypatch.setattr("dashboard_client.urlopen", urlopen_wsgi)
    monkeypatch.setenv("RATE_LIMIT_API_URL", "http://inprocess.test")
    return client, scenario_config


class LiveClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def request(self, method, path, **kwargs):
        import requests
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method, self.base_url + path, timeout=3, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)


@pytest.fixture
def live_backend(tmp_path, scenario_config):
    """Real isolated HTTP server per test, with no production reset/test endpoints."""
    import requests
    config = scenario_config
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "RATE_LIMIT_CONFIG": str(config)}
    with (tmp_path / "backend.log").open("w+") as log:
        process = subprocess.Popen(
            # Freeze application time so a real fixed-window boundary cannot make
            # concurrent admission assertions flaky. HTTP and application are real.
            [sys.executable, "-c",
             "from limiter.api import create_app; "
             "create_app(clock=lambda: 100.0).run(host='127.0.0.1', "
             f"port={port}, threaded=True, use_reloader=False)"],
            cwd=ROOT, env=env, stdout=log, stderr=log,
        )
        try:
            client = LiveClient(base_url)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail("Backend exited during startup: " + log.read())
                try:
                    if client.get("/health").status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Backend did not become healthy")
            yield base_url, client, config
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

