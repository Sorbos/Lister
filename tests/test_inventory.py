"""Inventory: finished batches land here; bulk pricing, staggering, listing and the Listed/Sold stages."""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lister import inventory
from lister.cli import main
from lister.review import BatchState, ReviewServer

SAMPLES = Path(__file__).parent.parent / "samples"


def finish_batch(root, name, approve=range(1, 10), skip=()):
    folder = root / "batches" / name
    main(["split", str(SAMPLES / "fronts_01.jpg"), str(SAMPLES / "backs_01.jpg"), "-o", str(folder)])
    b = BatchState(folder)
    cards = []
    for c in b.state["cards"]:
        c = dict(c)
        c["info"] = {**c["info"], "subject": f"Player {c['position']}", "year": "1990-91", "set_name": "Upper Deck",
                     "card_number": str(c["position"])}
        c["approved"], c["skip"] = c["position"] in approve, c["position"] in skip
        cards.append(c)
    b.update({"cards": cards})
    return b


def test_batch_lands_in_inventory_only_when_finished(tmp_path):
    root = tmp_path / "Lister"
    b = finish_batch(root, "2026-11-28_IMG_0045", approve=range(1, 9))  # card 9 neither approved nor skipped
    assert not b.state.get("finished") and inventory.load(root) == []
    cards = b.state["cards"]
    cards[8]["skip"] = True
    b.update({"cards": [cards[8]]})
    inv = inventory.load(root)
    assert len(inv) == 8 and b.state["finished"]["ids"] == [c["id"] for c in inv]
    c = inv[0]
    assert c["id"] == "2026-11-28_IMG_0045-01" and c["title"] == "1990-91 Upper Deck #1 Player 1"
    assert (c["_folder"] / "front.jpg").exists() and (c["_folder"] / "back.jpg").exists()
    assert c["listing"]["format"] == "auction" and c["listing"]["start_price"] == 0.99 and c["errors"] == []
    assert json.loads((root / "batches" / "2026-11-28_IMG_0045" / "status.json").read_text())["stage"] == "done"


def test_bulk_stagger_across_batches(tmp_path):
    root = tmp_path / "Lister"
    finish_batch(root, "2026-11-28_IMG_0045")
    finish_batch(root, "2026-11-28_IMG_0047")
    ids = [c["id"] for c in inventory.load(root)]
    assert len(ids) == 18
    inventory.bulk(root, ids, {"format": "auction", "start_price": 2, "duration_days": 5, "buy_it_now": 10})
    first = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    inventory.stagger(root, ids, first.isoformat(), 3)
    cards = inventory.load(root)
    starts = [datetime.fromisoformat(c["listing"]["start_time"].replace("Z", "+00:00")) for c in cards]
    assert starts[0] == first and starts[9] == first + timedelta(minutes=27)  # continues into the 2nd batch
    assert all(c["listing"]["start_price"] == 2 and c["listing"]["duration_days"] == 5 for c in cards)
    bad = inventory.bulk(root, ids[:1], {"buy_it_now": 2.1}) and inventory.get(root, ids[0])
    assert "30%" in " ".join(inventory.errors(bad))
    with pytest.raises(inventory.InventoryError):
        inventory.stagger(root, ids, None, 3)


def test_custom_title_and_auto(tmp_path):
    root = tmp_path / "Lister"
    finish_batch(root, "B1")
    cid = inventory.load(root)[0]["id"]
    inventory.update(root, [{"id": cid, "title": "My title", "title_custom": True}])
    assert inventory.get(root, cid)["title"] == "My title"
    inventory.update(root, [{"id": cid, "title_custom": False}])
    assert inventory.get(root, cid)["title"] == "1990-91 Upper Deck #1 Player 1"


# ---------------------------------------------------------------- the pages
@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "Lister"
    finish_batch(root, "2026-11-28_IMG_0045")
    srv = ReviewServer(root, port=0, hub=True)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


def call(srv, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(srv.url + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def test_inventory_pages_and_actions(hub):
    for page in ("inventory", "listed"):
        html = call(hub, page).decode()
        assert 'href="/inventory"' in html and 'href="/listed"' in html and f'const STAGE = "{page}"' in html
    d = json.loads(call(hub, "api/inventory"))
    assert len(d["cards"]) == 9 and d["counts"] == {"inventory": 9, "listed": 0, "sold": 0}
    assert d["ebay"]["ready"] is False and "eBay isn't connected" in d["ebay"]["message"]
    ids = [c["id"] for c in d["cards"]]
    assert call(hub, d["cards"][0]["front"].lstrip("/"))[:2] == b"\xff\xd8"  # a JPEG

    d = json.loads(call(hub, "api/inventory", {"action": "bulk", "ids": ids[:3], "listing": {"format": "fixed_price", "price": 5}}))
    assert [c["listing"]["format"] for c in d["cards"][:4]] == ["fixed_price"] * 3 + ["auction"]
    first = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    d = json.loads(call(hub, "api/inventory", {"action": "stagger", "ids": ids, "first_start": first.isoformat(),
                                               "interval_minutes": 2}))
    assert d["cards"][8]["listing"]["start_time"] == (first + timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d = json.loads(call(hub, "api/inventory", {"action": "update", "cards": [{"id": ids[0], "listing": {"price": None}}]}))
    assert "Set a price" in d["cards"][0]["errors"]
    d = json.loads(call(hub, "api/inventory", {"action": "stagger", "ids": ids, "first_start": None}))
    assert "first card" in d["error"]

    # Preview (no eBay keys): runs in the background; nothing moves.
    call(hub, "api/inventory", {"action": "list", "ids": ids[1:3], "dry_run": True})
    for _ in range(50):
        job = json.loads(call(hub, "api/inventory"))["job"]
        if not job["running"]:
            break
        time.sleep(0.1)
    assert job["error"] is None and job["done"] == 2 and any("[preview]" in line for line in job["log"])
    assert json.loads(call(hub, "api/inventory"))["counts"]["inventory"] == 9
    # Real listing without eBay set up is refused with a clear reason.
    call(hub, "api/inventory", {"action": "list", "ids": ids[1:3], "dry_run": False})
    for _ in range(50):
        job = json.loads(call(hub, "api/inventory"))["job"]
        if not job["running"]:
            break
        time.sleep(0.1)
    assert "EBAY_CLIENT_ID" in job["error"]


def test_listed_and_sold_moves(hub):
    root = hub.folder
    cid = inventory.load(root)[0]["id"]
    card = inventory.get(root, cid)
    card["ebay"] = {"listing_id": "110001", "environment": "sandbox", "sku": "L-x-1"}
    inventory.save(card)
    inventory.relocate(root, card, "listed")
    inventory.save(card)
    d = json.loads(call(hub, "api/inventory?stage=listed"))
    assert [c["id"] for c in d["cards"]] == [cid]
    d = json.loads(call(hub, "api/inventory", {"action": "back", "ids": [cid]}))
    assert d["cards"] == [] and inventory.get(root, cid)["_stage"] == "inventory"
    assert inventory.get(root, cid)["listings"][0]["outcome"] == "unsold"


def test_inventory_images_only_card_photos(hub):
    cid = inventory.load(hub.folder)[0]["id"]
    for bad in (f"inv/inventory/{cid}/card.json", f"inv/inventory/../batches/x/front.jpg", f"inv/nope/{cid}/front.jpg"):
        with pytest.raises(urllib.error.HTTPError):
            call(hub, bad)


def test_turn_inventory_photos(hub):
    import cv2

    root = hub.folder
    cards = inventory.load(root)
    ids = [c["id"] for c in cards[:2]]
    card = inventory.get(root, ids[0])
    card["ebay"] = {"image_urls": ["https://i.ebayimg.com/old"]}  # uploaded earlier, then the run stopped
    inventory.save(card)
    before = cv2.imread(str(cards[0]["_folder"] / "front.jpg"))
    d = json.loads(call(hub, "api/inventory", {"action": "rotate", "ids": ids, "side": "front", "degrees": 180}))
    assert d["cards"][0]["front"].endswith("front.jpg?v=1") and d["cards"][2]["front"].endswith("?v=0")
    after = cv2.imread(str(cards[0]["_folder"] / "front.jpg"))
    assert abs(cv2.rotate(before, cv2.ROTATE_180).astype(int) - after.astype(int)).mean() < 3
    assert "image_urls" not in inventory.get(root, ids[0])["ebay"]  # will be uploaded again
    assert call(hub, d["cards"][0]["front"].lstrip("/"))[:2] == b"\xff\xd8"
    bad = json.loads(call(hub, "api/inventory", {"action": "rotate", "ids": ids, "degrees": 45}))
    assert "90, 180 or 270" in bad["error"]
