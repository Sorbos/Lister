"""What the Claude calls cost, in dollars.

Prices are Anthropic's list prices per million tokens (input, output) at the time of writing.
They're an estimate: your Anthropic Console (Usage / Cost pages) is the authoritative bill.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import config_dir

PRICES = {  # $ per million tokens: (input, output)
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
CACHE_WRITE, CACHE_READ = 1.25, 0.10  # multipliers on the input price


def cost_of(model: str, usage) -> float | None:
    """Dollar cost of one response, or None if the model's price isn't known."""
    price = PRICES.get(model)
    if price is None or usage is None:
        return None
    get = (lambda k: usage.get(k) or 0) if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0) or 0)
    inp, out = price
    total = (get("input_tokens") * inp
             + get("cache_creation_input_tokens") * inp * CACHE_WRITE
             + get("cache_read_input_tokens") * inp * CACHE_READ
             + get("output_tokens") * out)
    return total / 1_000_000


def money(usd: float | None) -> str:
    if usd is None:
        return "unknown"
    return f"${usd:.2f}" if usd >= 0.995 or usd == 0 else f"{usd * 100:.1f}¢"


def ledger_path() -> Path:
    return config_dir() / "usage.jsonl"


def record(model: str, usd: float | None, folder: str, position: int, when: datetime | None = None) -> None:
    """Append one call to the running log (~/.config/lister/usage.jsonl)."""
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now(timezone.utc)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": when.isoformat(), "model": model, "usd": usd,
                            "folder": folder, "position": position}) + "\n")


def summary(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"all_time": 0.0, "this_month": 0.0, "last_7_days": 0.0, "cards": 0, "by_model": defaultdict(float),
           "by_folder": defaultdict(float), "unpriced": 0}
    p = ledger_path()
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        out["cards"] += 1
        usd = row.get("usd")
        if usd is None:
            out["unpriced"] += 1
            continue
        t = datetime.fromisoformat(row["time"])
        out["all_time"] += usd
        if (t.year, t.month) == (now.year, now.month):
            out["this_month"] += usd
        if now - t <= timedelta(days=7):
            out["last_7_days"] += usd
        out["by_model"][row["model"]] += usd
        out["by_folder"][row.get("folder", "?")] += usd
    return out
