from retro_e.judge import parse_decision, source_winner


def test_parse_decision_strict_but_tolerates_whitespace():
    assert parse_decision(" A\n") == "A"
    assert parse_decision("Tie") == "Tie"


def test_votes_are_mapped_to_sources_before_majority():
    rows = [
        {"decision": "A", "route_A_source": "E0", "route_B_source": "baseline"},
        {"decision": "B", "route_A_source": "baseline", "route_B_source": "E0"},
        {"decision": "Tie", "route_A_source": "E0", "route_B_source": "baseline"},
    ]
    assert source_winner(rows) == "E0"
