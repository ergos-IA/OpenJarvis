"""AI Fast Track plans and the price table used to meter AI credits.

1 credit = 0.01 € of real AI cost (provider price × tokens).  Plans with
included AI get a monthly allowance; "own" means the customer connected
their own AI account and pays the provider directly (no metering limits).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

PLANS: Dict[str, Dict[str, Any]] = {
    "own": {
        "label": "Conecta tu IA",
        "price": "9,90 €/mes",
        "included_ai": False,
        "credits": 0,
        "models": None,  # any model of the customer's provider
        "on_empty": None,
    },
    "esencial": {
        "label": "Esencial",
        "price": "16,90 €/mes",
        "included_ai": True,
        "credits": 400,
        "models": ["claude-haiku-5-5", "gpt-5-mini"],
        "on_empty": "pause",
    },
    "pro": {
        "label": "Pro",
        "price": "39,90 €/mes",
        "included_ai": True,
        "credits": 1500,
        "models": ["claude-sonnet-5-5", "gpt-5.4", "claude-haiku-5-5", "gpt-5-mini"],
        "on_empty": "downgrade",
        "fallback": "claude-haiku-5-5",
    },
}
TOPUP_CREDITS = 300
TOPUP_PRICE = "4,90 €"

# USD per 1M tokens (input, output).  Anthropic: official list prices.
# Others: the engine's own table (openjarvis.engine.cloud.PRICING) when it
# knows the model, else this conservative default so we never under-charge.
_ANTHROPIC = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}
_DEFAULT_PRICE = (2.00, 10.00)


def price_per_million(model: str) -> Tuple[float, float]:
    model = (model or "").removeprefix("openrouter/")
    if model in _ANTHROPIC:
        return _ANTHROPIC[model]
    try:
        from openjarvis.engine.cloud import PRICING

        if model in PRICING:
            return PRICING[model]
        for key, value in PRICING.items():
            if key.endswith(model) or model.startswith(key):
                return value
    except Exception:
        pass
    return _DEFAULT_PRICE


def usd_to_eur() -> float:
    try:
        return float(os.environ.get("USD_EUR", "0.93"))
    except ValueError:
        return 0.93


def credits_for(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Credits (cents of €) that a call with these tokens costs."""
    pin, pout = price_per_million(model)
    usd = prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout
    return usd * usd_to_eur() * 100


def plan(name: Optional[str] = None) -> Dict[str, Any]:
    key = (name or os.environ.get("JARVIS_PLAN") or "own").strip().lower()
    return {"key": key if key in PLANS else "own", **PLANS.get(key, PLANS["own"])}


def plan_models(name: Optional[str] = None) -> Optional[List[str]]:
    return plan(name)["models"]


__all__ = [
    "PLANS",
    "TOPUP_CREDITS",
    "TOPUP_PRICE",
    "credits_for",
    "plan",
    "plan_models",
    "price_per_million",
]
