"""Shared Claude client, cost meter and budget stop. Every Claude call in this project is recorded here."""
import json
import os
import time
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
USAGE_LOG = ROOT / "data" / "usage.jsonl"
BUDGET_USD = float(os.environ.get("ANALYST_BUDGET_USD", "25"))

# USD per million tokens: (input, output, cache read). Cache writes are costed at 1.25x input.
# shortcut: Haiku's cache-read price and all cache-write prices are assumed, not published in our notes; the Console bill is the truth.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class BudgetExceeded(RuntimeError):
    pass


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.strip().partition("=")
            if key and value and not key.startswith("#"):
                os.environ.setdefault(key, value)


def client():
    load_env()
    return anthropic.Anthropic(max_retries=5)


def cost(model, usage):
    """Dollar cost of one response's usage object."""
    p_in, p_out, p_read = PRICES[model]
    get = lambda name: getattr(usage, name, 0) or 0
    return (get("input_tokens") * p_in + get("output_tokens") * p_out + get("cache_read_input_tokens") * p_read
            + get("cache_creation_input_tokens") * p_in * 1.25) / 1e6


def record(model, usage, purpose):
    """Append one call to the usage log and return its cost. purpose is a short tag such as 'ingest' or 'ask'."""
    usd = cost(model, usage)
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG.open("a") as f:
        f.write(json.dumps({"t": time.time(), "model": model, "purpose": purpose, "usd": usd,
                            "in": getattr(usage, "input_tokens", 0), "out": getattr(usage, "output_tokens", 0)}) + "\n")
    return usd


def spent(purpose=None):
    if not USAGE_LOG.exists():
        return 0.0
    rows = [json.loads(line) for line in USAGE_LOG.read_text().splitlines() if line.strip()]
    return sum(r["usd"] for r in rows if purpose is None or r["purpose"] == purpose)


def guard():
    """Call before any paid request: stops the run once the project budget is used up."""
    if spent() >= BUDGET_USD:
        raise BudgetExceeded(f"Spent ${spent():.2f} of the ${BUDGET_USD:.2f} budget (ANALYST_BUDGET_USD).")
