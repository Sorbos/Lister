"""Build an eBay title (max 80 chars) from card details.

Format: Year [Manufacturer] Set [Insert] #Num Subject [Team] [RC] [Parallel] [/Serial] [AUTO] [Patch]
A card with no number gets "nno" (no number) in the number's place.
When too long, the least important pieces are dropped first.
"""

from __future__ import annotations

from typing import Any

MAX_LEN = 80

# Dropped first → last when the title is too long.
DROP_ORDER = ["manufacturer", "insert", "team", "parallel", "memorabilia", "serial"]


def _get(info: Any, name: str):
    return info.get(name) if isinstance(info, dict) else getattr(info, name, None)


def _pieces(info: Any) -> list[tuple[str | None, str]]:
    """(drop-key or None if never dropped, text) in title order."""
    out: list[tuple[str | None, str]] = []
    add = lambda key, text: text and out.append((key, str(text).strip()))

    set_name = _get(info, "set_name") or ""
    manu = _get(info, "manufacturer") or ""
    add(None, _get(info, "year"))
    # Skip the manufacturer when the set name already contains it ("Upper Deck SP").
    if manu and manu.lower() not in set_name.lower():
        add("manufacturer", manu)
    add(None, set_name)
    add("insert", _get(info, "subset_insert"))
    number = str(_get(info, "card_number") or "").strip().lstrip("#").strip()
    add(None, f"#{number}" if number else "nno")
    add(None, _get(info, "subject"))
    add("team", _get(info, "team"))
    if _get(info, "rookie"):
        add(None, "RC")
    add("parallel", _get(info, "parallel"))
    serial = _get(info, "serial_number")
    if serial:
        add("serial", serial if serial.startswith("/") or "/" in serial else f"/{serial}")
    if _get(info, "autograph"):
        add(None, "AUTO")
    if _get(info, "memorabilia"):
        add("memorabilia", "Patch")
    if _get(info, "graded") and _get(info, "grader") and _get(info, "grade"):
        add(None, f"{_get(info, 'grader')} {_get(info, 'grade')}")
    return out


def build_title(info: Any, max_len: int = MAX_LEN) -> str:
    pieces = _pieces(info)
    join = lambda ps: " ".join(t for _, t in ps)
    for key in DROP_ORDER:
        if len(join(pieces)) <= max_len:
            break
        pieces = [p for p in pieces if p[0] != key]
    title = join(pieces)
    if len(title) > max_len:  # still too long: cut at a word boundary
        title = title[: max_len + 1].rsplit(" ", 1)[0]
    return title
