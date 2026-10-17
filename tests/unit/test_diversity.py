from career_copilot.domain.diversity import cap_per_company


def test_cap_preserves_order_and_limits_each_company() -> None:
    rows = [(90, "a", 1), (80, "a", 2), (70, "a", 3), (60, "b", 4)]
    assert cap_per_company(rows, 4, 2, company=lambda row: row[1]) == [rows[0], rows[1], rows[3]]


def test_cap_is_stable_under_ties_and_noop_for_distinct_companies() -> None:
    rows = [(50, "a", 2), (50, "b", 1), (50, "c", 3)]
    assert cap_per_company(rows, 3, 1, company=lambda row: row[1]) == rows
