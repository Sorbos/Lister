"""Inventory: approved cards waiting to be listed, and what happens to them after.

ROOT/inventory/<card id>/   ready to price, schedule and list
ROOT/listed/<card id>/      live on eBay (or scheduled)
ROOT/sold/<card id>/        sold
ROOT/deleted/<card id>/     deleted from the inventory (kept on disk in case of a mistake)

Each card folder holds front.jpg, back.jpg (if scanned) and card.json:
  id, title, title_custom, info (card details), listing (format, prices, length, start time),
  source (batch + position), added_at, ebay (the current listing attempt), listings (past attempts).

Cards arrive here when every card in a batch has been approved or skipped. A listed card that
doesn't sell can be moved back to the inventory to be re-priced and listed again.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .review import DEFAULT_LISTING, _num, iso, parse_time, validate_card
from .title import build_title

STAGES = ("inventory", "listed", "sold")


class InventoryError(RuntimeError):
    pass


def stage_dir(root: Path, stage: str) -> Path:
    if stage not in STAGES:
        raise InventoryError(f"unknown stage {stage}")
    return Path(root) / stage


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "card"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(folder: Path, card: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in card.items() if not k.startswith("_") and k != "errors"}
    tmp = folder / "card.tmp"
    tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    tmp.replace(folder / "card.json")


# ---------------------------------------------------------------- arriving from a batch
def send_batch(batch_folder: Path, cards: list[dict], root: Path, listing_defaults: dict | None = None) -> list[str]:
    """Copy each approved (not skipped) card of a finished batch into ROOT/inventory. Returns the new ids."""
    batch_folder = Path(batch_folder)
    # Shipping isn't copied in: an empty card value follows the stock shipping cost, even when that changes.
    defaults = {k: v for k, v in (listing_defaults or {}).items() if k in DEFAULT_LISTING and k != "shipping_cost"}
    added = []
    for c in cards:
        if c.get("skip") or not c.get("approved"):
            continue
        card_id = _safe(f"{batch_folder.name}-{c['position']:02d}")
        dest = stage_dir(root, "inventory") / card_id
        if any((stage_dir(root, s) / card_id).exists() for s in STAGES):
            continue  # already sent (e.g. a retry)
        dest.mkdir(parents=True)
        files = {}
        for side in ("front", "back"):
            if c.get(side):
                shutil.copy2(batch_folder / c[side], dest / f"{side}.jpg")
                files[side] = f"{side}.jpg"
        _write(dest, {
            "id": card_id,
            "title": c["title"],
            "title_custom": bool(c.get("title_custom")),
            "info": c["info"],
            "front": files.get("front"),
            "back": files.get("back"),
            "listing": _clean_listing({**DEFAULT_LISTING, **defaults, "start_time": None}),
            "description": "",
            "source": {"batch": batch_folder.name, "position": c["position"]},
            "added_at": _now(),
            "ebay": None,
            "listings": [],
        })
        added.append(card_id)
    return added


# ---------------------------------------------------------------- reading
def load(root: Path, stage: str = "inventory", now: datetime | None = None) -> list[dict]:
    """All cards in a stage, oldest first, each with _stage, _folder and (for inventory) errors."""
    d = stage_dir(root, stage)
    out = []
    if d.exists():
        for f in d.iterdir():
            try:
                card = json.loads((f / "card.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError, NotADirectoryError):
                continue
            card["_stage"], card["_folder"] = stage, f
            if stage == "inventory":
                if not card.get("title_custom"):
                    auto = build_title(card["info"])
                    if auto != card.get("title"):  # e.g. after a change to the title format
                        card["title"] = auto
                        save(card)
                card["errors"] = errors(card, now)
            out.append(card)
    return sorted(out, key=lambda c: (c.get("added_at", ""), c["id"]))


def get(root: Path, card_id: str, stage: str | None = None) -> dict:
    for s in ([stage] if stage else STAGES):
        f = stage_dir(root, s) / _safe(card_id)
        if (f / "card.json").exists():
            card = json.loads((f / "card.json").read_text(encoding="utf-8"))
            card["_stage"], card["_folder"] = s, f
            return card
    raise InventoryError(f"No card {card_id}" + (f" in {stage}" if stage else ""))


def save(card: dict) -> None:
    _write(card["_folder"], card)


def errors(card: dict, now: datetime | None = None) -> list[str]:
    return validate_card({**card, "skip": False}, now=now)


def image(root: Path, stage: str, card_id: str, name: str) -> Path | None:
    """A card photo, only front.jpg/back.jpg of a real card."""
    if name not in ("front.jpg", "back.jpg") or stage not in STAGES or _safe(card_id) != card_id:
        return None
    f = stage_dir(root, stage) / card_id / name
    return f if f.is_file() else None


# ---------------------------------------------------------------- editing (inventory only)
LISTING_KEYS = tuple(DEFAULT_LISTING)


def _clean_listing(lst: dict) -> dict:
    for k in ("price", "start_price", "buy_it_now", "reserve", "shipping_cost"):
        lst[k] = _num(lst.get(k))
    lst["duration_days"] = int(lst.get("duration_days") or 7)
    if lst.get("format") not in ("auction", "fixed_price"):
        lst["format"] = "auction"
    t = parse_time(lst.get("start_time"))
    lst["start_time"] = iso(t) if t else None
    return lst


def update(root: Path, changes: list[dict]) -> None:
    """Per-card edits: {id, title?, title_custom?, description?, listing: {...}}."""
    for ch in changes:
        card = get(root, ch.get("id", ""), "inventory")
        if "description" in ch:
            card["description"] = str(ch["description"] or "")[:4000]
        if "listing" in ch:
            card["listing"].update({k: ch["listing"][k] for k in LISTING_KEYS if k in ch["listing"]})
        if "title_custom" in ch:
            card["title_custom"] = bool(ch["title_custom"])
        if card.get("title_custom") and "title" in ch:
            card["title"] = str(ch["title"])
        elif not card.get("title_custom"):
            card["title"] = build_title(card["info"])
        _clean_listing(card["listing"])
        save(card)


def bulk(root: Path, ids: list[str], listing: dict) -> int:
    """Apply the same listing settings to many cards; start times are left alone."""
    values = {k: listing[k] for k in LISTING_KEYS if k in listing and k != "start_time"}
    for card_id in ids:
        card = get(root, card_id, "inventory")
        card["listing"].update(values)
        _clean_listing(card["listing"])
        save(card)
    return len(ids)


def stagger(root: Path, ids: list[str], first_start, interval_minutes: float) -> None:
    """Start times first_start, +interval, +2×interval… in the order given (the order on the page)."""
    first = parse_time(first_start)
    if first is None:
        raise InventoryError("Pick when the first card should start")
    step = timedelta(minutes=max(0.0, float(interval_minutes or 0)))
    for n, card_id in enumerate(ids):
        card = get(root, card_id, "inventory")
        card["listing"]["start_time"] = iso(first + n * step)
        save(card)


def clear_start_times(root: Path, ids: list[str]) -> None:
    for card_id in ids:
        card = get(root, card_id, "inventory")
        card["listing"]["start_time"] = None
        save(card)


def rotate(root: Path, ids: list[str], side: str = "front", degrees: int = 90) -> None:
    """Turn cards' front (or back) photos clockwise by 90/180/270°."""
    from .images import rotate_file

    if side not in ("front", "back") or int(degrees) % 360 not in (90, 180, 270):
        raise InventoryError("Rotate by 90, 180 or 270 degrees, front or back")
    for card_id in ids:
        card = get(root, card_id, "inventory")
        if not card.get(side):
            continue
        rotate_file(card["_folder"] / card[side], int(degrees))
        card["img_v"] = card.get("img_v", 0) + 1
        if card.get("ebay"):
            card["ebay"].pop("image_urls", None)  # photos changed: upload them again
        save(card)


def delete(root: Path, ids: list[str]) -> int:
    """Remove cards from the inventory into ROOT/deleted (not erased, in case of a mistake)."""
    trash = Path(root) / "deleted"
    trash.mkdir(parents=True, exist_ok=True)
    for card_id in ids:
        card = get(root, card_id, "inventory")
        dest = trash / card["id"]
        n = 2
        while dest.exists():
            dest = trash / f"{card['id']}-{n}"
            n += 1
        card["deleted_at"] = _now()
        save(card)
        shutil.move(str(card["_folder"]), dest)
    return len(ids)


# ---------------------------------------------------------------- moving between stages
def relocate(root: Path, card: dict, to: str) -> dict:
    dest = stage_dir(root, to) / card["id"]
    if dest.exists():
        raise InventoryError(f"{card['id']} is already in {to}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(card["_folder"]), dest)
    card["_stage"], card["_folder"] = to, dest
    return card


def mark_sold(root: Path, ids: list[str]) -> None:
    for card_id in ids:
        card = get(root, card_id, "listed")
        if card.get("ebay"):
            card["ebay"]["outcome"], card["ebay"]["ended_at"] = "sold", _now()
        card["sold_at"] = _now()
        relocate(root, card, "sold")
        save(card)


def back_to_inventory(root: Path, ids: list[str]) -> None:
    """Didn't sell: keep the old listing in the card's history, ready to re-price and list again."""
    for card_id in ids:
        card = get(root, card_id, "listed")
        if card.get("ebay"):
            card.setdefault("listings", []).append({**card["ebay"], "outcome": "unsold", "ended_at": _now()})
        card["ebay"] = None
        card["listing"]["start_time"] = None
        relocate(root, card, "inventory")
        save(card)


def counts(root: Path) -> dict:
    return {s: sum(1 for f in stage_dir(root, s).glob("*/card.json")) if stage_dir(root, s).exists() else 0
            for s in STAGES}
