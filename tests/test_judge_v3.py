import pytest

from retro_e.judge import JudgmentError, parse_judgment

V3 = (
    '{"decision":"B","insufficient_information":false,'
    '"main_reason":"Route A step 3 is an SNAr on an unactivated arene",'
    '"fatal_A":true,"fatal_B":false,'
    '"error_tags":["infeasible_key_transformation"],"decisive_step":"A:3"}'
)


def test_structured_verdict_keeps_behavioural_fields():
    verdict = parse_judgment(V3)
    assert verdict["decision"] == "B"
    assert verdict["structured"] is True
    assert verdict["fatal_A"] is True
    assert verdict["error_tags"] == ["infeasible_key_transformation"]
    assert verdict["decisive_step"] == "A:3"
    assert verdict["parse_error"] is None


def test_json_survives_a_fenced_and_prefixed_reply():
    verdict = parse_judgment(f"Here is my verdict:\n```json\n{V3}\n```")
    assert verdict["decision"] == "B"
    assert verdict["structured"] is True


def test_unknown_error_tags_are_quarantined_not_counted():
    verdict = parse_judgment(V3.replace('"infeasible_key_transformation"', '"made_up_tag"'))
    assert verdict["error_tags"] == []
    assert verdict["unknown_error_tags"] == ["made_up_tag"]


def test_decision_only_reply_reports_absent_behaviour_rather_than_false():
    """A judge_v2 reply must never look like 'the judge found no fatal problem'."""
    verdict = parse_judgment("DECISION: A")
    assert verdict["decision"] == "A"
    assert verdict["structured"] is False
    assert verdict["fatal_A"] is None
    assert verdict["fatal_B"] is None
    assert verdict["parse_error"] == "no_json_object_found"


def test_malformed_json_is_recorded_as_parse_failure_not_chemistry():
    verdict = parse_judgment('{"decision": "B", oops\nDECISION: B')
    assert verdict["decision"] == "B"
    assert verdict["structured"] is False
    assert verdict["parse_error"] is not None


def test_unreadable_reply_still_raises():
    with pytest.raises(JudgmentError):
        parse_judgment("I would rather not say.")


def test_truncated_json_decision_is_salvaged_not_discarded():
    """Observed live: reasoning tokens ate the output budget mid-object at ~1% of calls."""
    verdict = parse_judgment(
        '```json\n{"decision":"A","insufficient_information":false,'
        '"main_reason":"Route A constructs the 1,2,4'
    )
    assert verdict["decision"] == "A"
    assert verdict["parse_error"] == "truncated_json_decision_salvaged"


def test_salvaged_truncation_reports_absent_behaviour_not_false():
    """Fields after the cut are missing; a missing fatal flag must not read as 'none found'."""
    verdict = parse_judgment('{"decision":"B","insufficient_information":false,"main_re')
    assert verdict["structured"] is False
    assert verdict["fatal_A"] is None
    assert verdict["fatal_B"] is None
    assert verdict["error_tags"] == []
