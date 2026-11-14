import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

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
        answer = self.answers.pop(0)
        usage = SimpleNamespace(input_tokens=2000, output_tokens=1500, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        if answer == "refuse":
            return SimpleNamespace(stop_reason="refusal", parsed_output=None, model=kw["model"], usage=usage)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=answer, model=kw["model"], usage=usage)


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


def test_costs_reported_in_dollars(tmp_path, capsys):
    from lister.costs import cost_of, summary

    # Opus 5.5: 2000 in × $4/M + 1500 out × $20/M = $0.008 + $0.03
    assert cost_of("claude-opus-5-5", {"input_tokens": 2000, "output_tokens": 1500}) == pytest.approx(0.038)
    assert cost_of("claude-haiku-5-5", {"input_tokens": 2000, "output_tokens": 1500}) == pytest.approx(0.00095)
    assert cost_of("some-unknown-model", {"input_tokens": 1}) is None

    assert main(["split", str(SAMPLES / "fronts_01.jpg"), str(SAMPLES / "backs_01.jpg"), "-o", str(tmp_path)]) == 0
    answers = [card(subject=f"P{i}") for i in range(1, 10)]
    answers[4] = "refuse"  # a declined card is still billed
    lines = []
    identify_batch(tmp_path, client=FakeClient(answers), model="claude-opus-5-5", progress=lines.append)

    data = json.loads((tmp_path / "cards.json").read_text())
    assert data["cost_usd"] == pytest.approx(9 * 0.038)
    assert data["cards"][0]["cost_usd"] == pytest.approx(0.038)
    assert data["cards"][4]["error"] and data["cards"][4]["cost_usd"] == pytest.approx(0.038)
    assert any(l.startswith("Claude cost for this batch: 34.2¢ (3.8¢ per card)") for l in lines)
    assert not any("token" in l.lower() for l in lines)  # dollars only

    s = summary()
    assert s["cards"] == 9 and s["all_time"] == pytest.approx(0.342) and s["this_month"] == pytest.approx(0.342)

    assert main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "All time:     34.2¢  (9 cards, 3.8¢ per card)" in out and "token" not in out.lower()


def test_usage_when_nothing_recorded(capsys):
    assert main(["usage"]) == 0
    assert "No Claude calls recorded yet." in capsys.readouterr().out
