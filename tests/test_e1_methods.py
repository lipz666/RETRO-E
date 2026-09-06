import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_e1_matrix import (
    ADVANCE_KEEP,
    ADVANCE_TARGETS,
    CANDIDATE_SLOTS,
    SAMPLES,
    SCREEN_TARGETS,
    SELECTION_KEEP,
    VOTES,
    partition_optimizer_targets,
)

from retro_e.optimize import (
    MUTATION_METHODS,
    audit_instruction_candidate,
    propose_candidate,
)


def _targets(count: int) -> list[dict[str, str]]:
    return [{"target_id": f"t-{index:04d}"} for index in range(count)]


def test_schedule_matches_the_predeclared_560_call_budget():
    """Plan v2 section 6.2 fixes 16->4->2; drift here silently changes the experiment."""
    generations = (
        CANDIDATE_SLOTS * SCREEN_TARGETS * SAMPLES
        + ADVANCE_KEEP * ADVANCE_TARGETS * SAMPLES
        + SELECTION_KEEP * 40 * SAMPLES
    )
    assert (CANDIDATE_SLOTS, ADVANCE_KEEP, SELECTION_KEEP) == (16, 4, 2)
    assert generations == 560
    assert generations * VOTES == 560


def test_screen_and_advance_batches_are_disjoint():
    screen, advance = partition_optimizer_targets(_targets(160), 11)
    assert len(screen) == SCREEN_TARGETS
    assert len(advance) == ADVANCE_TARGETS
    assert not {t["target_id"] for t in screen} & {t["target_id"] for t in advance}


def test_target_subsets_depend_on_seed_only_so_methods_share_them():
    """R, S and I must be compared on common targets (plan v2 section 6.2)."""
    first = partition_optimizer_targets(_targets(160), 29)
    second = partition_optimizer_targets(_targets(160), 29)
    other = partition_optimizer_targets(_targets(160), 47)
    assert first == second
    assert first[0] != other[0]


def test_partition_is_order_independent():
    shuffled = _targets(160)
    random.Random(5).shuffle(shuffled)
    assert partition_optimizer_targets(_targets(160), 11) == partition_optimizer_targets(
        shuffled, 11
    )


def test_instruction_audit_flags_chemistry_content():
    clean = audit_instruction_candidate(
        "1. Decompose the problem before committing to a plan.\n2. Verify internal consistency."
    )
    assert clean["boundary_respected"] is True
    assert clean["chemistry_terms_found"] == []

    leaked = audit_instruction_candidate(
        "1. Choose disconnections that simplify the molecule.\n2. Avoid protecting group loops."
    )
    assert leaked["boundary_respected"] is False
    assert "disconnections" in leaked["chemistry_terms_found"]


def test_audit_uses_word_boundaries_not_substrings():
    """A naive substring scan flags the named reaction 'heck' inside the word 'check'."""
    assert audit_instruction_candidate("Check each step, then check again.")[
        "chemistry_terms_found"
    ] == []


def test_mandated_title_line_is_not_counted_as_a_breach():
    """The title is required by the output format, so scanning it would flag every candidate."""
    result = audit_instruction_candidate("SYNTHETIC EXPERIENCE\n1. Decompose the problem.")
    assert result["boundary_respected"] is True
    assert result["chemistry_terms_found"] == []


def test_ambiguous_planning_words_are_reported_but_do_not_fail_the_boundary():
    result = audit_instruction_candidate("1. Prefer convergent plans and reuse intermediates.")
    assert result["boundary_respected"] is True
    assert "convergent" in result["ambiguous_terms_found"]
    assert result["chemistry_terms_found"] == []


def test_audit_does_not_claim_semantic_proof():
    assert "does not prove" in audit_instruction_candidate("nothing here")["method_note"]


def test_unknown_method_is_rejected_before_any_api_call():
    with pytest.raises(ValueError, match="Unknown mutation method"):
        propose_candidate(None, "gradient_descent", "text", [], 800)


def test_three_methods_are_declared():
    assert MUTATION_METHODS == ("reflective", "search_only", "instruction_opt")
