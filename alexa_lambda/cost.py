"""
Actual (not estimated) per-call cost calculation, using the real token
counts Bedrock reports back. See solution-design.md Section 7.0.2 for the
pricing table this is based on, and Section 7.1's distinction between the
pre-call character-based cap (input_length check, a rejection threshold)
and this post-call calculation (a cost figure, using real tokens).

Pricing is Nova Lite's on-demand rate as of solution-design.md's writing
(July 2026) -- $0.06 per 1M input tokens, $0.24 per 1M output tokens.
Bedrock pricing changes periodically; re-verify against
aws.amazon.com/bedrock/pricing/ before relying on this for real budget
decisions, per solution-design.md Section 7.0.2's own caveat.
"""

import os

_DEFAULT_INPUT_PRICE_PER_MILLION = 0.06
_DEFAULT_OUTPUT_PRICE_PER_MILLION = 0.24


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """
    Despite the module-level docstring calling this "actual," the
    function name keeps `estimate_` because the *tokens* are exact
    (from Bedrock's own response) but the *price table* is a value we
    maintain ourselves and could go stale if Bedrock's pricing changes
    without this file being updated -- see the caveat above. "Actual
    tokens, estimated price" is the precise description.

    Prices are overridable via environment variables so a pricing update
    doesn't require a code change, only a Terraform variable change.
    """
    input_price = float(os.environ.get("INPUT_PRICE_PER_MILLION_TOKENS", _DEFAULT_INPUT_PRICE_PER_MILLION))
    output_price = float(os.environ.get("OUTPUT_PRICE_PER_MILLION_TOKENS", _DEFAULT_OUTPUT_PRICE_PER_MILLION))

    return (input_tokens / 1_000_000 * input_price) + (output_tokens / 1_000_000 * output_price)
