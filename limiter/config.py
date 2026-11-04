"""Static JSON configuration, validated once using only the standard library."""

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal

AlgorithmName = Literal["token_bucket", "fixed_window", "sliding_window"]
ALGORITHMS = ("token_bucket", "fixed_window", "sliding_window")
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config.json"


class ConfigError(ValueError):
    pass


def positive(value, name, integer=False, maximum=None):
    valid_type = type(value) is int if integer else type(value) in (int, float)
    if not valid_type or (maximum is not None and value > maximum) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive finite {'integer' if integer else 'number'}"
                          + (f" <= {maximum}" if maximum is not None else ""))


@dataclass(frozen=True)
class TokenSettings:
    capacity: int
    refill_per_second: float

    def __post_init__(self):
        positive(self.capacity, "capacity", integer=True, maximum=100000)
        positive(self.refill_per_second, "refill_per_second", maximum=100000)


@dataclass(frozen=True)
class WindowSettings:
    limit: int
    window_seconds: float

    def __post_init__(self):
        positive(self.limit, "limit", integer=True, maximum=100000)
        positive(self.window_seconds, "window_seconds", maximum=86400)
        if self.window_seconds < 0.001:
            raise ConfigError("window_seconds must be at least 0.001 (one millisecond)")


@dataclass(frozen=True)
class StorageSettings:
    max_users: int
    idle_ttl_seconds: float
    max_events: int
    event_retention_seconds: float
    max_session_users: int = 1000

    def __post_init__(self):
        positive(self.max_users, "max_users", integer=True, maximum=1000000)
        positive(self.idle_ttl_seconds, "idle_ttl_seconds", maximum=31536000)
        positive(self.max_events, "max_events", integer=True, maximum=1000000)
        positive(self.event_retention_seconds, "event_retention_seconds", maximum=31536000)
        positive(self.max_session_users, "max_session_users", integer=True, maximum=1000)


@dataclass(frozen=True)
class Settings:
    default_algorithm: AlgorithmName
    token_bucket: TokenSettings
    fixed_window: WindowSettings
    sliding_window: WindowSettings
    storage: StorageSettings

    def __post_init__(self):
        if self.default_algorithm not in ALGORITHMS:
            raise ConfigError("default_algorithm must be one of: " + ", ".join(ALGORITHMS))
        recovery = max(self.token_bucket.capacity / self.token_bucket.refill_per_second,
                       self.fixed_window.window_seconds, self.sliding_window.window_seconds)
        if self.storage.idle_ttl_seconds < recovery:
            raise ConfigError("idle_ttl_seconds must cover all windows and a full token refill")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, values):
        if not isinstance(values, dict) or set(values) != {field.name for field in fields(cls)}:
            raise ConfigError("Configuration must contain exactly: " + ", ".join(field.name for field in fields(cls)))
        try:
            return cls(
                default_algorithm=values["default_algorithm"],
                token_bucket=TokenSettings(**values["token_bucket"]),
                fixed_window=WindowSettings(**values["fixed_window"]),
                sliding_window=WindowSettings(**values["sliding_window"]),
                storage=StorageSettings(**values["storage"]),
            )
        except TypeError as exc:
            raise ConfigError(f"Invalid configuration fields: {exc}") from exc


def load_settings(path=DEFAULT_CONFIG) -> Settings:
    return Settings.from_dict(json.loads(Path(path).read_text()))

