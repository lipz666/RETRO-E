from retro_e.metrics import add_holm_adjustment


def test_holm_adjustment_is_monotone_in_sorted_p_values():
    metrics = [
        {"two_sided_exact_sign_test_p": 0.03},
        {"two_sided_exact_sign_test_p": 0.01},
        {"two_sided_exact_sign_test_p": 0.20},
    ]
    add_holm_adjustment(metrics)
    assert metrics[1]["holm_adjusted_p"] == 0.03
    assert metrics[0]["holm_adjusted_p"] == 0.06
    assert metrics[2]["holm_adjusted_p"] == 0.20
