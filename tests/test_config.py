from pathlib import Path

import pytest

from claude_heat.config import Config, ConfigError, load, parse


def test_empty_config_gives_defaults():
    assert parse({}) == Config()


def test_overrides_apply_and_ints_become_floats():
    config = parse({"interval": 1, "flame_thresholds": [2, 6], "flame": "*", "docker": False})
    assert config.interval == 1.0
    assert config.flame_thresholds == (2.0, 6.0)
    assert config.flame == "*"
    assert config.docker is False


def test_window_samples_rounds_to_whole_intervals():
    assert Config(interval=2.0, window=10.0).window_samples == 5
    assert Config(interval=3.0, window=10.0).window_samples == 3


@pytest.mark.parametrize(
    "raw",
    [
        {"intervall": 2},
        {"interval": "2"},
        {"interval": True},
        {"docker": 1},
        {"flame_thresholds": 1.5},
        {"flame_thresholds": []},
        {"flame_thresholds": [4, 2]},
        {"flame_thresholds": [2, 2]},
        {"cool_cores": 1.5},
        {"interval": 0},
        {"window": 1, "interval": 2},
    ],
)
def test_bad_values_are_rejected(raw):
    with pytest.raises(ConfigError):
        parse(raw)


def test_missing_default_file_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load() == Config()


def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path / "nope.toml")


def test_loads_toml_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('cool_cores = 0.25\nflame = "♨"\n')
    config = load(path)
    assert config.cool_cores == 0.25
    assert config.flame == "♨"


def test_example_config_matches_defaults():
    example = Path(__file__).parent.parent / "config.example.toml"
    assert load(example) == Config()
