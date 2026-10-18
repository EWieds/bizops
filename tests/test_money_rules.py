from bizops.policy import check_cap_change, max_allowed_cap

M = 1_000_000


def test_no_raise_without_two_profitable_months():
    assert max_allowed_cap(100 * M, [], 1000 * M) == 100 * M
    assert max_allowed_cap(100 * M, [5 * M], 1000 * M) == 100 * M
    assert max_allowed_cap(100 * M, [5 * M, -1], 1000 * M) == 100 * M
    assert max_allowed_cap(100 * M, [5 * M, 0], 1000 * M) == 100 * M


def test_raise_at_most_25_pct():
    assert max_allowed_cap(100 * M, [-3 * M, 5 * M, 7 * M], 1000 * M) == 125 * M


def test_never_above_ceiling():
    assert max_allowed_cap(900 * M, [1, 1], 1000 * M) == 1000 * M
    assert max_allowed_cap(2000 * M, [1, 1], 1000 * M) == 1000 * M


def test_check_cap_change():
    ok, _ = check_cap_change(100 * M, 80 * M, [], 1000 * M)
    assert ok  # decreases always allowed
    ok, why = check_cap_change(100 * M, 126 * M, [1, 1], 1000 * M)
    assert not ok and "25" in why
    ok, _ = check_cap_change(100 * M, 125 * M, [1, 1], 1000 * M)
    assert ok
    ok, why = check_cap_change(100 * M, 2000 * M, [1, 1], 1000 * M)
    assert not ok and "ceiling" in why
