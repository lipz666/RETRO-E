from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass, replace
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
class RolesConfig:
    """Environment-variable names for each v2 model role (plan section 3.1).

    Round 1 had exactly two roles wired to two env vars, which cannot express G != JG or a
    separate mutator/external judge. Roles are declared here and resolved to concrete model
    IDs at run time, so every manifest records which model actually played which part.
    """

    generator: str
    mutator: str
    judge: str
    external_generator: str | None = None
    external_judge: str | None = None

    def env_for(self, role: str) -> str:
        try:
            value = getattr(self, role)
        except AttributeError:
            raise ConfigError(
                f"Unknown model role {role!r}; known roles: {sorted(self.__dataclass_fields__)}"
            ) from None
        if not value:
            raise ConfigError(f"Role {role!r} is not configured in [roles]")
        return str(value)


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
    roles: RolesConfig | None = None
    generator_model_override: str | None = None
    judge_model_override: str | None = None

    @property
    def api_key(self) -> str:
        value = os.getenv(self.api.api_key_env, "").strip()
        if not value:
            raise ConfigError(f"Missing API key environment variable: {self.api.api_key_env}")
        return value

    @property
    def generator_model(self) -> str:
        if self.generator_model_override:
            return self.generator_model_override
        value = os.getenv(self.api.generator_model_env, "").strip()
        if not value:
            raise ConfigError(
                f"Missing model environment variable: {self.api.generator_model_env}. "
                "Run `retro-e list-models`, choose one exact model ID, and lock it."
            )
        return value

    @property
    def judge_model(self) -> str:
        if self.judge_model_override:
            return self.judge_model_override
        value = os.getenv(self.api.judge_model_env, "").strip()
        if not value:
            raise ConfigError(
                f"Missing model environment variable: {self.api.judge_model_env}. "
                "Use a fixed model, preferably from a different family than the generator."
            )
        return value

    def model_for_role(self, role: str) -> str:
        """Resolve a declared role (generator/mutator/judge/external_*) to one exact model ID."""
        if self.roles is None:
            raise ConfigError(
                "This config declares no [roles] section; use config/v2_pilot.toml for "
                "multi-role runs, or generator_model/judge_model for round-1 compatibility."
            )
        env_name = self.roles.env_for(role)
        value = os.getenv(env_name, "").strip()
        if not value:
            raise ConfigError(
                f"Role {role!r} maps to environment variable {env_name}, which is unset. "
                "Lock an exact model ID from `retro-e list-models` before any run."
            )
        return value

    def with_roles(self, *, generator: str, judge: str) -> ExperimentConfig:
        """Bind two roles to concrete model IDs for one run.

        The bound IDs land in public_dict and therefore in the protocol hash, so a run with
        G=gemini-3.8 / JG=gemini-3.7 can never be confused with a self-judging run.
        """
        return replace(
            self,
            generator_model_override=self.model_for_role(generator),
            judge_model_override=self.model_for_role(judge),
        )

    def public_dict(self) -> dict[str, Any]:
        """Serializable configuration with environment variable names, never secrets.

        Keys added after round 1 are omitted while unset, so a round-1 config still hashes to
        its recorded protocol_sha256 instead of silently drifting.
        """
        payload = _jsonable(asdict(self))
        for key in ("roles", "generator_model_override", "judge_model_override"):
            if payload.get(key) is None:
                payload.pop(key, None)
        return payload


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
    roles = RolesConfig(**raw["roles"]) if "roles" in raw else None
    return ExperimentConfig(root, api, generation, judge, paths, context, protocol, roles)


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
    resolved_models = {
        "generator": config.generator_model,
        "judge": config.judge_model,
    }
    if config.roles is not None:
        for role in sorted(config.roles.__dataclass_fields__):
            if getattr(config.roles, role):
                try:
                    resolved_models[f"role:{role}"] = config.model_for_role(role)
                except ConfigError:
                    # An unset optional role must be visible in the manifest, not silently absent.
                    resolved_models[f"role:{role}"] = "UNSET"
    payload = {
        "config": config.public_dict(),
        "resolved_models": resolved_models,
        "file_sha256": hashes,
        "metadata": metadata or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["protocol_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return payload
