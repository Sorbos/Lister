import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import cv2

from lister.cli import main
from lister.identify import CardInfo, identify_batch
from lister.title import build_title

SAMPLES = Path(__file__).parent.parent / "samples"


def card(**kw) -> CardInfo:
    base = dict(
        category="sports", sport="Ice Hockey", league="NHL", year="1990-91", manufacturer="Upper Deck",
        set_name="Upper Deck", subset_insert=None, parallel=None, card_number="1", subject="Wayne Gretzky",
        team="Los Angeles Kings", rookie=False, autograph=False, memorabilia=False, serial_number=None,
        graded=False, grader=None, grade=None, condition="near_mint_or_better", front_orientation="upright",
        confidence="high", uncertain_fields=[], notes="",
    )
    return CardInfo(**{**base, **kw})


def test_title_basic_format():
    assert build_title(card()) == "1990-91 Upper Deck Wayne Gretzky Los Angeles Kings #1"


def test_title_rookie_and_manufacturer():
    t = build_title(card(year="2005-06", manufacturer="Upper Deck", set_name="Young Guns", card_number="201",
                         subject="Sidney Crosby", team="Pittsburgh Penguins", rookie=True))
    assert t == "2005-06 Upper Deck Young Guns Sidney Crosby Pittsburgh Penguins #201 RC"


def test_title_non_sport():
    t = build_title(card(category="non_sport", sport=None, league=None, year="1978", manufacturer=None,
                         set_name="The Muppet Show", card_number="60", subject="Fozzie Bear", team=None))
    assert t == "1978 The Muppet Show Fozzie Bear #60"


def test_title_drops_extras_to_fit_80():
    t = build_title(card(year="2015-16", manufacturer="Upper Deck", set_name="The Cup",
                         subset_insert="Rookie Patch Autographs", parallel="Gold Spectrum",
                         subject="Connor McDavid", team="Edmonton Oilers", card_number="111",
                         rookie=True, autograph=True, memorabilia=True, serial_number="12/99"))
    assert len(t) <= 80
    for must in ("2015-16", "The Cup", "Connor McDavid", "#111", "RC", "AUTO"):
        assert must in t


class FakeClient:
    """Stands in for anthropic.Anthropic; returns canned answers in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.answers.pop(0))


def test_identify_batch_writes_cards_json(tmp_path):
    assert main(["split", str(SAMPLES / "fronts_01.jpg"), str(SAMPLES / "backs_01.jpg"), "-o", str(tmp_path)]) == 0
    before = cv2.imread(str(tmp_path / "card_02_front.jpg"))
    answers = [card(subject=f"Player {i}", card_number=str(i)) for i in range(1, 10)]
    answers[1] = card(subject="Player 2", card_number="2", front_orientation="upside_down")
    client = FakeClient(answers)

    results = identify_batch(tmp_path, client=client, progress=lambda *_: None)

    assert len(results) == 9 and len(client.calls) == 9
    # Each request carries front and back images.
    imgs = [b for b in client.calls[0]["messages"][0]["content"] if b["type"] == "image"]
    assert len(imgs) == 2
    data = json.loads((tmp_path / "cards.json").read_text())
    assert data["cards"][0]["title"].endswith("Player 1 Los Angeles Kings #1")
    # Upside-down card was rotated 180° on disk.
    after = cv2.imread(str(tmp_path / "card_02_front.jpg"))
    assert (cv2.rotate(before, cv2.ROTATE_180).astype(int) - after.astype(int)).__abs__().mean() < 3
