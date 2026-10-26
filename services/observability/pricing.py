"""What a model call costs, from its token counts.

Purpose:     USD per million tokens for the models this project uses, matched by
             model-name prefix (longest first).
Why needed:  Providers return tokens, not money. Prices are ESTIMATES that drift:
             edit `PRICES` when a vendor changes them. A model not listed costs 0 and
             is marked unpriced, so a missing entry shows up instead of hiding.
"""

# (input, output) USD per 1M tokens, keyed by the model name without any vendor prefix.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "gpt-oss-120b": (0.15, 0.75),
    "gpt-oss-20b": (0.075, 0.30),
    "llama-3.3-70b": (0.59, 0.79),
    "llama-3.1-8b": (0.05, 0.08),
}


def price_of(model: str) -> tuple[float, float] | None:
    """The (input, output) price for `model`, or None if it is not listed."""
    name = model.lower().rsplit("/", 1)[-1]  # "openai/gpt-oss-120b" -> "gpt-oss-120b"
    for prefix in sorted(PRICES, key=len, reverse=True):
        if name.startswith(prefix):
            return PRICES[prefix]
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated cost of one call; 0.0 when the model has no listed price."""
    price = price_of(model)
    if price is None:
        return 0.0
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
