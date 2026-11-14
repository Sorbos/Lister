"""Watch-folder tests: pairing decisions, timeouts, problems, and the all-batches page."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from lister.images import detect_cards, load
from lister.pairing import SheetCheck
from lister.review import ReviewServer
from lister.watcher import Watcher, natural_key

SAMPLES = Path(__file__).parent.parent / "samples"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class SheetClient:
    """Fake Claude for the front/back sheet check."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kw):
        a = self.answers.pop(0)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=a, model=kw["model"],
                               usage={"input_tokens": 3000, "output_tokens": 300})


def check(first, second, same):
    return SheetCheck(first=first, second=second, same_cards=same, reason="test")


@pytest.fixture
def eight_cards(tmp_path):
    """The front scan with card 9 painted over: a scan that doesn't line up with the backs."""
    img = load(SAMPLES / "fronts_01.jpg")
    card = detect_cards(img)[8]
    bg = tuple(int(v) for v in img[20, 20])
    cv2.fillPoly(img, [card.corners.astype(np.int32)], bg)
    cv2.drawContours(img, [card.corners.astype(np.int32)], -1, bg, 40)
    p = tmp_path / "eight.jpg"
    cv2.imwrite(str(p), img)
    return p


def make(tmp_path, client=None, **kw):
    clock = Clock()
    identified = []

    def fake_identify(folder, model, progress, **_):
        identified.append(Path(folder).name)
        return []

    w = Watcher(tmp_path / "Lister", client=client, clock=clock, progress=lambda *_: None,
                identify=fake_identify, background=False, **kw)
    return w, clock, identified


def drop(w, src, name):
    shutil.copy(src, w.inbox / name)


def settle(w, clock):
    w.poll()  # first sighting
    clock.t += 5
    w.poll()


def batches(w):
    return sorted(p.name for p in w.batches.iterdir())


def manifest(w, name):
    return json.loads((w.batches / name / "manifest.json").read_text())


def test_natural_order():
    names = ["IMG_10.jpg", "IMG_9.jpg", "IMG_0046.jpg", "IMG_0045.jpg"]
    assert [p.name for p in sorted(map(Path, names), key=natural_key)] == \
        ["IMG_9.jpg", "IMG_10.jpg", "IMG_0045.jpg", "IMG_0046.jpg"]


def test_pair_front_then_back(tmp_path):
    w, clock, identified = make(tmp_path, client=SheetClient(check("fronts", "backs", True)))
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_0045.jpg")
    drop(w, SAMPLES / "backs_01.jpg", "IMG_0046.jpg")
    settle(w, clock)
    [name] = batches(w)
    m = manifest(w, name)
    assert len(m["cards"]) == 9 and all(c.get("back") for c in m["cards"])
    assert m["source"] == {"fronts": "IMG_0045.jpg", "backs": "IMG_0046.jpg"}
    assert not list(w.inbox.iterdir())
    assert sorted(p.name for p in (w.done / name).iterdir()) == ["IMG_0045.jpg", "IMG_0046.jpg"]
    assert identified == [name]
    assert json.loads((w.batches / name / "status.json").read_text())["stage"] == "ready"


def test_waits_for_file_to_finish_writing(tmp_path):
    w, clock, _ = make(tmp_path)
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_1.jpg")
    w.poll()
    clock.t += 5
    with open(w.inbox / "IMG_1.jpg", "ab") as f:  # scanner still writing
        f.write(b"\0")
    w.poll()
    assert batches(w) == []


def test_backs_scanned_first_are_swapped(tmp_path):
    w, clock, _ = make(tmp_path, client=SheetClient(check("backs", "fronts", True)))
    drop(w, SAMPLES / "backs_01.jpg", "IMG_0001.jpg")
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_0002.jpg")
    settle(w, clock)
    [name] = batches(w)
    assert manifest(w, name)["source"] == {"fronts": "IMG_0002.jpg", "backs": "IMG_0001.jpg"}


def test_fronts_only_then_next_pair(tmp_path, eight_cards):
    """Fronts-only batch, then a normal pair: the shapes don't line up, so no off-by-one."""
    client = SheetClient(check("fronts", "backs", True))
    w, clock, _ = make(tmp_path, client=client)
    drop(w, eight_cards, "IMG_0001.jpg")  # fronts only (8 cards)
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_0002.jpg")
    drop(w, SAMPLES / "backs_01.jpg", "IMG_0003.jpg")
    settle(w, clock)
    a, b = batches(w)
    assert manifest(w, a)["source"] == {"fronts": "IMG_0001.jpg", "backs": None}
    assert manifest(w, b)["source"] == {"fronts": "IMG_0002.jpg", "backs": "IMG_0003.jpg"}
    assert client.answers == []  # Claude only asked about the pair whose shapes matched


def test_claude_says_different_cards(tmp_path):
    """Two front scans laid out alike: shapes match, Claude says they're different cards."""
    w, clock, _ = make(tmp_path, client=SheetClient(check("fronts", "fronts", False)), pair_timeout=60)
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_0001.jpg")
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_0002.jpg")
    settle(w, clock)
    [name] = batches(w)
    assert manifest(w, name)["source"]["backs"] is None
    assert [p.name for p in w.inbox.iterdir()] == ["IMG_0002.jpg"]  # waits for its own back
    clock.t += 61
    w.poll()
    assert len(batches(w)) == 2


def test_lone_backs_go_to_problems(tmp_path):
    w, clock, _ = make(tmp_path, client=SheetClient(check("backs", "fronts", False)))
    drop(w, SAMPLES / "backs_01.jpg", "IMG_0001.jpg")
    drop(w, SAMPLES / "backs_01.jpg", "IMG_0002.jpg")
    settle(w, clock)
    assert (w.problems / "IMG_0001.jpg").exists()
    assert "backs with no matching front" in (w.problems / "IMG_0001.txt").read_text()


def test_lone_scan_waits_then_goes_alone(tmp_path):
    w, clock, _ = make(tmp_path, pair_timeout=600)
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_1.jpg")
    settle(w, clock)
    assert batches(w) == []
    clock.t += 601
    w.poll()
    [name] = batches(w)
    assert "no back scan" in json.loads((w.batches / name / "status.json").read_text())["message"]


def test_unreadable_scan_goes_to_problems(tmp_path):
    w, clock, _ = make(tmp_path)
    cv2.imwrite(str(w.inbox / "IMG_1.jpg"), np.full((800, 600, 3), 40, np.uint8))
    settle(w, clock)
    assert (w.problems / "IMG_1.txt").read_text().startswith("Couldn't find cards")


def test_claude_down_still_pairs_by_shape(tmp_path):
    class Broken:
        beta = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: (_ for _ in ()).throw(ConnectionError())))

    w, clock, _ = make(tmp_path, client=Broken())
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_1.jpg")
    drop(w, SAMPLES / "backs_01.jpg", "IMG_2.jpg")
    settle(w, clock)
    [name] = batches(w)
    assert manifest(w, name)["source"]["backs"] == "IMG_2.jpg"
    assert "please confirm" in json.loads((w.batches / name / "status.json").read_text())["message"]


def test_identify_failure_leaves_batch_reviewable(tmp_path):
    def boom(folder, **_):
        raise RuntimeError("no API key")

    w = Watcher(tmp_path / "L", clock=Clock(), progress=lambda *_: None, identify=boom, background=False)
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_1.jpg")
    w.make_batch(w.inbox / "IMG_1.jpg", None)
    [name] = batches(w)
    st = json.loads((w.batches / name / "status.json").read_text())
    assert st["stage"] == "error" and "no API key" in st["message"]


# ---------------------------------------------------------------- all-batches page
def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


def test_hub_lists_and_serves_batches(tmp_path):
    w, clock, _ = make(tmp_path, client=SheetClient(check("fronts", "backs", True)))
    drop(w, SAMPLES / "fronts_01.jpg", "IMG_0045.jpg")
    drop(w, SAMPLES / "backs_01.jpg", "IMG_0046.jpg")
    settle(w, clock)
    [name] = batches(w)
    (w.problems / "bad.txt").write_text("Couldn't find cards")

    srv = ReviewServer(w.root, port=0, hub=True)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        status, page = get(srv.url)
        assert status == 200 and name.encode() in page and b"9 cards" in page and b"Problems" in page
        state = json.loads(get(srv.url + f"b/{name}/api/state")[1])
        assert state["hub"] is True and len(state["cards"]) == 9
        assert get(srv.url + f"b/{name}/img/card_01_back.jpg")[0] == 200
        assert b"Lister Review" in get(srv.url + f"b/{name}/")[1]
        # A batch still being read isn't opened; paths outside batches/ aren't served.
        (w.batches / name / "status.json").write_text(json.dumps({"stage": "reading"}))
        for bad in (f"b/{name}/api/state", "b/../inbox/api/state", "b/..%2Fdone/api/state"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(srv.url + bad)
            assert e.value.code == 404
        assert b"reading" in get(srv.url)[1]
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()
