"""Tests for config parsing helpers."""

from daemon.config import APIConfig


class TestEffectiveBindAddress:
    def test_defaults_to_advertised_address(self):
        cfg = APIConfig(address="localhost:4223")
        assert cfg.effective_bind_address() == "localhost:4223"

    def test_bind_address_overrides_when_set(self):
        # Container case: listen on all interfaces so the published port works,
        # while agents inside the container still dial localhost.
        cfg = APIConfig(address="localhost:4223", bind_address="0.0.0.0:4223")
        assert cfg.effective_bind_address() == "0.0.0.0:4223"
        assert cfg.address == "localhost:4223"

    def test_empty_bind_address_is_not_treated_as_a_bind_target(self):
        cfg = APIConfig(address="localhost:4223", bind_address="")
        assert cfg.effective_bind_address() == "localhost:4223"


class TestBindResolutionOrder:
    """Exercises APIConfig.resolve_bind_address, the same call daemon.main makes.

    Two mechanisms for binding 0.0.0.0 landed independently — the DEVTEAM_API_ADDRESS
    env override and the api.bind_address config field — and collapsing them would
    silently break one of the two documented paths.
    """

    def test_config_bind_address_beats_advertised(self):
        cfg = APIConfig(address="localhost:4223", bind_address="0.0.0.0:4223")
        assert cfg.resolve_bind_address("localhost:4223", env={}) == "0.0.0.0:4223"

    def test_env_beats_config(self):
        cfg = APIConfig(address="localhost:4223", bind_address="0.0.0.0:4223")
        env = {"DEVTEAM_BIND_ADDRESS": "127.0.0.1:9999"}
        assert cfg.resolve_bind_address("localhost:4223", env=env) == "127.0.0.1:9999"

    def test_falls_back_to_advertised_address(self):
        # DEVTEAM_API_ADDRESS moves api_address upstream in daemon.main; with no bind
        # override the bind follows it, preserving pre-bind_address behavior.
        cfg = APIConfig(address="localhost:4223")
        assert cfg.resolve_bind_address("0.0.0.0:4223", env={}) == "0.0.0.0:4223"

    def test_falls_back_to_config_address_when_nothing_else_set(self):
        cfg = APIConfig(address="localhost:4223")
        assert cfg.resolve_bind_address("", env={}) == "localhost:4223"
