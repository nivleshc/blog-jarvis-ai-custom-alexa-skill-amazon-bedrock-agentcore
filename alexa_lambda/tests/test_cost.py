"""Tests for cost.py -- actual-token-based cost calculation, solution-
design.md Section 7.0.2/7.3."""


def test_cost_calculation_matches_worked_example_from_solution_design(mocked_aws):
    """Reproduces solution-design.md Section 7.3's worked example exactly:
    200 input tokens, 512 output tokens, Nova Lite pricing -> $0.0001349."""
    from cost import estimate_cost_usd

    # solution-design.md Section 7.3 displays this rounded to 4 sig figs
    # ($0.0001349); the exact computed value is 0.00013488, so the
    # tolerance here reflects that rounding, not a loose assertion.
    cost = estimate_cost_usd(input_tokens=200, output_tokens=512)
    assert abs(cost - 0.00013488) < 1e-9, cost


def test_cost_is_zero_for_zero_tokens(mocked_aws):
    from cost import estimate_cost_usd

    assert estimate_cost_usd(0, 0) == 0.0


def test_cost_scales_linearly_with_tokens(mocked_aws):
    from cost import estimate_cost_usd

    single = estimate_cost_usd(100, 100)
    double = estimate_cost_usd(200, 200)
    assert abs(double - (single * 2)) < 1e-12


def test_cost_respects_price_env_var_overrides(mocked_aws, monkeypatch):
    monkeypatch.setenv("INPUT_PRICE_PER_MILLION_TOKENS", "1.0")
    monkeypatch.setenv("OUTPUT_PRICE_PER_MILLION_TOKENS", "2.0")
    import cost
    import importlib
    importlib.reload(cost)

    result = cost.estimate_cost_usd(input_tokens=1_000_000, output_tokens=1_000_000)
    assert abs(result - 3.0) < 1e-9
