import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lister.cli import main
from lister.review import ReviewServer

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def server(tmp_path):
    main(["split", str(SAMPLES / "fronts_01.jpg"), str(SAMPLES / "backs_01.jpg"), "-o", str(tmp_path)])
    cards = json.loads((tmp_path / "manifest.json").read_text())["cards"]
    info = dict(category="non_sport", year="1978", set_name="The Muppet Show", card_number="60",
                subject="Fozzie Bear", rookie=False, autograph=False, memorabilia=False, graded=False,
                condition="excellent", confidence="medium", uncertain_fields=["set_name"], notes="")
    cards[0] = {**cards[0], "info": info, "title": "1978 The Muppet Show Fozzie Bear #60", "error": None}
    (tmp_path / "cards.json").write_text(json.dumps({"cards": cards}))
    srv = ReviewServer(tmp_path, port=0)
    t = threading.Thread(target=srv.httpd.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


def get(srv, path):
    with urllib.request.urlopen(srv.url.rstrip("/") + path) as r:
        return r.status, r.read()


def post(srv, body, origin=None):
    req = urllib.request.Request(srv.url + "api/state", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_page_and_state(server):
    status, html = get(server, "/")
    assert status == 200 and b"Lister Review" in html
    state = json.loads(get(server, "/api/state")[1])
    assert len(state["cards"]) == 9
    c1 = state["cards"][0]
    assert c1["title"] == "1978 The Muppet Show Fozzie Bear #60"
    assert c1["uncertain"] == ["set_name"]
    assert c1["errors"] == []  # default auction at $0.99 is valid
    # Unidentified cards need a subject before they can be approved.
    assert "Player/subject is empty" in state["cards"][1]["errors"]


def test_edit_rebuilds_title_and_persists(server):
    state = json.loads(get(server, "/api/state")[1])
    card = state["cards"][0]
    card["info"]["rookie"] = True
    card["approved"] = True
    out = post(server, {"cards": [card]}, origin=server.url.rstrip("/"))
    assert out["cards"][0]["title"].endswith("#60 RC")
    assert out["cards"][0]["approved"] is True
    saved = json.loads((server.folder / "review.json").read_text())
    assert saved["cards"][0]["approved"] is True


def test_custom_title_kept(server):
    card = json.loads(get(server, "/api/state")[1])["cards"][0]
    card.update(title="My own title", title_custom=True)
    card["info"]["team"] = "Muppets"
    assert post(server, {"cards": [card]})["cards"][0]["title"] == "My own title"


def test_validation_blocks_approval(server):
    card = json.loads(get(server, "/api/state")[1])["cards"][0]
    card["listing"].update(format="auction", start_price=10, buy_it_now=11)
    card["approved"] = True
    out = post(server, {"cards": [card]})["cards"][0]
    assert any("30%" in e for e in out["errors"])
    assert out["approved"] is False
    card["listing"].update(format="fixed_price", price=None)
    assert "Set a price" in post(server, {"cards": [card]})["cards"][0]["errors"]


def test_cannot_change_image_paths_or_read_other_files(server):
    card = json.loads(get(server, "/api/state")[1])["cards"][0]
    card["front"] = "../../etc/passwd"
    post(server, {"cards": [card]})
    assert json.loads(get(server, "/api/state")[1])["cards"][0]["front"] == "card_01_front.jpg"
    for bad in ("/img/manifest.json", "/img/../manifest.json", "/img/review.json"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(server, bad)
        assert e.value.code == 404


def test_other_websites_cannot_post(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(server, {"cards": []}, origin="https://evil.example")
    assert e.value.code == 403


def test_stagger_assigns_times_in_scanner_order(server):
    from datetime import datetime, timedelta, timezone

    first = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0, second=0)
    state = json.loads(get(server, "/api/state")[1])
    state["cards"][2]["skip"] = True
    out = post(server, {"cards": [state["cards"][2]], "schedule": {"first_start": first.isoformat(), "interval_minutes": 5},
                        "apply_schedule": True})
    times = [c["listing"]["start_time"] for c in out["cards"]]
    assert times[2] is None  # skipped card left alone
    others = [datetime.fromisoformat(t.replace("Z", "+00:00")) for i, t in enumerate(times) if i != 2]
    assert others[0] == first
    assert [b - a for a, b in zip(others, others[1:])] == [timedelta(minutes=5)] * 7
    assert out["cards"][0]["errors"] == []
    # Persisted
    assert json.loads((server.folder / "review.json").read_text())["cards"][1]["listing"]["start_time"] == times[1]


def test_schedule_needs_first_start(server):
    out = post(server, {"schedule": {"first_start": None}, "apply_schedule": True})
    assert "first card" in out["schedule_error"]


def test_start_time_limits():
    from datetime import datetime, timedelta, timezone

    from lister.review import DEFAULT_LISTING, validate_card

    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    card = {"skip": False, "title": "t", "info": {"subject": "x"}, "listing": dict(DEFAULT_LISTING)}
    for delta, msg in [(-1, "past"), (22 * 24, "3 weeks"), (2, None)]:
        card["listing"]["start_time"] = (now + timedelta(hours=delta)).isoformat()
        errs = validate_card(card, now=now)
        assert (msg is None and errs == []) or any(msg in e for e in errs)


def test_state_includes_batch_cost(server):
    data = json.loads((server.folder / "cards.json").read_text())
    data["cost_usd"] = 0.3421
    (server.folder / "cards.json").write_text(json.dumps(data))
    assert json.loads(get(server, "/api/state")[1])["claude_cost_usd"] == 0.3421


def test_claude_results_fill_in_after_page_was_opened(tmp_path):
    """Open the review before Claude has read the cards, then Claude finishes: titles appear,
    but a card you already typed into keeps your edits."""
    import os
    import time

    from lister.review import BatchState

    main(["split", str(SAMPLES / "fronts_01.jpg"), "-o", str(tmp_path)])
    b = BatchState(tmp_path)  # writes review.json with blank cards
    assert b.state["cards"][0]["title"] == ""
    mine = dict(b.state["cards"][1])
    mine["info"] = {**mine["info"], "subject": "My own words"}
    b.update({"cards": [mine]})

    cards = json.loads((tmp_path / "manifest.json").read_text())["cards"]
    info = dict(category="non_sport", year="1978", set_name="The Muppet Show", subject="Fozzie Bear",
                card_number="60", condition="excellent", confidence="high", uncertain_fields=[], notes="")
    cards = [{**c, "info": info, "title": "1978 The Muppet Show Fozzie Bear #60", "error": None} for c in cards]
    (tmp_path / "cards.json").write_text(json.dumps({"cards": cards}))
    later = time.time() + 5
    os.utime(tmp_path / "cards.json", (later, later))

    state = b.payload()  # what the page loads
    assert state["cards"][0]["title"] == "1978 The Muppet Show Fozzie Bear #60"
    assert state["cards"][0]["confidence"] == "high"
    assert state["cards"][1]["info"]["subject"] == "My own words"  # edits kept
    # And a fresh start (e.g. restarting lister review) sees the same.
    again = BatchState(tmp_path).state
    assert again["cards"][0]["title"].startswith("1978") and again["cards"][1]["info"]["subject"] == "My own words"
