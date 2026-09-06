from dataclasses import replace
from pathlib import Path

from retro_e.config import load_config
from retro_e.prompts import compose_generation_prompt

ROOT = Path(__file__).resolve().parents[1]


def test_baseline_has_no_experience_block():
    config = load_config(ROOT / "config/experiment.toml")
    prompt = compose_generation_prompt(config, "CCO", "baseline")
    assert "SYNTHETIC EXPERIENCE CONTEXT" not in prompt
    assert "Target SMILES:\nCCO" in prompt


def test_e0_injects_context_at_one_fixed_location(tmp_path):
    config = load_config(ROOT / "config/experiment.toml")
    context = tmp_path / "experience.md"
    context.write_text("1. Prefer robust convergent disconnections.")
    config = replace(config, paths=replace(config.paths, experience_e0=context))
    prompt = compose_generation_prompt(config, "CCO", "E0")
    assert prompt.count("SYNTHETIC EXPERIENCE CONTEXT") == 2
    assert prompt.index("SYNTHETIC EXPERIENCE CONTEXT") < prompt.index("Target SMILES")
