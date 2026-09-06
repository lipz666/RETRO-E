import json

from retro_e.validation import extract_json, validate_response


def _valid_body():
    return {
        "target_smiles": "CCOC(=O)C",
        "routes": [
            {
                "strategy_plan": "先断开酯键，再由目录级醇和酸汇合。",
                "key_disconnection_class": "Fischer esterification",
                "disconnection_type": "coupling",
                "planned_num_steps": 1,
                "strategy": "酯键断开",
                "route_id": 1,
                "steps": [
                    {
                        "step_id": 1,
                        "product": "CCOC(=O)C",
                        "precursors": ["CCO", "CC(=O)O"],
                        "reaction_class": "Fischer esterification",
                    }
                ],
                "starting_materials": ["CCO", "CC(=O)O"],
            }
        ],
    }


def test_extract_json_from_fence():
    body = _valid_body()
    assert extract_json(f"```json\n{json.dumps(body)}\n```") == body


def test_valid_route_passes():
    result = validate_response(json.dumps(_valid_body()), "CCOC(=O)C")
    assert result.valid, result.errors


def test_wrong_leaves_fail():
    body = _valid_body()
    body["routes"][0]["starting_materials"] = ["CCO"]
    result = validate_response(json.dumps(body), "CCOC(=O)C")
    assert not result.valid
    assert any("graph leaves" in error for error in result.errors)


def test_first_product_must_be_target():
    body = _valid_body()
    body["routes"][0]["steps"][0]["product"] = "CCO"
    body["routes"][0]["starting_materials"] = ["CC(=O)O"]
    result = validate_response(json.dumps(body), "CCOC(=O)C")
    assert not result.valid
    assert any("not the requested target" in error for error in result.errors)
