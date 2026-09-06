from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .api import OpenAICompatibleClient
from .chemistry import ChemistryDependencyError, canonicalize
from .config import ConfigError, build_manifest, load_config
from .dataset import audit_target_files
from .generate import run_generation
from .io import write_json
from .judge import run_judging
from .metrics import add_holm_adjustment, summarize_judgments, write_metric_outputs
from .optimize import optimization_contract
from .prompts import compose_generation_prompt, validate_context_budget


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retro-e")
    parser.add_argument("--config", default="config/experiment.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor", help="Check local assets and environment without calling the API"
    )
    doctor.add_argument(
        "--stage",
        choices=["pilot", "h1_screen", "optimization", "final"],
        default="pilot",
    )
    sub.add_parser("list-models", help="List model IDs exposed by the configured API")
    manifest = sub.add_parser("manifest", help="Freeze hashes of experiment-defining files")
    manifest.add_argument(
        "--stage", required=True, choices=["pilot", "h1_screen", "optimization", "final"]
    )
    sub.add_parser("context-budget", help="Check approximate E0/E* length ratio")
    sub.add_parser("optimization-contract", help="Print the frozen/mutable optimizer boundary")

    audit = sub.add_parser("audit-targets")
    audit.add_argument("paths", nargs="+", type=Path)

    render = sub.add_parser("render-prompt")
    render.add_argument("--target", required=True)
    render.add_argument(
        "--condition", required=True, choices=["baseline", "E0", "E_star", "control"]
    )

    generate = sub.add_parser("generate")
    generate.add_argument("--targets", type=Path, required=True)
    generate.add_argument(
        "--condition", required=True, choices=["baseline", "E0", "E_star", "control"]
    )
    generate.add_argument("--output", type=Path)
    generate.add_argument("--protocol-hash", required=True)
    generate.add_argument("--limit", type=int)
    generate.add_argument("--workers", type=int, default=1)

    judge = sub.add_parser("judge")
    judge.add_argument("--left", type=Path, required=True)
    judge.add_argument("--right", type=Path, required=True)
    judge.add_argument("--left-source", required=True)
    judge.add_argument("--right-source", required=True)
    judge.add_argument("--comparison", required=True)
    judge.add_argument("--output", type=Path, required=True)
    judge.add_argument("--protocol-hash", required=True)
    judge.add_argument("--limit", type=int)
    judge.add_argument("--workers", type=int, default=1)

    metrics = sub.add_parser("metrics")
    metrics.add_argument(
        "--comparison",
        action="append",
        required=True,
        metavar="FAVORED_SOURCE=JUDGMENTS_JSONL",
        help="Repeat for every comparison in the confirmatory multiplicity family",
    )
    metrics.add_argument("--output-json", type=Path, default=Path("results/metrics.json"))
    metrics.add_argument("--output-csv", type=Path, default=Path("results/results.csv"))
    return parser


def _manifest_files(config, stage: str) -> list[Path]:
    protocol_files = [
        config.root / "config/experiment.toml",
        config.paths.system_prompt,
        config.paths.judge_prompt,
        config.root / "schemas/target.schema.json",
        config.root / "schemas/experience_route.schema.json",
    ]
    if stage == "pilot":
        protocol_files += [config.paths.experience_e0, config.paths.pilot_targets]
    elif stage == "h1_screen":
        protocol_files += [config.paths.experience_e0, config.paths.h1_screen_targets]
    elif stage == "optimization":
        protocol_files += [
            config.paths.experience_synthesis_prompt,
            config.paths.experience_e0,
            config.paths.experience_source_routes,
            config.paths.optimizer_targets,
            config.paths.selection_targets,
        ]
    elif stage == "final":
        protocol_files += [
            config.paths.experience_e0,
            config.paths.experience_e_star,
            config.paths.experience_control,
            config.paths.test_targets,
        ]
    else:
        raise ValueError(f"Unknown manifest stage: {stage}")
    return protocol_files + sorted((config.root / "src/retro_e").glob("*.py"))


def _doctor(config, stage: str) -> dict[str, object]:
    assets = _manifest_files(config, stage)
    placeholder_assets = {}
    for path in assets:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        placeholder_assets[str(path)] = path.parent == config.root / "contexts" and "TBD:" in text
    checks: dict[str, object] = {
        "python": sys.version.split()[0],
        "stage": stage,
        "supported_python": (3, 11) <= sys.version_info[:2] < (3, 14),
        "api_key_env": config.api.api_key_env,
        "api_key_set": bool(__import__("os").getenv(config.api.api_key_env, "").strip()),
        "generator_model_env": config.api.generator_model_env,
        "generator_model_set": bool(
            __import__("os").getenv(config.api.generator_model_env, "").strip()
        ),
        "judge_model_env": config.api.judge_model_env,
        "judge_model_set": bool(__import__("os").getenv(config.api.judge_model_env, "").strip()),
        "assets": {str(path): path.exists() for path in assets},
        "placeholder_assets": placeholder_assets,
    }
    try:
        checks["rdkit_available"] = canonicalize("CCO") == "CCO"
    except ChemistryDependencyError:
        checks["rdkit_available"] = False
    checks["ready_for_api_run"] = all(
        [
            checks["supported_python"],
            checks["api_key_set"],
            checks["generator_model_set"],
            checks["judge_model_set"],
            checks["rdkit_available"],
            all(checks["assets"].values()),
            not any(placeholder_assets.values()),
        ]
    )
    return checks


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            result = _doctor(config, args.stage)
        elif args.command == "list-models":
            with OpenAICompatibleClient(config) as client:
                result = {"models": client.list_models()}
        elif args.command == "manifest":
            manifest_files = _manifest_files(config, args.stage)
            missing = [str(path) for path in manifest_files if not path.exists()]
            placeholders = [
                str(path)
                for path in manifest_files
                if path.exists()
                and path.parent == config.root / "contexts"
                and "TBD:" in path.read_text(encoding="utf-8")
            ]
            if missing or placeholders:
                raise ConfigError(
                    f"Manifest preflight failed; missing={missing}, placeholders={placeholders}"
                )
            result = build_manifest(config, manifest_files, {"stage": args.stage})
            result["created_at"] = datetime.now(UTC).isoformat()
            output = (
                config.paths.results_dir
                / "manifests"
                / f"{args.stage}-{result['protocol_sha256']}.json"
            )
            write_json(output, result)
            result = {"manifest": str(output), "protocol_sha256": result["protocol_sha256"]}
        elif args.command == "context-budget":
            result = validate_context_budget(config)
        elif args.command == "optimization-contract":
            result = optimization_contract()
        elif args.command == "audit-targets":
            result = audit_target_files([path.resolve() for path in args.paths])
        elif args.command == "render-prompt":
            result = compose_generation_prompt(config, args.target, args.condition)
            print(result)
            return 0
        elif args.command == "generate":
            output = args.output or (
                config.paths.results_dir / "generations" / f"{args.condition}.jsonl"
            )
            result = run_generation(
                config,
                args.targets.resolve(),
                args.condition,
                output.resolve(),
                args.protocol_hash,
                limit=args.limit,
                workers=args.workers,
            )
        elif args.command == "judge":
            result = run_judging(
                config,
                args.left.resolve(),
                args.right.resolve(),
                args.left_source,
                args.right_source,
                args.comparison,
                args.output.resolve(),
                args.protocol_hash,
                limit=args.limit,
                workers=args.workers,
            )
        elif args.command == "metrics":
            metrics = []
            for spec in args.comparison:
                if "=" not in spec:
                    raise ValueError(
                        "--comparison must have the form FAVORED_SOURCE=JUDGMENTS_JSONL"
                    )
                favored_source, raw_path = spec.split("=", 1)
                metrics.append(
                    summarize_judgments(Path(raw_path).resolve(), favored_source.strip())
                )
            add_holm_adjustment(metrics)
            write_metric_outputs(args.output_json.resolve(), args.output_csv.resolve(), metrics)
            result = {"comparisons": metrics}
        else:
            raise AssertionError(args.command)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ConfigError, ChemistryDependencyError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
