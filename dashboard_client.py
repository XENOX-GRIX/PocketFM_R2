"""Small standard-library HTTP client; all UI/backend communication passes here."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BackendError(Exception):
    pass


class BackendClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def request(self, method, path, payload=None, accept_rejection=False):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                body = json.load(exc)
            if exc.code == 429 and accept_rejection:
                return body
            raise BackendError(f"HTTP {exc.code}: {body.get('detail', body)}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BackendError(f"Cannot reach the backend at {self.base_url}. Start the API and refresh.") from exc

    def configuration(self):
        return self.request("GET", "/admin/config")

    def switch(self, algorithm, version):
        return self.request("PUT", "/admin/algorithm", {"algorithm": algorithm, "expected_version": version})

    def check(self, user_id):
        return self.request("POST", "/v1/rate-limit/check", {"user_id": user_id}, accept_rejection=True)

    def dashboard(self, search="", lookback_seconds=None, offset=0):
        params = {"search": search, "offset": offset, "limit": 1000}
        if lookback_seconds is not None:
            params["lookback_seconds"] = lookback_seconds
        query = urlencode(params)
        return self.request("GET", "/admin/dashboard?" + query)

