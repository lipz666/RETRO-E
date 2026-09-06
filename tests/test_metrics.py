from retro_e.metrics import exact_sign_test_p_value, wilson_interval


def test_sign_test_balanced_is_not_significant():
    assert exact_sign_test_p_value(5, 5) == 1.0


def test_sign_test_one_sided_extreme_converted_to_two_sided():
    assert exact_sign_test_p_value(10, 0) == 2 / 1024


def test_wilson_interval_contains_observed_rate():
    lower, upper = wilson_interval(7, 10)
    assert lower < 0.7 < upper
