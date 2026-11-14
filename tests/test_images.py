from pathlib import Path

import cv2
import numpy as np
import pytest

from lister.cli import main
from lister.images import DetectionError, detect_cards, load, pair_backs

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture(scope="module")
def fronts():
    return detect_cards(load(SAMPLES / "fronts_01.jpg"))


@pytest.fixture(scope="module")
def backs():
    return detect_cards(load(SAMPLES / "backs_01.jpg"))


def test_finds_nine_upright_cards(fronts, backs):
    for cards in (fronts, backs):
        assert len(cards) == 9
        for c in cards:
            h, w = c.image.shape[:2]
            assert h > w  # portrait cards stay portrait
            assert abs(c.angle) < 5


def test_reading_order(fronts):
    centers = [c.center for c in fronts]
    for row in range(3):
        xs = [centers[row * 3 + i][0] for i in range(3)]
        assert xs == sorted(xs)
    assert centers[0][1] < centers[3][1] < centers[6][1]


def test_pairs_same_position(fronts, backs):
    pairs = pair_backs(fronts, backs, "same")
    assert [(f.index, b.index) for f, b in pairs] == [(i, i) for i in range(1, 10)]


def test_crop_keeps_background_margin(fronts):
    # Corners of the crop should be background (green), not card.
    img = fronts[0].image
    for y, x in [(2, 2), (2, -3), (-3, 2), (-3, -3)]:
        b, g, r = img[y, x].astype(int)
        assert g > r and g > b


def test_wrong_count_raises():
    with pytest.raises(DetectionError):
        detect_cards(load(SAMPLES / "fronts_01.jpg"), expected=8)


def test_synthetic_rotated_card():
    canvas = np.full((1200, 1000, 3), (40, 120, 40), np.uint8)
    box = cv2.boxPoints(((500, 600), (300, 450), 7)).astype(np.int32)
    cv2.fillPoly(canvas, [box], (240, 240, 240))
    (card,) = detect_cards(canvas, expected=1)
    assert abs(abs(card.angle) - 7) < 1


def test_cli_split(tmp_path):
    assert main(["split", str(SAMPLES / "fronts_01.jpg"), str(SAMPLES / "backs_01.jpg"), "-o", str(tmp_path)]) == 0
    assert len(list(tmp_path.glob("card_*_front.jpg"))) == 9
    assert len(list(tmp_path.glob("card_*_back.jpg"))) == 9
    assert (tmp_path / "manifest.json").exists()
