import pytest

from retro_e.config import ConfigError, load_config


def test_round1_config_declares_no_roles_and_keeps_its_payload(monkeypatch):
    """Round-1 protocol hashes must not drift because v2 added fields to the config object."""
    config = load_config("config/experiment.toml")
    assert config.roles is None
    assert "roles" not in config.public_dict()
    assert "generator_model_override" not in config.public_dict()
    with pytest.raises(ConfigError):
        config.model_for_role("generator")


def test_v2_roles_bind_distinct_generator_and_judge(monkeypatch):
    monkeypatch.setenv("RETRO_E_V2_GENERATOR_MODEL", "model-g")
    monkeypatch.setenv("RETRO_E_V2_JUDGE_MODEL", "model-j")
    bound = load_config("config/v2_pilot.toml").with_roles(generator="generator", judge="judge")
    assert bound.generator_model == "model-g"
    assert bound.judge_model == "model-j"
    assert bound.public_dict()["generator_model_override"] == "model-g"


def test_unset_role_fails_loudly_instead_of_guessing(monkeypatch):
    monkeypatch.delenv("RETRO_E_V2_MUTATOR_MODEL", raising=False)
    with pytest.raises(ConfigError, match="unset"):
        load_config("config/v2_pilot.toml").model_for_role("mutator")


def test_unknown_role_is_rejected():
    with pytest.raises(ConfigError, match="Unknown model role"):
        load_config("config/v2_pilot.toml").model_for_role("oracle")


def test_cli_binds_roles_so_it_cannot_fall_back_to_round1_env(monkeypatch):
    """Regression: the v2 CLI generated a whole baseline cache with the round-1 model.

    load_config alone leaves generator_model reading RETRO_E_GENERATOR_MODEL, so a config
    declaring [roles] must be bound before any command runs.
    """
    monkeypatch.setenv("RETRO_E_GENERATOR_MODEL", "round1-model")
    monkeypatch.setenv("RETRO_E_JUDGE_MODEL", "round1-model")
    monkeypatch.setenv("RETRO_E_V2_GENERATOR_MODEL", "v2-generator")
    monkeypatch.setenv("RETRO_E_V2_JUDGE_MODEL", "v2-judge")

    unbound = load_config("config/v2_pilot.toml")
    assert unbound.generator_model == "round1-model"

    bound = unbound.with_roles(generator="generator", judge="judge")
    assert bound.generator_model == "v2-generator"
    assert bound.judge_model == "v2-judge"
