#!/usr/bin/env python3
"""Exercise a running rate limiter using only Python's standard library.

Start the API separately, then run:
    python3 scripts/test_multi_user.py
    python3 scripts/test_multi_user.py --durations 20,40,60 --base-url http://127.0.0.1:8000

Ten concurrent users keep distinct request intervals (seconds between starts).
Rounds grow in duration; each ends with a full cooldown and recovery checks.
IDs are unique to each run. Only GET /admin/config and POST /v1/rate-limit/check
are used. Checks consume real quota and update service metrics/history.

Logs include every response, latency, validation errors, and per-user totals.
The output directory also contains a machine-readable summary.json.
Exit codes: 0 = checks passed with rejection/recovery coverage; 1 = failure;
2 = insufficient coverage (increase duration/frequency); 130 = interrupted.

This is a live smoke/correctness test, not a precise load benchmark. Requests
are sequential within each user and concurrent across users; missed send slots
are skipped. Timing assertions use request/response bounds, not exact server
timestamps. Fixed-window checks allow boundary bursts of twice the limit;
exact boundary behavior still requires the existing deterministic unit tests.
Keep the active policy unchanged during a run. No automatic HTTP retries occur.
"""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid


LOG = logging.getLogger("multi_user")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def numbers(value):
    try:
        return [positive(part) for part in value.split(",")]
    except (ValueError, argparse.ArgumentTypeError) as exc:
        raise argparse.ArgumentTypeError("use comma-separated positive numbers") from exc


def http(base_url, path, timeout, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(base_url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        response = exc  # A 429 is a normal rate-limit decision, not a transport error.
    with response:
        raw = response.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"invalid_json": raw[:1000]}
        return response.status, dict(response.headers.items()), body


class TimingChecks:
    """Independent, conservative checks; never imports service implementation."""

    def __init__(self, algorithm, policy, capacity):
        self.algorithm, self.policy, self.capacity = algorithm, policy, capacity
        self.low = self.high = float(capacity)
        self.previous = None
        self.admitted = deque()

    def check(self, start, end, body):
        errors = []
        allowed, remaining = body["allowed"], body["remaining"]
        if self.algorithm == "token_bucket":
            rate = self.policy["refill_per_second"]
            if self.previous is not None:
                before, after = self.previous
                self.low = min(self.capacity, self.low + rate * max(0, start - after))
                self.high = min(self.capacity, self.high + rate * (end - before))
            if allowed:
                if self.high < 1 - 1e-6:
                    errors.append("admitted without enough possible tokens")
                self.low, self.high = max(0, self.low - 1), max(0, self.high - 1)
                if not math.floor(self.low - 1e-6) <= remaining <= math.floor(self.high + 1e-6):
                    errors.append("remaining outside possible token balance")
            elif self.low >= 1 + 1e-6:
                errors.append("rejected despite a guaranteed available token")
            self.previous = (start, end)
        else:
            window = self.policy["window_seconds"]
            # Keep admissions that could still fall in the current rolling window.
            while self.admitted and self.admitted[0][1] <= start - window:
                self.admitted.popleft()
            possible = len(self.admitted)
            certain = sum(before > end - window for before, _ in self.admitted)
            if self.algorithm == "sliding_window":
                if allowed and certain >= self.capacity:
                    errors.append("admitted above the rolling-window limit")
                if not allowed and possible < self.capacity:
                    errors.append("rejected below the rolling-window limit")
                if allowed and not max(0, self.capacity - possible - 1) <= remaining <= self.capacity - certain - 1:
                    errors.append("remaining outside possible rolling-window allowance")
            else:
                if allowed and certain >= 2 * self.capacity:
                    errors.append("exceeded two fixed-window allowances within one window duration")
                if not allowed and possible < self.capacity:
                    errors.append("rejected below any possible fixed-window limit")
                if allowed and remaining < max(0, self.capacity - possible - 1):
                    errors.append("remaining below any possible fixed-window allowance")
            if allowed:
                self.admitted.append((start, end))
        return errors


class User:
    def __init__(self, user_id, interval, runner):
        self.user_id, self.interval, self.runner = user_id, interval, runner
        self.allowed = self.rejected = self.errors = self.requests = self.recoveries = 0
        self.latency_total = self.latency_max = 0.0
        self.oracle = self.new_oracle()

    def new_oracle(self):
        return TimingChecks(self.runner.algorithm, self.runner.policy, self.runner.capacity)

    def send(self, phase, fresh=False):
        start = time.monotonic()
        errors, body, status = [], None, None
        try:
            status, headers, body = http(self.runner.args.base_url, "/v1/rate-limit/check",
                                         self.runner.args.timeout, {"user_id": self.user_id})
            end = time.monotonic()
            errors = self.runner.validate(self.user_id, status, headers, body)
            if not errors:
                if fresh and (not body["allowed"] or body["remaining"] != self.runner.capacity - 1):
                    errors.append("fresh/recovered user did not receive a full independent allowance")
                if self.oracle is not None:
                    errors.extend(self.oracle.check(start, end, body))
                if body["allowed"]:
                    self.allowed += 1
                else:
                    self.rejected += 1
                if fresh and phase.startswith("recovery") and not errors:
                    self.recoveries += 1
            else:
                self.oracle = None
        except Exception as exc:
            end = time.monotonic()
            errors.append(f"transport/client error: {type(exc).__name__}: {exc}")
            # A lost response may have consumed quota. Resume timing checks only
            # after a full cooldown; do not retry and accidentally consume twice.
            self.oracle = None
        latency = (end - start) * 1000
        self.requests += 1
        self.errors += len(errors)
        self.latency_total += latency
        self.latency_max = max(self.latency_max, latency)
        LOG.log(logging.ERROR if errors else logging.INFO,
                "phase=%s user=%s request=%d interval_s=%.3f status=%s latency_ms=%.2f response=%s errors=%s",
                phase, self.user_id, self.requests, self.interval, status, latency,
                json.dumps(body, ensure_ascii=True), json.dumps(errors))

    def run(self, phase, start, deadline):
        due = start
        while not self.runner.stop.is_set():
            if self.runner.stop.wait(max(0, due - time.monotonic())):
                break
            if time.monotonic() >= deadline:
                break
            self.send(phase)
            due += self.interval
            now = time.monotonic()
            if due < now:
                due += (math.floor((now - due) / self.interval) + 1) * self.interval

    def summary(self):
        return {"user_id": self.user_id, "interval_seconds": self.interval,
                "requests": self.requests, "allowed": self.allowed, "rejected": self.rejected,
                "validation_errors": self.errors, "recovery_checks_passed": self.recoveries,
                "mean_latency_ms": round(self.latency_total / max(1, self.requests), 2),
                "max_latency_ms": round(self.latency_max, 2)}


class Runner:
    def __init__(self, args, config):
        self.args, self.config = args, config
        self.algorithm = config["active_algorithm"]
        if self.algorithm not in ("token_bucket", "fixed_window", "sliding_window"):
            raise ValueError(f"unsupported algorithm: {self.algorithm}")
        self.policy = config["settings"][self.algorithm]
        self.capacity = self.policy["capacity" if self.algorithm == "token_bucket" else "limit"]
        self.recovery = (self.capacity / self.policy["refill_per_second"]
                         if self.algorithm == "token_bucket" else self.policy["window_seconds"])
        self.stop = threading.Event()
        prefix = "probe-" + uuid.uuid4().hex[:16]
        self.users = [User(f"{prefix}-{index:02d}", interval, self)
                      for index, interval in enumerate(args.intervals, 1)]

    def validate(self, user_id, status, headers, body):
        if status not in (200, 429):
            return [f"unexpected HTTP status {status}"]
        if not isinstance(body, dict):
            return ["response must be a JSON object"]
        errors = []
        for key, expected in (("user_id", user_id), ("algorithm", self.algorithm),
                              ("policy_version", self.config["policy_version"]),
                              ("allowed", status == 200)):
            if type(body.get(key)) is not type(expected) or body[key] != expected:
                errors.append(f"{key}: expected {expected!r}, got {body.get(key)!r}")
        for key in ("remaining", "retry_after_ms"):
            if type(body.get(key)) is not int or body[key] < 0:
                errors.append(f"{key} must be a nonnegative integer")
        if errors:
            return errors
        if body["remaining"] >= self.capacity:
            errors.append("remaining exceeds maximum post-decision allowance")
        if status == 200 and body["retry_after_ms"] != 0:
            errors.append("allowed decision must have zero retry delay")
        if status == 429:
            max_retry = (1 / self.policy["refill_per_second"]
                         if self.algorithm == "token_bucket" else self.recovery)
            if body["remaining"] != 0 or not 1 <= body["retry_after_ms"] <= math.ceil(max_retry * 1000):
                errors.append("rejection has invalid remaining/retry delay")
            headers = {key.lower(): value for key, value in headers.items()}
            expected = str(max(1, math.ceil(body["retry_after_ms"] / 1000)))
            if headers.get("retry-after") != expected:
                errors.append(f"Retry-After must equal {expected}")
        return errors

    def run(self):
        LOG.info("algorithm=%s version=%s policy=%s users=10 durations_s=%s cooldown_s=%.3f",
                 self.algorithm, self.config["policy_version"], self.policy,
                 self.args.durations, self.recovery + self.args.cooldown_padding)
        pool = ThreadPoolExecutor(max_workers=10)
        try:
            for future in [pool.submit(user.send, "initial", True) for user in self.users]:
                future.result()
            for index, duration in enumerate(self.args.durations, 1):
                LOG.info("ROUND %d/%d duration_s=%.3f", index, len(self.args.durations), duration)
                start = time.monotonic() + 0.05
                futures = [pool.submit(user.run, f"round-{index}", start, start + duration)
                           for user in self.users]
                for future in futures:
                    future.result()
                LOG.info("COOLDOWN %.3fs; checking full recovery for all users afterward",
                         self.recovery + self.args.cooldown_padding)
                self.stop.wait(self.recovery + self.args.cooldown_padding)
                for user in self.users:
                    user.oracle = user.new_oracle()
                for future in [pool.submit(user.send, f"recovery-{index}", True) for user in self.users]:
                    future.result()
        finally:
            self.stop.set()
            pool.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--durations", type=numbers, default=numbers("10,20,30"), help="increasing round durations in seconds")
    parser.add_argument("--intervals", type=numbers, default=numbers("0.05,0.1,0.15,0.25,0.4,0.6,0.8,1,1.5,2"), help="exactly 10 user request intervals in seconds")
    parser.add_argument("--timeout", type=positive, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument("--cooldown-padding", type=positive, default=0.25, help="extra seconds after full quota recovery")
    parser.add_argument("--output-dir", type=Path, help="new directory for run.log and summary.json")
    args = parser.parse_args()
    if len(args.intervals) != 10:
        parser.error("--intervals must contain exactly 10 values")
    if any(right <= left for left, right in zip(args.durations, args.durations[1:])):
        parser.error("--durations must be strictly increasing")
    args.base_url = args.base_url.rstrip("/")
    output = args.output_dir or Path("test-results") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(output / "run.log", encoding="utf-8")])
    logging.Formatter.converter = time.gmtime
    LOG.info("START base_url=%s output=%s", args.base_url, output.resolve())
    runner, fatal, code = None, None, 1
    try:
        status, _, config = http(args.base_url, "/admin/config", args.timeout)
        if status != 200:
            raise RuntimeError(f"cannot read active configuration: HTTP {status}: {config}")
        runner = Runner(args, config)
        runner.run()
        status, _, final_config = http(args.base_url, "/admin/config", args.timeout)
        if status != 200 or final_config != config:
            raise RuntimeError("policy/configuration changed during the run; results are invalid")
        if any(user.errors for user in runner.users):
            code = 1
        elif not any(user.rejected for user in runner.users):
            code = 2
            LOG.warning("No 429 observed: increase round durations or reduce request intervals")
        else:
            code = 0
    except KeyboardInterrupt:
        fatal, code = "interrupted by user", 130
        LOG.warning(fatal)
    except Exception as exc:
        fatal = str(exc)
        LOG.exception("RUN FAILED: %s", exc)
    users = [] if runner is None else [user.summary() for user in runner.users]
    result = {"result": {0: "PASS", 1: "FAIL", 2: "INCOMPLETE", 130: "INTERRUPTED"}[code],
              "exit_code": code, "fatal_error": fatal, "base_url": args.base_url,
              "durations_seconds": args.durations,
              "configuration": None if runner is None else runner.config, "users": users}
    for user in users:
        LOG.info("USER SUMMARY %s", json.dumps(user))
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    LOG.info("RESULT=%s summary=%s", result["result"], (output / "summary.json").resolve())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
