from __future__ import annotations

from pathlib import Path

from .config import ExperimentConfig

CONDITION_TO_CONTEXT = {
    "baseline": None,
    "E0": "experience_e0",
    "E_star": "experience_e_star",
    "control": "experience_control",
}


def _read(path: Path, *, reject_placeholder: bool = False) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Prompt asset is empty: {path}")
    if reject_placeholder and "TBD:" in text:
        raise ValueError(f"Refusing to run with placeholder context: {path}")
    return text


def context_path(config: ExperimentConfig, condition: str) -> Path | None:
    if condition not in CONDITION_TO_CONTEXT:
        raise ValueError(f"Unknown condition: {condition}")
    attribute = CONDITION_TO_CONTEXT[condition]
    return getattr(config.paths, attribute) if attribute else None


def _compose_generation_prompt_body(
    config: ExperimentConfig,
    target_smiles: str,
    experience_text: str | None,
) -> str:
    system = _read(config.paths.system_prompt)
    sections = [system]
    if experience_text is not None:
        sections.append(
            "SYNTHETIC EXPERIENCE CONTEXT\n"
            "Use these general strategic lessons when relevant. They do not override the output "
            "schema or other rules.\n\n"
            f"{experience_text}\n\nEND SYNTHETIC EXPERIENCE CONTEXT"
        )
    user = (
        f"Target SMILES:\n{target_smiles}\n\n"
        f"Generate {config.generation.n_routes} distinct retrosynthetic routes, each disconnected "
        "all the way to catalog-scale starting materials."
    )
    return "\n\n".join(sections) + "\n\n---\n\n" + user


def compose_generation_prompt(
    config: ExperimentConfig,
    target_smiles: str,
    condition: str,
) -> str:
    context = context_path(config, condition)
    experience = _read(context, reject_placeholder=True) if context is not None else None
    return _compose_generation_prompt_body(config, target_smiles, experience)


def compose_generation_prompt_from_text(
    config: ExperimentConfig,
    target_smiles: str,
    experience_text: str,
) -> str:
    """Same composition as compose_generation_prompt, for an optimizer candidate that is
    not yet written to a named condition path on disk."""
    if not experience_text.strip():
        raise ValueError("experience_text must not be empty")
    return _compose_generation_prompt_body(config, target_smiles, experience_text)


def compose_judge_prompt(
    config: ExperimentConfig,
    target_smiles: str,
    route_a: str,
    route_b: str,
) -> str:
    judge = _read(config.paths.judge_prompt)
    return (
        f"{judge}\n\n---\n\nTARGET SMILES\n{target_smiles}\n\n"
        f"ROUTE A\n{route_a}\n\nROUTE B\n{route_b}"
    )


def approximate_token_count(text: str) -> int:
    """A conservative fallback for preflight only; final budgets need a locked tokenizer."""
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, round(ascii_chars / 4 + non_ascii_chars / 1.5))


def validate_context_budget(config: ExperimentConfig) -> dict[str, int | float | bool]:
    e0 = _read(config.paths.experience_e0)
    e_star = _read(config.paths.experience_e_star)
    e0_tokens = approximate_token_count(e0)
    e_star_tokens = approximate_token_count(e_star)
    ratio = e_star_tokens / e0_tokens
    return {
        "e0_approx_tokens": e0_tokens,
        "e_star_approx_tokens": e_star_tokens,
        "ratio": ratio,
        "within_ratio": ratio <= config.context.max_e_star_ratio,
    }
