from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class APIConfig:
    base_url: str
    chat_completions_path: str
    models_path: str
    api_key_env: str
    generator_model_env: str
    judge_model_env: str
    timeout_seconds: float
    max_retries: int


@dataclass(frozen=True)
class SamplingConfig:
    temperature: float
    top_p: float
    max_tokens: int
    n_routes: int = 1
    samples_per_target: int = 3
    runs_per_pair: int = 3
    seed: int = 0


@dataclass(frozen=True)
class PathsConfig:
    system_prompt: Path
    judge_prompt: Path
    experience_synthesis_prompt: Path
    experience_e0: Path
    experience_e_star: Path
    experience_control: Path
    train_targets: Path
    test_targets: Path
    experience_source_routes: Path
    optimizer_targets: Path
    selection_targets: Path
    pilot_targets: Path
    h1_screen_targets: Path
    results_dir: Path


@dataclass(frozen=True)
class ContextConfig:
    target_tokens: int
    max_e_star_ratio: float
    tokenizer: str


@dataclass(frozen=True)
class ProtocolConfig:
    system_prompt_version: str
    judge_prompt_version: str
    conditions: tuple[str, ...]
    primary_comparisons: tuple[str, ...]
    control_comparison: str


@dataclass(frozen=True)
class ExperimentConfig:
    root: Path
    api: APIConfig
    generation: SamplingConfig
    judge: SamplingConfig
    paths: PathsConfig
    context: ContextConfig
    protocol: ProtocolConfig

    @property
    def api_key(self) -> str:
        value = os.getenv(self.api.api_key_env, "").strip()
        if not value:
            raise ConfigError(f"Missing API key environment variable: {self.api.api_key_env}")
        return value

    @property
    def generator_model(self) -> str:
        value = os.getenv(self.api.generator_model_env, "").strip()
        if not value:
            raise ConfigError(
                f"Missing model environment variable: {self.api.generator_model_env}. "
                "Run `retro-e list-models`, choose one exact model ID, and lock it."
            )
        return value

    @property
    def judge_model(self) -> str:
        value = os.getenv(self.api.judge_model_env, "").strip()
        if not value:
            raise ConfigError(
                f"Missing model environment variable: {self.api.judge_model_env}. "
                "Use a fixed model, preferably from a different family than the generator."
            )
        return value

    def public_dict(self) -> dict[str, Any]:
        """Serializable configuration with environment variable names, never secrets."""
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_config(path: str | Path = "config/experiment.toml") -> ExperimentConfig:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")
    root = config_path.parent.parent
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    api = APIConfig(**raw["api"])
    generation = SamplingConfig(**raw["generation"])
    judge_raw = dict(raw["judge"])
    judge_raw.setdefault("n_routes", 1)
    judge_raw.setdefault("samples_per_target", 1)
    judge = SamplingConfig(**judge_raw)
    path_values = {key: _resolve(root, value) for key, value in raw["paths"].items()}
    paths = PathsConfig(**path_values)
    context = ContextConfig(**raw["context"])
    protocol_raw = raw["protocol"]
    protocol = ProtocolConfig(
        system_prompt_version=protocol_raw["system_prompt_version"],
        judge_prompt_version=protocol_raw["judge_prompt_version"],
        conditions=tuple(protocol_raw["conditions"]),
        primary_comparisons=tuple(protocol_raw["primary_comparisons"]),
        control_comparison=protocol_raw["control_comparison"],
    )
    return ExperimentConfig(root, api, generation, judge, paths, context, protocol)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(
    config: ExperimentConfig,
    files: list[Path],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hashes = {}
    for path in files:
        if path.exists():
            hashes[str(path.relative_to(config.root))] = sha256_file(path)
    payload = {
        "config": config.public_dict(),
        "resolved_models": {
            "generator": config.generator_model,
            "judge": config.judge_model,
        },
        "file_sha256": hashes,
        "metadata": metadata or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["protocol_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return payload
