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
