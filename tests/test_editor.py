"""Lister Scan Editor: folder of scans → <scan>_scanN.jpg next to each scan."""

import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from lister import orient
from lister.editor_core import Cancelled, apply_turns, find_scans, outputs_of, process_folder
from lister.images import detect_cards, load, recover_from_partner

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "My scans"
    d.mkdir()
    shutil.copy(SAMPLES / "fronts_01.jpg", d / "IMG_20261023_0045.jpg")
    shutil.copy(SAMPLES / "backs_01.jpg", d / "IMG_20261023_0046.jpg")
    return d


def test_names_pairs_and_leaves_scans_alone(folder):
    before = (folder / "IMG_20261023_0045.jpg").read_bytes()
    results = process_folder(folder)
    assert [r.scan.name for r in results] == ["IMG_20261023_0045.jpg", "IMG_20261023_0046.jpg"]
    assert results[0].partner.name == "IMG_20261023_0046.jpg" and results[1].partner.name == "IMG_20261023_0045.jpg"
    names = sorted(p.name for p in folder.iterdir())
    assert "IMG_20261023_0045_scan1.jpg" in names and "IMG_20261023_0046_scan9.jpg" in names
    assert len(names) == 2 + 18
    assert (folder / "IMG_20261023_0045.jpg").read_bytes() == before  # original untouched
    # outputs are never treated as new scans
    assert [p.name for p in find_scans(folder)] == ["IMG_20261023_0045.jpg", "IMG_20261023_0046.jpg"]
    assert [p.name for p in outputs_of(folder / "IMG_20261023_0045.jpg")][:3] == [
        "IMG_20261023_0045_scan1.jpg", "IMG_20261023_0045_scan2.jpg", "IMG_20261023_0045_scan3.jpg"]


def test_skips_done_scans_unless_overwrite(folder):
    process_folder(folder)
    out = folder / "IMG_20261023_0045_scan1.jpg"
    out.write_bytes(b"edited by hand")
    again = process_folder(folder)
    assert all(r.skipped for r in again) and out.read_bytes() == b"edited by hand"
    redo = process_folder(folder, overwrite=True)
    assert not any(r.skipped for r in redo) and out.read_bytes()[:2] == b"\xff\xd8"


def test_overwrite_removes_extra_old_cards(folder):
    process_folder(folder)
    (folder / "IMG_20261023_0045_scan12.jpg").write_bytes((folder / "IMG_20261023_0045_scan1.jpg").read_bytes())
    process_folder(folder, overwrite=True)
    assert not (folder / "IMG_20261023_0045_scan12.jpg").exists()
    assert len(outputs_of(folder / "IMG_20261023_0045.jpg")) == 9


def test_fronts_only_scan_is_fine(tmp_path):
    shutil.copy(SAMPLES / "fronts_01.jpg", tmp_path / "a.jpg")
    [r] = process_folder(tmp_path)
    assert r.partner is None and len(r.outputs) == 9 and not r.error


def test_scan_without_cards_is_reported_not_fatal(folder):
    cv2.imwrite(str(folder / "IMG_20261023_0047.jpg"), np.full((800, 600, 3), (60, 120, 40), np.uint8))
    results = process_folder(folder)
    assert results[2].error and results[0].outputs and results[1].outputs


def test_recovers_a_card_missed_on_one_side():
    """Card found on the back but missed on the front: cut it from the front at the same spot."""
    fi, bi = load(SAMPLES / "fronts_01.jpg"), load(SAMPLES / "backs_01.jpg")
    fronts, backs = detect_cards(fi), detect_cards(bi)
    missing = fronts[4]
    cards, n = recover_from_partner(fi, fronts[:4] + fronts[5:], backs)
    assert n == 1 and len(cards) == 9 and [c.index for c in cards] == list(range(1, 10))
    got = cards[4]
    assert np.linalg.norm(np.array(got.center) - np.array(missing.center)) < 12  # within ~1 mm
    assert abs(got.image.shape[0] - missing.image.shape[0]) < 15


def test_nothing_recovered_where_there_is_no_card():
    fi = load(SAMPLES / "fronts_01.jpg")
    fronts = detect_cards(fi)
    blank = np.full_like(fi, fi[20, 20])  # just the backdrop
    assert recover_from_partner(blank, [], fronts)[1] == 0


def test_ai_turns_and_failure_keeps_edit(folder):
    class FakeOrienter:
        cost_usd = 0.0

        def __call__(self, images):
            return [90] + [0] * (len(images) - 1)

    process_folder(folder, orient=FakeOrienter())
    first = load(folder / "IMG_20261023_0045_scan1.jpg")
    second = load(folder / "IMG_20261023_0045_scan2.jpg")
    assert first.shape[1] > first.shape[0] and second.shape[0] > second.shape[1]  # only the first was turned

    def broken(images):
        raise RuntimeError("no internet")

    msgs = []
    results = process_folder(folder, overwrite=True, orient=broken, progress=lambda m, d, t: msgs.append(m))
    assert all(len(r.outputs) == 9 for r in results)
    assert any("couldn't check orientation" in m for m in msgs)


def test_orienter_request_and_answer():
    calls = []

    def parse(**kw):
        calls.append(kw)
        cards = [orient.CardTop(card=1, top="left"), orient.CardTop(card=2, top="down"), orient.CardTop(card=3, top="up")]
        return SimpleNamespace(stop_reason="end_turn", parsed_output=orient.Orientation(cards=cards),
                               model=kw["model"], usage={"input_tokens": 2000, "output_tokens": 200})

    o = orient.Orienter(SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    imgs = [np.zeros((900, 600, 3), np.uint8)] * 3
    assert o(imgs) == [90, 180, 0]
    req = calls[0]
    assert req["model"] == "claude-haiku-5-5" and req["output_config"] == {"effort": "low"}
    assert sum(1 for b in req["messages"][0]["content"] if b["type"] == "image") == 3
    assert 0 < o.cost_usd < 0.01


def test_apply_turns(folder):
    process_folder(folder)
    p = folder / "IMG_20261023_0045_scan1.jpg"
    h, w = load(p).shape[:2]
    assert apply_turns({p: 90, folder / "IMG_20261023_0045_scan2.jpg": 0}) == 1
    assert load(p).shape[:2] == (w, h)


def test_stop(folder):
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        process_folder(folder, cancel=stop)


def test_self_test_mode(folder):
    from lister.editor import main

    assert main(["--self-test", str(folder)]) == 0
    assert (folder / "self-test.txt").read_text(encoding="utf-8").strip().endswith("OK")


def test_api_key_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert orient.api_key() is None and orient.make_orienter() is None
    orient.save_settings({"anthropic_api_key": "sk-ant-x"})
    assert orient.api_key() == "sk-ant-x"
    assert (tmp_path / "Lister" / "scan-editor.json").exists()
