"""Цены Claude API за 1M токенов (USD). Проверяйте актуальность на странице цен Anthropic."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write_mult: float = 1.25  # запись в кэш на 5 минут


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01),
    # возможные цели серверного fallback
    "claude-opus-5": Price(input=5.00, output=25.00, cache_read=0.50),
    "claude-opus-4-8": Price(input=5.00, output=25.00, cache_read=0.50),
    "claude-sonnet-5": Price(input=2.00, output=10.00, cache_read=0.20),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> float:
    price = PRICES.get(model) or PRICES["claude-opus-5-5"]
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_write * price.input * price.cache_write_mult
        + cache_read * price.cache_read
    )
    return total / 1_000_000
