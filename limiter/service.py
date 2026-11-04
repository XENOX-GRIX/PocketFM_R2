"""One process owns all state. A lock makes decisions and policy switches atomic."""

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

from .algorithms import FixedWindow, SlidingWindow, TokenBucket
from .config import AlgorithmName, Settings


class StableClock:
    """Epoch-aligned time progressing monotonically despite wall-clock adjustments."""

    def __init__(self):
        self.epoch = time.time()
        self.start = time.monotonic()

    def __call__(self):
        return self.epoch + time.monotonic() - self.start


class CapacityExceeded(Exception):
    pass


class PolicyConflict(Exception):
    pass


@dataclass
class UserState:
    value: Any
    last_seen: float


def iso_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class LimiterService:
    def __init__(self, settings: Settings, clock: Optional[Callable[[], float]] = None):
        self.settings = settings
        self.clock = clock or StableClock()
        self.lock = threading.Lock()
        self.algorithms = {
            "token_bucket": TokenBucket(settings.token_bucket),
            "fixed_window": FixedWindow(settings.fixed_window),
            "sliding_window": SlidingWindow(settings.sliding_window),
        }
        self.active_algorithm = settings.default_algorithm
        self.policy_version = 1
        self.changed_at = self.clock()
        self.started_at = self.changed_at
        self.session_id = uuid4().hex
        self.users = OrderedDict()
        # Latest distinct rejected users, independent of raw-event expiry/churn.
        self.session_users = OrderedDict()
        self.events = deque(maxlen=settings.storage.max_events)
        self.allowed_count = 0
        self.rejected_count = 0

    def _prune(self, now):
        # Ordered by last access: idle cleanup costs only the number expired.
        cutoff = now - self.settings.storage.idle_ttl_seconds
        while self.users:
            first = next(iter(self.users))
            if self.users[first].last_seen > cutoff:
                break
            self.users.popitem(last=False)
        event_cutoff = now - self.settings.storage.event_retention_seconds
        while self.events and self.events[0]["timestamp"] <= event_cutoff:
            self.events.popleft()

    def check(self, user_id: str):
        with self.lock:
            now = self.clock()
            self._prune(now)
            algorithm = self.algorithms[self.active_algorithm]
            entry = self.users.get(user_id)
            if entry is None:
                if len(self.users) >= self.settings.storage.max_users:
                    raise CapacityExceeded("In-memory user capacity reached; retry after idle users expire")
                entry = UserState(algorithm.new_state(now), now)
                self.users[user_id] = entry
            entry.last_seen = now
            self.users.move_to_end(user_id)
            decision = algorithm.check(entry.value, now)
            result = {
                "user_id": user_id,
                "allowed": decision.allowed,
                "algorithm": self.active_algorithm,
                "remaining": decision.remaining,
                "retry_after_ms": max(0, math.ceil(decision.retry_after_seconds * 1000)),
                "policy_version": self.policy_version,
                "session_id": self.session_id,
            }
            if decision.allowed:
                self.allowed_count += 1
            else:
                self.rejected_count += 1
                self.events.append({
                    **result,
                    "timestamp": now,
                    "reason": "rate_limit_exceeded",
                })
                row = self.session_users.pop(user_id, {"user_id": user_id, "rejection_count": 0})
                row.update({
                    "rejection_count": row["rejection_count"] + 1,
                    "last_rejection": iso_time(now),
                    "algorithm": self.active_algorithm,
                    "policy_version": self.policy_version,
                    "reason": "rate_limit_exceeded",
                    "last_retry_after_ms": result["retry_after_ms"],
                })
                self.session_users[user_id] = row
                if len(self.session_users) > self.settings.storage.max_session_users:
                    self.session_users.popitem(last=False)
            return result

    def switch(self, algorithm: AlgorithmName, expected_version: int):
        with self.lock:
            if expected_version != self.policy_version:
                raise PolicyConflict("Configuration changed. Refresh before applying your selection.")
            if algorithm not in self.algorithms:
                raise ValueError("Unknown algorithm")
            if algorithm != self.active_algorithm:
                self.active_algorithm = algorithm
                self.policy_version += 1
                self.changed_at = self.clock()
                self.users.clear()
            return self._configuration()

    def _configuration(self):
        return {
            "active_algorithm": self.active_algorithm,
            "policy_version": self.policy_version,
            "changed_at": iso_time(self.changed_at),
            "session_id": self.session_id,
            "started_at": iso_time(self.started_at),
            "settings": self.settings.to_dict(),
        }

    def configuration(self):
        with self.lock:
            return self._configuration()

    def dashboard(self, search: str = "", lookback_seconds: Optional[float] = None,
                  limit: int = 1000, offset: int = 0):
        with self.lock:
            now = self.clock()
            self._prune(now)
            grouped = OrderedDict()
            for event in self.events:
                if lookback_seconds is None:
                    break
                if event["timestamp"] < now - lookback_seconds or search not in event["user_id"]:
                    continue
                user_id = event["user_id"]
                if user_id not in grouped:
                    grouped[user_id] = {"user_id": user_id, "rejection_count": 0}
                row = grouped[user_id]
                row.update({
                    "rejection_count": row["rejection_count"] + 1,
                    "last_rejection": iso_time(event["timestamp"]),
                    "algorithm": event["algorithm"],
                    "policy_version": event["policy_version"],
                    "reason": event["reason"],
                    "last_retry_after_ms": event["retry_after_ms"],
                })
                grouped.move_to_end(user_id)
            source = self.session_users if lookback_seconds is None else grouped
            # Arrival order breaks timestamp ties correctly (including sub-microsecond ties).
            rows = [dict(row, policy_status=("Current policy" if row["policy_version"] == self.policy_version
                                            else "Earlier policy"))
                    for row in reversed(source.values()) if search in row["user_id"]]
            return {
                "configuration": self._configuration(),
                "metrics": {
                    "allowed": self.allowed_count,
                    "rejected": self.rejected_count,
                    "tracked_users": len(self.users),
                    "retained_events": len(self.events),
                },
                "users": rows[offset:offset + limit],
                "total_users": len(rows),
                "limit": limit,
                "offset": offset,
                "scope": "session" if lookback_seconds is None else "period",
            }

