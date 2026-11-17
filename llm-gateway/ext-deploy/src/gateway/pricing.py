"""Model prices and cost in integer micro-units.

PRICES is an example table. Replace the model ids and the prices with the ones
your upstream publishes. The ids are the names callers send in `model` and the
names the gateway passes upstream. The tests use the example ids and prices, so
run them before you edit the table, not after.

Prices are micro-units per million tokens. One unit of the currency is 1,000,000 micro-units. No floats
anywhere. For another currency the arithmetic is the same: only the label changes.
"""

PRICES: dict[str, tuple[int, int]] = {
    # model id: (input, output) in micro-units per million tokens
    "example-large": (100_000, 200_000),
    "example-small": (50_000, 100_000),
    "example-embed": (10_000, 10_000),
}
CHAT_MODELS = ("example-large", "example-small")
EMBEDDING_MODELS = ("example-embed",)
ALL_MODELS = CHAT_MODELS + EMBEDDING_MODELS


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def cost_in(model: str, tokens: int) -> int:
    """Cost of `tokens` input tokens in micro-units, rounded up."""
    return _ceil_div(tokens * PRICES[model][0], 1_000_000)


def cost_out(model: str, tokens: int) -> int:
    """Cost of `tokens` output tokens in micro-units, rounded up."""
    return _ceil_div(tokens * PRICES[model][1], 1_000_000)


def cost(model: str, tokens_in: int, tokens_out: int) -> int:
    return cost_in(model, tokens_in) + cost_out(model, tokens_out)


def micro_to_amount(micro: int) -> str:
    """Format micro-units as an amount with six decimals, for printing only."""
    sign = "-" if micro < 0 else ""
    micro = abs(int(micro))
    return f"{sign}{micro // 1_000_000}.{micro % 1_000_000:06d}"
