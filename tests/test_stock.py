"""Stock description, per-card description, shipping cost (stock / per card / bulk) and removing cards."""

import json
from pathlib import Path

import pytest

from lister import inventory, settings
from lister.config import EbaySettings
from lister.listing import build_description, build_offer, effective_shipping

from test_inventory import call, finish_batch, hub  # noqa: F401  (fixture)


def card(**listing):
    return {"title": "1978 The Muppet Show #60 Fozzie Bear", "description": "",
            "info": {"category": "non_sport", "year": "1978", "set_name": "The Muppet Show", "card_number": "60",
                     "subject": "Fozzie Bear", "condition": "excellent"},
            "listing": {"format": "auction", "start_price": 0.99, "duration_days": 7, "buy_it_now": None,
                        "reserve": None, "price": None, "start_time": None, "shipping_cost": None, **listing}}


def test_description_order_and_safety():
    c = card()
    c["description"] = "Small crease on the back.\n\nCentered well <b>really</b>."
    out = build_description(c, "Ships in a top loader.\nCombined shipping available.")
    title, mine, table, stock = (out.index(x) for x in ("<h2>", "Small crease", "<table>", "Ships in a top loader"))
    assert title < mine < table < stock
    assert "<p>Small crease on the back.</p><p>Centered well &lt;b&gt;really&lt;/b&gt;.</p>" in out  # escaped
    assert "<p>Ships in a top loader.<br>Combined shipping available.</p>" in out
    assert "<p>" not in build_description(card(), "")  # nothing empty added


def test_shipping_override_card_then_stock_then_policy():
    s = EbaySettings("", "", "", "sandbox", "EBAY_US")
    stock = {"listing_defaults": {"shipping_cost": 1.5}, "stock_description": "x"}
    assert effective_shipping(card(), None) is None
    assert effective_shipping(card(), stock) == 1.5
    assert effective_shipping(card(shipping_cost=0), stock) == 0  # free shipping on this card
    offer = build_offer(card(shipping_cost=4.25), "SKU", s, True, stock)
    assert offer["listingPolicies"]["shippingCostOverrides"] == [
        {"priority": 1, "shippingServiceType": "DOMESTIC", "shippingCost": {"value": "4.25", "currency": "USD"}}]
    assert "shippingCostOverrides" not in build_offer(card(), "SKU", s, True, {})["listingPolicies"]
    assert "<p>x</p>" in build_offer(card(), "SKU", s, True, stock)["listingDescription"]


def test_settings_page_saves_stock_text_and_defaults(hub):  # noqa: F811
    s = json.loads(call(hub, "api/settings"))
    assert "scans of the actual card" in s["stock_description"] and s["listing_defaults"]["shipping_cost"] is None
    out = json.loads(call(hub, "api/settings", {"stock_description": "Ships in a top loader."}))
    assert out["result"] == "ok" and settings.load(hub.folder)["stock_description"] == "Ships in a top loader."
    out = json.loads(call(hub, "api/settings", {"listing_defaults": {"format": "auction", "start_price": 2,
                                                                     "duration_days": 5, "shipping_cost": 1.25}}))
    assert out["result"] == "ok" and out["listing_defaults"]["shipping_cost"] == 1.25
    assert json.loads(call(hub, "api/settings", {"listing_defaults": {"format": "fixed_price", "price": None}}))[
        "result"] == "error"
    assert json.loads(call(hub, "api/settings", {"listing_defaults": {"shipping_cost": -1}}))["result"] == "error"
    # Existing inventory cards follow the stock shipping cost; the page knows it for its placeholders.
    d = json.loads(call(hub, "api/inventory"))
    assert d["stock"] == {"shipping_cost": 1.25, "has_description": True}
    assert all(c["listing"]["shipping_cost"] is None for c in d["cards"])
    # New cards start with the new defaults (but shipping stays "stock" so it keeps following Settings).
    finish_batch(hub.folder, "2026-11-29_IMG_0050")
    new = [c for c in inventory.load(hub.folder) if c["id"].startswith("2026-11-29")]
    assert new and all(c["listing"]["start_price"] == 2 and c["listing"]["duration_days"] == 5
                       and c["listing"]["shipping_cost"] is None for c in new)


def test_card_description_and_bulk_shipping(hub):  # noqa: F811
    ids = [c["id"] for c in inventory.load(hub.folder)]
    d = json.loads(call(hub, "api/inventory", {"action": "update", "cards": [{"id": ids[0], "description": "Crease."}]}))
    assert d["cards"][0]["description"] == "Crease."
    d = json.loads(call(hub, "api/inventory", {"action": "bulk", "ids": ids[:3], "listing": {"shipping_cost": 3}}))
    assert [c["listing"]["shipping_cost"] for c in d["cards"][:4]] == [3, 3, 3, None]
    assert d["cards"][0]["listing"]["start_price"] == 0.99  # other settings untouched
    d = json.loads(call(hub, "api/inventory", {"action": "bulk", "ids": ids[:1], "listing": {"shipping_cost": None}}))
    assert d["cards"][0]["listing"]["shipping_cost"] is None  # back to stock


def test_remove_selected_cards(hub):  # noqa: F811
    ids = [c["id"] for c in inventory.load(hub.folder)]
    d = json.loads(call(hub, "api/inventory", {"action": "delete", "ids": ids[:2]}))
    assert [c["id"] for c in d["cards"]] == ids[2:] and d["counts"]["inventory"] == len(ids) - 2
    kept = Path(hub.folder) / "deleted" / ids[0]
    assert (kept / "front.jpg").exists() and json.loads((kept / "card.json").read_text())["deleted_at"]
    with pytest.raises(inventory.InventoryError):
        inventory.get(hub.folder, ids[0])
