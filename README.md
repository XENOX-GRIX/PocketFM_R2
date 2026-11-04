# Rate Limiter MVP

Python + Flask backend, Streamlit admin console, static JSON configuration, and in-memory storage. Every user gets the same policy, with an independent allowance. No database or authentication is required.

## Run locally

Use Python 3.9–3.13 (3.12 recommended). From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m limiter.api
```

In a second terminal:

```sh
source .venv/bin/activate
python -m streamlit run dashboard.py
```

- Dashboard: http://127.0.0.1:8501
- Health: http://127.0.0.1:8000/health

Runtime dependencies alone are in `requirements.txt`. To use a different config path, set `RATE_LIMIT_CONFIG` for the API. To change the dashboard's backend address, set `RATE_LIMIT_API_URL` (default `http://127.0.0.1:8000`).

`requirements-lock.txt` records the complete environment verified on Python 3.9/macOS. Use `requirements-dev.txt` for other supported Python versions so compatible transitive dependencies can be selected.

The included command starts a local Flask development server on port 8000 (`--port` changes it). **Run exactly one backend process and one backend instance.** Multiple workers have independent memory and would give users multiple allowances. Restarting clears all usage, metrics, and rejection history.

## Algorithms

| Option | Implementation | Default |
| --- | --- | --- |
| Token bucket | Consume one token per admission; refill continuously, capped at capacity | Capacity 10; 2 tokens/second |
| Fixed time slot | Count admissions in epoch-aligned slots | 10 requests per 10 seconds |
| Rolling timestamp queue | Store admitted timestamps in a deque; expire timestamps at or before `now - duration` | 10 requests in the preceding 10 seconds |

The queue implementation is a **sliding-window limiter**, not a delayed-work queue. Every check returns immediately. Fixed windows intentionally allow two full allowances close together on either side of a slot boundary. Rejected requests do not consume quota or extend the rolling window. Tokens start full.

## Configuration and switching

`config.json` contains all rate-limit policies, the default algorithm, and storage bounds. Settings are validated once at API startup. Edit the file and restart the API to change limits. No user-specific overrides exist.

Window durations must be between one millisecond and one day. Counts must be positive integers; nonfinite numbers, unknown fields, and misspelled algorithm names are rejected at startup.

The dashboard selects exactly one active algorithm. Applying a different selection atomically increments the policy version and clears usage state for every user. Rejection history and lifetime counters remain. Selecting the already active algorithm is a no-op and does not reset allowances. Stale admin updates receive `409` so two sessions cannot silently overwrite each other. The static file is never rewritten; restarting restores its default selection.

Requests and switches share a lock. Every decision uses one complete policy version. Time advances using a monotonic clock anchored to epoch time at startup, avoiding quota resets from system-clock changes. Fixed-window alignment and event timestamps follow that anchored clock until restart.

Idle users expire after the configured TTL. Validation prevents a TTL shorter than any window or a complete token refill, so expiration cannot give an early allowance. Cleanup runs when checks or monitoring requests arrive. When `max_users` is reached, new identities get `503`; active users are never evicted to make space. Raw rejection events are bounded by event count and age. A separate in-memory summary keeps the latest `storage.max_session_users` distinct rejected users (default and maximum: 1,000) for the backend session. It survives event expiry and policy changes, but clears on restart; the least recently rejected user is evicted when full. Its counts cover each user's continuous time in that summary and restart if an evicted user returns. A rolling queue can hold up to `limit` timestamps per tracked user, so size limits and `max_users` together determine memory needs.

## Public decision endpoint

```sh
curl -i -X POST http://127.0.0.1:8000/v1/rate-limit/check \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"alice"}'
```

Allowed decisions return `200`; exhausted allowances return `429`:

```json
{
  "user_id": "alice",
  "allowed": false,
  "algorithm": "token_bucket",
  "remaining": 0,
  "retry_after_ms": 500,
  "policy_version": 1,
  "session_id": "<backend-session-id>"
}
```

`429` also includes `Retry-After` in whole seconds, rounded up. Retry time is an estimate assuming no competing requests and no policy change. Invalid input returns `422`. User IDs are case-sensitive, trimmed, printable strings of 1–128 characters.

An allowed check consumes one request allowance. The caller must enforce the decision before performing protected work; this service does not proxy or execute that work. Failed downstream work is not refunded. There is no idempotency key in this MVP: repeating a check is a new attempt, including a retry after a lost response.

There is **one public rate-limiting endpoint**. Dashboard operations use separate admin endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/admin/config` | Active algorithm, version, backend session ID/start time, and static settings |
| PUT | `/admin/algorithm` | Select algorithm with `algorithm` and `expected_version` |
| GET | `/admin/dashboard` | Metrics and rejection summaries; accepts `search`, `lookback_seconds`, `limit` (maximum/default 1,000), `offset`. Omit `lookback_seconds` for session summaries; supply it for retained events in a time period. |

The console defaults to **Current backend session** (since backend startup, shared across dashboard tabs). It displays a 420px-tall scrolling table with at most 1,000 users, latest rejection first, with no page selector. Equal timestamps are ordered by rejection arrival, and a new rejection moves an existing user to the top. Search operates on retained session summaries. Optional last-minute/five-minute/hour filters instead aggregate the bounded raw events; counts are labeled accordingly. The table is rejection history, not a guaranteed current block status. Global allowed/rejected counters cover the backend's lifetime, so they can exceed retained history.

Algorithm/version columns explicitly describe the **last rejection**, and each row is marked **Current policy** or **Earlier policy**. Old rows are preserved accurately when an algorithm changes. Previous burst results clear when the backend's policy or session changes; new results name the algorithm/version that actually evaluated them. The settings panels label inactive algorithms, and selecting an option does not activate it until **Apply algorithm** is submitted. Monitoring refreshes every five seconds and has a manual refresh button. If a refresh detects a policy/session change by another client, it reruns the entire UI to synchronize the sidebar, selector, and burst panel. A burst simulator sends real checks against the backend.

After updating the backend code, restart the API to load the new session-history behavior. This clears existing in-memory data. The dashboard shows a restart notice if it detects an older running backend.

## Add authentication later

`limiter/api.py` contains two replaceable authentication hooks:

- `resolve_identity`: currently accepts the supplied `user_id`; replace it with verified token/API-key identity resolution.
- `require_admin`: currently allows access; replace it with an admin-role check. It protects the whole admin blueprint.

The algorithms and state store do not depend on authentication. Add authorization headers in `dashboard_client.py` when needed. Pass `identity_resolver` and `admin_guard` to `create_app`, or configure `IDENTITY_RESOLVER` and `ADMIN_GUARD` on the Flask app. Both run in the request context and can access headers/cookies. Tests demonstrate that the hooks can be replaced without changing the limiter.

For this unauthenticated MVP, services bind to localhost. A client can otherwise spoof user IDs or switch algorithms. There is no distributed coordination, persistent history, background job processing, or user management.

## Tests

```sh
python -m pytest -q
# Deterministic algorithms, service lifecycle, configuration, and API contracts:
python -m pytest -q tests/test_algorithms.py tests/test_service.py tests/test_api.py
# Streamlit-to-backend workflows without network sockets:
python -m pytest -q -m "e2e and not live"
# Live HTTP tests (requires permission to bind a localhost port):
python -m pytest -q -m live
```

Functional tests use an injected clock for exact refill/expiry boundaries, rejection/recovery, user isolation, and concurrent admission correctness. They also cover stale switches, storage bounds, retention, invalid configuration, and future auth integration. Routing regression tests instrument all three algorithm implementations and verify that only the selected method executes after each switch, with correct response/history labels.

UI workflow tests drive Streamlit with its official `AppTest` API, routing the HTTP client through the actual Flask WSGI application in process. Only the socket transport is replaced: routes, JSON validation, policies, storage, and dashboard rendering all execute. They check selecting each algorithm, sending a burst, seeing rejected users, filtering, stale admin forms, connection failures, and keeping the static config unchanged. They also exercise 1,002 distinct users to verify the latest-1,000 cap/order, session history after event expiry, clearing stale bursts, historical-policy labels, external policy changes, and the backend restart notice. AppTest tests widget execution and rendered data; it is not a browser pixel/layout test.

Separate `live` E2E tests start an isolated backend subprocess on a free localhost port and send concurrent HTTP requests for all three algorithms. They check admission counts, rejection history, per-user isolation, and switching policies. Run the complete suite on a machine that permits localhost sockets; in a restricted environment run `python -m pytest -q -m "not live"`.

Workflow tests inject a fixed clock into the real backend, including the live server subprocess, so burst assertions cannot straddle a time-slot boundary. Exact recovery timing is covered by deterministic functional/API tests that advance the clock.

## Code map

- `limiter/algorithms.py`: token bucket, fixed window, rolling timestamp queue.
- `limiter/service.py`: atomic checks, active policy, bounded in-memory state and history.
- `limiter/config.py`: strict static config validation.
- `limiter/api.py`: HTTP contracts and replaceable authentication hooks.
- `dashboard.py` / `dashboard_client.py`: admin UI and HTTP client.
- `tests/`: functional, API, and E2E tests.

Reference APIs: [Flask testing](https://flask.palletsprojects.com/en/stable/testing/), [Streamlit AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).
