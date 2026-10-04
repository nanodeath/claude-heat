"""Settings, loaded from a TOML file with defaults for anything left out."""

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # Seconds between CPU samples
    interval: float = 2.0

    # Seconds of samples averaged before a session turns hot or cools down
    window: float = 10.0

    # Cores needed for one, two, three... flames; the first marks a session hot
    flame_thresholds: tuple[float, ...] = (1.5, 4.0, 8.0)

    # A hot session cools down once its average drops below this
    cool_cores: float = 0.5

    # Repeated once per threshold reached
    flame: str = "🔥"

    # Keep Claude Code's ✳ / ◐ / ◑ status glyph in titles we write
    status_glyphs: bool = True

    # Credit Docker Compose containers to the session in their project directory
    docker: bool = True

    @property
    def window_samples(self) -> int:
        return max(1, round(self.window / self.interval))

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ConfigError("interval must be positive")
        if self.window < self.interval:
            raise ConfigError("window must be at least one interval")
        if not self.flame_thresholds:
            raise ConfigError("flame_thresholds needs at least one value")
        if list(self.flame_thresholds) != sorted(set(self.flame_thresholds)):
            raise ConfigError("flame_thresholds must be strictly increasing")
        if not 0 <= self.cool_cores < self.flame_thresholds[0]:
            raise ConfigError("cool_cores must be below the first flame threshold")


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "claude-heat" / "config.toml"


def load(path: Path | None = None) -> Config:
    """Load settings from `path`, or from the default path if it exists."""
    if path is None:
        path = default_path()
        if not path.exists():
            return Config()

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse(raw, source=str(path))


def parse(raw: dict, source: str = "config") -> Config:
    known = {f.name: f for f in fields(Config)}
    unknown = raw.keys() - known.keys()
    if unknown:
        raise ConfigError(f"{source}: unknown option(s): {', '.join(sorted(unknown))}")

    values = {}
    for name, value in raw.items():
        default = getattr(Config, name)
        values[name] = _coerce(name, value, default, source)
    return Config(**values)


def _coerce(name: str, value, default, source: str):
    def bad() -> ConfigError:
        return ConfigError(f"{source}: {name} must be {type(default).__name__}, got {value!r}")

    # TOML ints are fine where a float is expected, but bools are not numbers
    def number(v) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise bad()
        return float(v)

    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise bad()
        return value
    if isinstance(default, float):
        return number(value)
    if isinstance(default, tuple):
        if not isinstance(value, list):
            raise bad()
        return tuple(number(v) for v in value)
    if not isinstance(value, type(default)):
        raise bad()
    return value
