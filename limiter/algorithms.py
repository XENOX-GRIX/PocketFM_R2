"""Algorithms are clock-independent. The service supplies time and synchronization."""

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from .config import TokenSettings, WindowSettings


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_seconds: float = 0.0


@dataclass
class TokenState:
    tokens: float
    updated_at: float


class TokenBucket:
    def __init__(self, settings: TokenSettings):
        self.settings = settings

    def new_state(self, now):
        return TokenState(float(self.settings.capacity), now)

    def check(self, state, now):
        state.tokens = min(
            self.settings.capacity,
            state.tokens + max(0.0, now - state.updated_at) * self.settings.refill_per_second,
        )
        state.updated_at = now
        if state.tokens >= 1.0:
            state.tokens -= 1.0
            return Decision(True, math.floor(state.tokens))
        return Decision(False, 0, (1.0 - state.tokens) / self.settings.refill_per_second)


@dataclass
class FixedWindowState:
    window: int
    count: int = 0


class FixedWindow:
    def __init__(self, settings: WindowSettings):
        self.settings = settings

    def new_state(self, now):
        return FixedWindowState(math.floor(now / self.settings.window_seconds))

    def check(self, state, now):
        window = math.floor(now / self.settings.window_seconds)
        if state.window != window:
            state.window, state.count = window, 0
        if state.count < self.settings.limit:
            state.count += 1
            return Decision(True, self.settings.limit - state.count)
        return Decision(False, 0, (window + 1) * self.settings.window_seconds - now)


@dataclass
class SlidingWindowState:
    timestamps: Deque[float] = field(default_factory=deque)


class SlidingWindow:
    """Queue of admitted timestamps in (now - duration, now]; never queues work."""

    def __init__(self, settings: WindowSettings):
        self.settings = settings

    def new_state(self, now):
        return SlidingWindowState()

    def check(self, state, now):
        cutoff = now - self.settings.window_seconds
        while state.timestamps and state.timestamps[0] <= cutoff:
            state.timestamps.popleft()
        if len(state.timestamps) < self.settings.limit:
            state.timestamps.append(now)
            return Decision(True, self.settings.limit - len(state.timestamps))
        return Decision(False, 0, state.timestamps[0] + self.settings.window_seconds - now)

