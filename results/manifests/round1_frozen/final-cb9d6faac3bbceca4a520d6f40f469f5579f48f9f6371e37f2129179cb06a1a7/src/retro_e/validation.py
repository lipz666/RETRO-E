from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .chemistry import canonicalize, heavy_atoms


class ResponseParseError(ValueError):
    pass


@dataclass(frozen=True)
class ValidationResult:
    parsed: dict[str, Any] | None
    normalized: dict[str, Any] | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return self.normalized is not None and not self.errors


def extract_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ResponseParseError("No JSON object found")
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"Invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ResponseParseError("Top-level JSON must be an object")
    return value


def validate_response(
    text: str, expected_target: str, expected_routes: int = 1
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        parsed = extract_json(text)
    except ResponseParseError as exc:
        return ValidationResult(None, None, (str(exc),), ())

    if set(parsed) != {"target_smiles", "routes"}:
        errors.append("Top-level keys must be exactly target_smiles and routes")
    target = canonicalize(str(parsed.get("target_smiles") or ""))
    expected = canonicalize(expected_target)
    if target is None:
        errors.append("target_smiles is not valid SMILES")
    elif expected is None or target != expected:
        errors.append("target_smiles does not match requested target")

    raw_routes = parsed.get("routes")
    if not isinstance(raw_routes, list):
        errors.append("routes must be an array")
        return ValidationResult(parsed, None, tuple(errors), tuple(warnings))
    if len(raw_routes) != expected_routes:
        errors.append(f"Expected {expected_routes} routes, received {len(raw_routes)}")

    normalized_routes: list[dict[str, Any]] = []
    for route_index, route in enumerate(raw_routes, 1):
        normalized = _validate_route(route, route_index, expected, errors, warnings)
        if normalized is not None:
            normalized_routes.append(normalized)

    normalized_body = None
    if target is not None and len(normalized_routes) == len(raw_routes):
        normalized_body = {"target_smiles": target, "routes": normalized_routes}
    return ValidationResult(parsed, normalized_body, tuple(errors), tuple(warnings))


def _validate_route(
    route: Any,
    route_index: int,
    expected_target: str | None,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any] | None:
    prefix = f"route {route_index}"
    if not isinstance(route, dict):
        errors.append(f"{prefix} must be an object")
        return None
    required = {
        "strategy_plan",
        "key_disconnection_class",
        "disconnection_type",
        "planned_num_steps",
        "strategy",
        "route_id",
        "steps",
        "starting_materials",
    }
    if set(route) != required:
        errors.append(f"{prefix} keys do not exactly match the schema")

    raw_steps = route.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        errors.append(f"{prefix} has no steps")
        return None
    if route.get("planned_num_steps") != len(raw_steps):
        errors.append(f"{prefix} planned_num_steps does not equal steps length")

    steps: list[dict[str, Any]] = []
    seen_products: set[str] = set()
    referenced_precursors: list[str] = []
    for position, step in enumerate(raw_steps, 1):
        step_prefix = f"{prefix} step {position}"
        if not isinstance(step, dict):
            errors.append(f"{step_prefix} must be an object")
            continue
        if set(step) != {"step_id", "product", "precursors", "reaction_class"}:
            errors.append(f"{step_prefix} keys do not exactly match the schema")
        if step.get("step_id") != position:
            warnings.append(f"{step_prefix} step_id is not its topological position")
        product = canonicalize(str(step.get("product") or ""))
        if product is None:
            errors.append(f"{step_prefix} product is invalid SMILES")
            continue
        raw_precursors = step.get("precursors")
        if not isinstance(raw_precursors, list) or not raw_precursors:
            errors.append(f"{step_prefix} has no precursors")
            continue
        precursors: list[str] = []
        for precursor_index, raw in enumerate(raw_precursors, 1):
            precursor = canonicalize(raw) if isinstance(raw, str) else None
            if precursor is None:
                errors.append(f"{step_prefix} precursor {precursor_index} is invalid SMILES")
            else:
                precursors.append(precursor)
                referenced_precursors.append(precursor)
        if product in seen_products:
            errors.append(f"{step_prefix} repeats a product")
        seen_products.add(product)
        steps.append(
            {
                "step_id": position,
                "product": product,
                "precursors": precursors,
                "reaction_class": str(step.get("reaction_class") or "").strip(),
            }
        )

    for step_index, step in enumerate(steps):
        earlier_products = {item["product"] for item in steps[:step_index]}
        if step_index > 0 and step["product"] not in {
            precursor for item in steps[:step_index] for precursor in item["precursors"]
        }:
            errors.append(f"{prefix} step {step_index + 1} product was not introduced earlier")
        if step["product"] in earlier_products:
            errors.append(f"{prefix} contains a duplicate product")

    products = {step["product"] for step in steps}
    leaves = sorted(set(referenced_precursors) - products)
    raw_starting = route.get("starting_materials")
    if not isinstance(raw_starting, list):
        errors.append(f"{prefix} starting_materials must be an array")
        declared: list[str] = []
    else:
        declared = []
        for index, raw in enumerate(raw_starting, 1):
            value = canonicalize(raw) if isinstance(raw, str) else None
            if value is None:
                errors.append(f"{prefix} starting material {index} is invalid SMILES")
            else:
                declared.append(value)
    if set(declared) != set(leaves):
        errors.append(f"{prefix} starting_materials do not equal graph leaves")
    large_leaves = [leaf for leaf in leaves if heavy_atoms(leaf) > 12]
    if large_leaves:
        warnings.append(
            f"{prefix} has {len(large_leaves)} leaves above the 12-heavy-atom heuristic; "
            "catalog/chiral-pool status needs review"
        )
    if steps:
        if expected_target is not None and steps[0]["product"] != expected_target:
            errors.append(f"{prefix} step 1 product is not the requested target")
        first_class = steps[0]["reaction_class"]
        if str(route.get("key_disconnection_class") or "").strip() != first_class:
            errors.append(f"{prefix} key_disconnection_class differs from step 1")

    raw_route_id = route.get("route_id")
    try:
        route_id = int(raw_route_id)
    except (TypeError, ValueError):
        errors.append(f"{prefix} route_id must be an integer")
        route_id = route_index

    return {
        "strategy_plan": str(route.get("strategy_plan") or "").strip(),
        "key_disconnection_class": str(route.get("key_disconnection_class") or "").strip(),
        "disconnection_type": str(route.get("disconnection_type") or "").strip(),
        "planned_num_steps": len(steps),
        "strategy": str(route.get("strategy") or "").strip(),
        "route_id": route_id,
        "steps": steps,
        "starting_materials": leaves,
    }
