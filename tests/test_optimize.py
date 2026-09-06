from pathlib import Path

import pytest

from retro_e.optimize import (
    ContextCandidate,
    build_reflection_examples,
    enforce_candidate_budget,
    inspect_candidate_text,
    leak_scan,
    locked_token_count,
    outcomes_from_judgments,
)


def test_locked_token_count_is_deterministic_and_positive():
    text = "SYNTHETIC EXPERIENCE\n1. Prefer convergent routes when fragments are similarly complex."
    assert locked_token_count(text) == locked_token_count(text)
    assert locked_token_count(text) > 0
    assert locked_token_count(text * 3) > locked_token_count(text)


def test_inspect_candidate_text_is_content_addressed():
    a = inspect_candidate_text("SYNTHETIC EXPERIENCE\n1. foo")
    b = inspect_candidate_text("SYNTHETIC EXPERIENCE\n1. foo")
    c = inspect_candidate_text("SYNTHETIC EXPERIENCE\n1. bar")
    assert a.candidate_id == b.candidate_id
    assert a.candidate_id != c.candidate_id


class _FakeContext:
    max_e_star_ratio = 1.10


class _FakeConfig:
    context = _FakeContext()


def test_enforce_candidate_budget_allows_within_ratio():
    baseline = ContextCandidate("base", Path("."), 1000, None)
    candidate = ContextCandidate("cand", Path("."), 1090, "base")
    enforce_candidate_budget(_FakeConfig(), candidate, baseline)  # must not raise


def test_enforce_candidate_budget_rejects_over_ratio():
    baseline = ContextCandidate("base", Path("."), 1000, None)
    candidate = ContextCandidate("cand", Path("."), 1200, "base")
    with pytest.raises(ValueError, match="above the allowed"):
        enforce_candidate_budget(_FakeConfig(), candidate, baseline)


def test_leak_scan_flags_verbatim_target_smiles():
    forbidden = {"CC(C)(C)C(=O)NC1=CC=CN=C1"}
    clean_text = "Prefer late-stage functionalization when protecting groups are avoidable."
    leaking_text = f"This context references {next(iter(forbidden))} directly."
    assert leak_scan(clean_text, forbidden, set()) == []
    assert leak_scan(leaking_text, forbidden, set()) == list(forbidden)


def test_leak_scan_ignores_short_fragments_to_avoid_false_positives():
    # A short SMILES-like fragment (e.g. "CCO") is common English-adjacent text and
    # should not trip the leak guard by accident.
    assert leak_scan("some context mentions CCO in passing", {"CCO"}, set()) == []


def test_outcomes_from_judgments_maps_majority_source_to_reward():
    rows = [
        {
            "pair_id": "p1",
            "target_id": "t1",
            "sample_id": 0,
            "decision": "A",
            "route_A_source": "candidate",
            "route_B_source": "baseline",
        },
        {
            "pair_id": "p1",
            "target_id": "t1",
            "sample_id": 0,
            "decision": "A",
            "route_A_source": "baseline",
            "route_B_source": "candidate",
        },
        {
            "pair_id": "p1",
            "target_id": "t1",
            "sample_id": 0,
            "decision": "Tie",
            "route_A_source": "candidate",
            "route_B_source": "baseline",
        },
    ]
    outcomes = outcomes_from_judgments(rows)
    assert len(outcomes) == 1
    assert outcomes[0]["winner"] == "Tie"
    assert outcomes[0]["reward"] == 0.5


def test_build_reflection_examples_prioritizes_losses_over_ties():
    outcomes = [
        {"pair_id": "p1", "target_id": "t1", "sample_id": 0, "winner": "candidate", "reward": 1.0},
        {"pair_id": "p2", "target_id": "t2", "sample_id": 0, "winner": "Tie", "reward": 0.5},
        {"pair_id": "p3", "target_id": "t3", "sample_id": 0, "winner": "baseline", "reward": 0.0},
    ]
    routes = {
        ("t2", 0): {"valid": False},
        ("t3", 0): {"valid": False},
    }
    examples = build_reflection_examples(outcomes, routes, routes, max_examples=6)
    assert len(examples) == 2
    assert examples[0]["outcome"] == "baseline"
    assert examples[1]["outcome"] == "Tie"


def test_build_reflection_examples_returns_empty_when_candidate_never_loses():
    outcomes = [
        {"pair_id": "p1", "target_id": "t1", "sample_id": 0, "winner": "candidate", "reward": 1.0},
    ]
    examples = build_reflection_examples(outcomes, {}, {}, max_examples=6)
    assert examples == []


def test_incomplete_evaluation_error_exists_for_partial_batches():
    """Regression: a smoke run scored a candidate reward=1.000 from 1 of 20 planned pairs.

    Judge parsing failed silently on 19 of them, and nothing downstream noticed. A reward
    computed on a non-random subset is worse than no reward at all.
    """
    from retro_e.optimize import IncompleteEvaluationError

    assert issubclass(IncompleteEvaluationError, RuntimeError)


def test_optimizer_judging_uses_the_structured_parser():
    """judge_v3 emits JSON, which parse_decision cannot read; the optimizer must not use it."""
    import inspect

    from retro_e import optimize

    source = inspect.getsource(optimize)
    assert "parse_judgment" in source
    assert "parse_decision" not in source
