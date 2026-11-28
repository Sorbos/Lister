"""Turn inventory cards into eBay listings.

For each card:
  1. upload front + back photos to eBay
  2. create/replace an inventory item (title, condition, item specifics, photos)
  3. create/update an offer (auction or fixed price, policies, optional scheduled start)
  4. publish it, either now or, in "local" schedule mode, at its start time
  5. move the card from ROOT/inventory to ROOT/listed

Progress is saved in each card's card.json after every step, so re-running continues where it
left off and never lists the same card twice.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import EbaySettings
from .review import parse_time, validate_card

CATEGORY = {"sports": "261328", "non_sport": "183050"}  # Sports / Non-Sport Trading Card Singles
CONDITION_UNGRADED, CONDITION_GRADED = "4000", "2750"
CONDITION_ENUM = {CONDITION_UNGRADED: "USED_VERY_GOOD", CONDITION_GRADED: "LIKE_NEW"}
DESC_CARD_CONDITION, DESC_GRADER, DESC_GRADE = "40001", "27501", "27502"
CONDITION_WORDS = {"near_mint_or_better": "near mint", "excellent": "excellent", "very_good": "very good",
                   "poor": "poor"}
CONDITION_LABEL = {"near_mint_or_better": "Near Mint or Better", "excellent": "Excellent",
                   "very_good": "Very Good", "poor": "Poor"}

# Our field → eBay item-specific names to try, first match wins.
ASPECT_CANDIDATES = {
    "sport": ["Sport"],
    "subject": ["Player/Athlete", "Character", "Player"],
    "team": ["Team"],
    "league": ["League"],
    "manufacturer": ["Manufacturer"],
    "set_name": ["Set"],
    "subset_insert": ["Insert Set"],
    "parallel": ["Parallel/Variety"],
    "card_number": ["Card Number"],
    "season": ["Season"],
    "year_made": ["Year Manufactured"],
    "autographed": ["Autographed"],
    "graded": ["Graded"],
    "grader": ["Professional Grader"],
    "grade": ["Grade"],
}
FEATURE_ASPECTS = ["Features", "Attributes"]


class ListingError(RuntimeError):
    pass


# ---------------------------------------------------------------- payload building
def category_for(info: dict) -> str:
    return CATEGORY["non_sport" if info.get("category") == "non_sport" else "sports"]


def _match(values: list[dict], key: str, want: str, exact=False) -> str | None:
    want_l = want.lower().strip()
    for v in values:
        name = str(v.get(key + "Name", "")).lower()
        if (name == want_l) if exact else (want_l in name or (want_l and name in want_l and len(name) > 1)):
            return v.get(key + "Id")
    return None


def condition_fields(info: dict, policy: dict | None) -> dict:
    """condition + conditionDescriptors for the inventory item.

    The numeric value IDs (e.g. which code means "Excellent" or "PSA") come from eBay's
    condition policy for the category, fetched at run time.
    """
    graded = bool(info.get("graded"))
    cid = CONDITION_GRADED if graded else CONDITION_UNGRADED
    out = {"condition": CONDITION_ENUM[cid]}
    if policy is None:  # offline dry run
        out["conditionDescriptors"] = [{"name": "(looked up from eBay)", "values": [info.get("condition")]}]
        return out
    cond = next((c for c in policy.get("itemConditions", []) if str(c.get("conditionId")) == cid), None)
    if cond is None:
        raise ListingError(f"eBay doesn't offer condition {cid} for this category")
    descs = {str(d["conditionDescriptorId"]): d.get("conditionDescriptorValues", [])
             for d in cond.get("conditionDescriptors", [])}
    if graded:
        if not (info.get("grader") and info.get("grade")):
            raise ListingError("Graded card needs a grader and a grade")
        grader = _match(descs.get(DESC_GRADER, []), "conditionDescriptorValue", info["grader"])
        grade = _match(descs.get(DESC_GRADE, []), "conditionDescriptorValue", str(info["grade"]), exact=True)
        if not grader:
            raise ListingError(f"eBay doesn't recognise grader '{info['grader']}'")
        if not grade:
            raise ListingError(f"eBay doesn't recognise grade '{info['grade']}'")
        out["conditionDescriptors"] = [{"name": DESC_GRADER, "values": [str(grader)]},
                                       {"name": DESC_GRADE, "values": [str(grade)]}]
    else:
        word = CONDITION_WORDS[info.get("condition") or "excellent"]
        value = _match(descs.get(DESC_CARD_CONDITION, []), "conditionDescriptorValue", word)
        if not value:
            raise ListingError(f"eBay has no card condition matching '{word}'")
        out["conditionDescriptors"] = [{"name": DESC_CARD_CONDITION, "values": [str(value)]}]
    return out


def _our_aspect_values(info: dict) -> dict[str, str]:
    year = info.get("year") or ""
    vals = {k: info.get(k) for k in ("sport", "subject", "team", "league", "manufacturer", "set_name",
                                     "subset_insert", "parallel", "card_number")}
    if info.get("category") != "non_sport" and year:
        vals["season"] = year
    if re.match(r"\d{4}", year):
        vals["year_made"] = year[:4]
    vals["autographed"] = "Yes" if info.get("autograph") else "No"
    vals["graded"] = "Yes" if info.get("graded") else "No"
    if info.get("graded"):
        vals["grader"], vals["grade"] = info.get("grader"), info.get("grade")
    return {k: str(v) for k, v in vals.items() if v not in (None, "")}


def build_aspects(info: dict, aspects_meta: list[dict] | None, extra: dict | None = None) -> tuple[dict, list[str]]:
    """Item specifics as {name: [values]} plus warnings (e.g. required ones we couldn't fill)."""
    ours = _our_aspect_values(info)
    out: dict[str, list[str]] = {}
    warnings: list[str] = []
    if aspects_meta is None:  # offline: use our first-choice names
        for k, v in ours.items():
            out[ASPECT_CANDIDATES[k][0]] = [v]
        if info.get("rookie"):
            out["Features"] = ["Rookie"]
    else:
        meta = {a["localizedAspectName"]: a for a in aspects_meta}

        def allowed(name):
            a = meta[name]
            mode = a.get("aspectConstraint", {}).get("aspectMode", "FREE_TEXT")
            return [v["localizedValue"] for v in a.get("aspectValues", [])], mode == "SELECTION_ONLY"

        for k, v in ours.items():
            name = next((n for n in ASPECT_CANDIDATES[k] if n in meta), None)
            if not name:
                continue
            values, strict = allowed(name)
            if strict:
                hit = next((x for x in values if x.lower() == v.lower()), None)
                if hit is None:
                    warnings.append(f"'{v}' isn't one of eBay's choices for {name}; left out")
                    continue
                v = hit
            out[name] = [v]
        feats = [("rookie", "rookie"), ("memorabilia", "patch"), ("memorabilia", "memorabilia"),
                 ("memorabilia", "relic"), ("serial_number", "serial")]
        fname = next((n for n in FEATURE_ASPECTS if n in meta), None)
        if fname:
            values, _ = allowed(fname)
            chosen = []
            for field, word in feats:
                if info.get(field):
                    hit = next((x for x in values if word in x.lower()), None)
                    if hit and hit not in chosen:
                        chosen.append(hit)
            if chosen:
                out[fname] = chosen
    for name, value in (extra or {}).items():
        out.setdefault(name, value if isinstance(value, list) else [value])
    if aspects_meta is not None:
        for a in aspects_meta:
            if a.get("aspectConstraint", {}).get("aspectRequired") and a["localizedAspectName"] not in out:
                warnings.append(f"eBay requires '{a['localizedAspectName']}' for this category; "
                                f"add it under extra_aspects in setup.json")
    return out, warnings


def build_description(card: dict) -> str:
    info = card["info"]
    rows = [
        ("Year", info.get("year")), ("Manufacturer", info.get("manufacturer")), ("Set", info.get("set_name")),
        ("Insert", info.get("subset_insert")), ("Parallel", info.get("parallel")),
        ("Card #", info.get("card_number")), ("Player / subject", info.get("subject")), ("Team", info.get("team")),
        ("Rookie card", "Yes" if info.get("rookie") else None),
        ("Autograph", "Yes" if info.get("autograph") else None),
        ("Serial #", info.get("serial_number")),
    ]
    if info.get("graded"):
        rows.append(("Grade", f"{info.get('grader')} {info.get('grade')}"))
    else:
        rows.append(("Condition", CONDITION_LABEL.get(info.get("condition"), "")))
    table = "".join(f"<tr><td><b>{html.escape(k)}</b></td><td>{html.escape(str(v))}</td></tr>" for k, v in rows if v)
    return (f"<h2>{html.escape(card['title'])}</h2><table>{table}</table>"
            "<p>The photos are scans of the actual card you will receive. Please look at them closely "
            "for corners, edges and surface.</p>")


def build_item(card: dict, image_urls: list[str], policy: dict | None, aspects_meta, settings: EbaySettings):
    aspects, warnings = build_aspects(card["info"], aspects_meta,
                                      settings.extra_aspects.get(category_for(card["info"])))
    body = {
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
        **condition_fields(card["info"], policy),
        "product": {"title": card["title"], "aspects": aspects, "imageUrls": image_urls},
    }
    return body, warnings


def _money(v, currency):
    return {"value": f"{float(v):.2f}", "currency": currency}


def build_offer(card: dict, sku: str, settings: EbaySettings, send_start_time: bool) -> dict:
    lst = card["listing"]
    cur = settings.currency
    offer = {
        "sku": sku,
        "marketplaceId": settings.marketplace,
        "availableQuantity": 1,
        "categoryId": category_for(card["info"]),
        "listingDescription": build_description(card),
        "merchantLocationKey": settings.location_key,
        "listingPolicies": {"fulfillmentPolicyId": settings.fulfillment_policy_id,
                            "paymentPolicyId": settings.payment_policy_id,
                            "returnPolicyId": settings.return_policy_id},
    }
    if lst["format"] == "auction":
        pricing = {"auctionStartPrice": _money(lst["start_price"], cur)}
        if lst.get("reserve"):
            pricing["auctionReservePrice"] = _money(lst["reserve"], cur)
        if lst.get("buy_it_now"):
            pricing["price"] = _money(lst["buy_it_now"], cur)
        offer.update(format="AUCTION", listingDuration=f"DAYS_{lst['duration_days']}", pricingSummary=pricing)
    else:
        offer.update(format="FIXED_PRICE", listingDuration="GTC",
                     pricingSummary={"price": _money(lst["price"], cur)})
    if send_start_time and lst.get("start_time"):
        offer["listingStartDate"] = parse_time(lst["start_time"]).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return offer


# ---------------------------------------------------------------- the listing run
def sku_for(card: dict) -> str:
    """eBay SKU for this listing attempt; a relisted card gets a new one (…-2, …-3)."""
    attempt = len(card.get("listings") or []) + 1
    base = re.sub(r"[^A-Za-z0-9-]+", "-", card["id"]).strip("-")
    return f"L-{base}"[:46] + f"-{attempt}"


def list_cards(root: str | Path, ids: list[str], settings: EbaySettings, client=None, *, dry_run: bool = False,
               progress=print, clock=time.time, sleep=time.sleep, on_card=None) -> list[dict]:
    """List inventory cards on eBay. Each listed card moves to ROOT/listed.

    Progress is saved in each card's card.json after every step, so if something fails part-way,
    running it again continues where it stopped and never creates a second listing.
    client=None only makes sense with dry_run (offline preview). on_card(done, total) reports progress.
    """
    from . import inventory

    root = Path(root)
    if not ids:
        raise ListingError("No cards selected.")
    cards = [inventory.get(root, i, "inventory") for i in ids]
    if not dry_run:
        missing = [k for k in ("fulfillment_policy_id", "payment_policy_id", "return_policy_id", "location_key")
                   if not getattr(settings, k)]
        if missing:
            raise ListingError("eBay setup isn't finished (" + ", ".join(missing) + "). Run: lister ebay setup")

    now = datetime.fromtimestamp(clock(), timezone.utc)
    problems = [(c, validate_card({**c, "skip": False}, now=now)) for c in cards]
    problems = [(c, e) for c, e in problems if e]
    if problems:
        lines = [f"  {c['title'] or c['id']}: {'; '.join(e)}" for c, e in problems]
        raise ListingError("Fix these in the inventory first:\n" + "\n".join(lines))

    local_mode = settings.schedule_mode == "local"
    meta: dict[str, tuple] = {}
    previews, queue, done = [], [], 0
    total = len(cards)

    def tick():
        if on_card:
            on_card(done, total)

    for card in cards:
        name = card["title"]
        rec = card.get("ebay") or {}
        if rec.get("environment") not in (None, settings.environment):
            rec = {}  # a sandbox test doesn't count as a live listing
        sku = rec.get("sku") or sku_for(card)
        cat = category_for(card["info"])
        if cat not in meta:
            meta[cat] = (client.condition_policy(cat), client.item_aspects(cat)) if client else (None, None)
        policy, aspects_meta = meta[cat]
        folder = card["_folder"]

        if dry_run:
            images = [str(folder / card[k]) for k in ("front", "back") if card.get(k)]
        else:
            rec.update(environment=settings.environment, sku=sku)
            card["ebay"] = rec
            images = rec.get("image_urls")
            if not images:
                progress(f"{name}: uploading photos…")
                images = [client.upload_image(folder / card[k]) for k in ("front", "back") if card.get(k)]
                rec["image_urls"] = images
                inventory.save(card)

        item, warnings = build_item(card, images, policy, aspects_meta, settings)
        offer = build_offer(card, sku, settings, send_start_time=not local_mode)
        for w in warnings:
            progress(f"{name}: note: {w}")

        start = parse_time(card["listing"].get("start_time"))
        when = (f"scheduled for {start.astimezone():%a %d %b %H:%M}" if start else "goes live now")
        if dry_run:
            previews.append({"id": card["id"], "sku": sku, "inventory_item": item, "offer": offer, "notes": warnings})
            progress(f"[preview] {name}  ({offer['format']}, {when})")
            done += 1
            tick()
            continue

        client.put_inventory_item(sku, item)
        rec["offer_id"] = client.create_or_update_offer(sku, offer)
        inventory.save(card)

        if local_mode and start and start > datetime.fromtimestamp(clock(), timezone.utc):
            queue.append((start, card))
            progress(f"{name}: ready, will go live at {start.astimezone():%a %d %b %H:%M}")
            continue
        _publish(root, card, progress, clock, when, client)
        done += 1
        tick()

    if queue:
        progress(f"Keep Lister running: {len(queue)} card(s) will go live at their start times.")
        for start, card in sorted(queue, key=lambda q: q[0]):
            wait = (start - datetime.fromtimestamp(clock(), timezone.utc)).total_seconds()
            if wait > 0:
                sleep(wait)
            _publish(root, card, progress, clock, "went live", client)
            done += 1
            tick()

    if dry_run:
        (root / "ebay_preview.json").write_text(json.dumps(previews, indent=2))
        progress(f"Preview only: nothing was sent to eBay. Details in {root / 'ebay_preview.json'}")
        return previews
    return cards


def _publish(root: Path, card: dict, progress, clock, when: str, client) -> None:
    from . import inventory

    rec = card["ebay"]
    rec["listing_id"] = client.publish_offer(rec["offer_id"])
    rec["published_at"] = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
    start = parse_time(card["listing"].get("start_time"))
    if card["listing"]["format"] == "auction":
        begin = start or datetime.fromtimestamp(clock(), timezone.utc)
        rec["ends_at"] = (begin + timedelta(days=card["listing"]["duration_days"])).isoformat()
    rec["listing"] = dict(card["listing"])
    inventory.save(card)
    inventory.relocate(root, card, "listed")
    inventory.save(card)
    progress(f"{card['title']}: listed as eBay item {rec['listing_id']} ({when})")
