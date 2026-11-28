"""Lister Scan Editor: turn a folder of multi-card scans into one image per card.

For every scan in the folder (e.g. IMG_20261122_0067.jpg) it finds each card, straightens it,
crops it with a thin margin so all four corners show, and saves IMG_20261122_0067_scan1.jpg,
_scan2.jpg … next to the scan, numbered row by row, left to right. The original scans are never
changed.

Consecutive scans that are the front and back of the same cards (each card flipped where it lay)
are recognised by their layout; a card one of them missed is then cut out at the spot where the
other scan has it. So _scan3 of the front scan and _scan3 of the back scan are the same card.

A scan that already has _scan files is skipped unless overwrite is on. Optionally each card is
turned upright by an AI check (see orient.py); without it, cards keep the way they lay.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .images import (Card, DetectionError, backdrop_colour, detect_cards, fill_grid, load, match_cards,
                     outline_preview, recover_from_partner, rotate_file, save)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
OUTPUT_RE = re.compile(r"_scan\d+$", re.IGNORECASE)

# orient(images) -> degrees to turn each clockwise (0/90/180/270), or None if unavailable
Orienter = Callable[[list[np.ndarray]], "list[int] | None"]


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def is_output(p: Path) -> bool:
    return bool(OUTPUT_RE.search(p.stem))


def find_scans(folder: Path) -> list[Path]:
    """Scan images in the folder (not our own _scanN outputs), in file-name order."""
    return sorted((p for p in Path(folder).iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXT and not is_output(p)), key=natural_key)


def outputs_of(scan: Path) -> list[Path]:
    """The _scanN images already made from this scan, in number order."""
    pat = re.compile(re.escape(scan.stem) + r"_scan(\d+)$", re.IGNORECASE)
    found = [(int(m.group(1)), p) for p in scan.parent.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXT and (m := pat.match(p.stem))]
    return [p for _, p in sorted(found)]


@dataclass
class ScanResult:
    scan: Path
    outputs: list[Path] = field(default_factory=list)
    skipped: bool = False
    partner: Path | None = None
    recovered: int = 0  # cut out at the spot where the other side's scan has them
    filled: int = 0  # found in an empty slot of the card grid
    turned: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    preview: object = None  # small copy of the scan with every card outlined (numpy image), this run only

    def summary(self) -> str:
        if self.error:
            return f"{self.scan.name}: {self.error}"
        if self.skipped:
            return f"{self.scan.name}: already done ({len(self.outputs)} cards), skipped"
        bits = [f"{len(self.outputs)} card{'s' if len(self.outputs) != 1 else ''}"]
        if self.partner:
            bits.append(f"paired with {self.partner.name}")
        if self.recovered:
            bits.append(f"{self.recovered} found using {self.partner.name if self.partner else 'its partner'}")
        if self.filled:
            bits.append(f"{self.filled} found in a gap in the grid")
        if self.turned:
            bits.append(f"{self.turned} turned upright")
        text = f"{self.scan.name}: " + ", ".join(bits)
        return text + "".join(f"\n   Check: {w}" for w in self.warnings)


def layouts_match(a: list[Card], b: list[Card]) -> bool:
    """Do these two scans look like the front and back of the same cards (same spots, same sizes)?"""
    if not a or not b:
        return False
    matched, _ = match_cards(a, b)
    return len(matched) >= max(1, round(0.75 * min(len(a), len(b))))


class Cancelled(Exception):
    pass


def process_folder(folder: str | Path, *, overwrite: bool = False, orient: Orienter | None = None,
                   progress: Callable[[str, int, int], None] = lambda msg, done, total: None,
                   cancel: threading.Event | None = None, margin_frac: float = 0.03) -> list[ScanResult]:
    """Edit every scan in the folder. progress(message, scans done, total) is called as it goes."""
    folder = Path(folder)
    scans = find_scans(folder)
    total = len(scans)
    results: dict[Path, ScanResult] = {s: ScanResult(s) for s in scans}
    cache: dict[Path, tuple[np.ndarray, list[Card]] | None] = {}

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    def detect(scan: Path):
        if scan not in cache:
            try:
                img = load(scan)
                cache[scan] = (img, detect_cards(img, expected=None, margin_frac=margin_frac))
            except (DetectionError, FileNotFoundError, OSError) as e:
                results[scan].error = f"couldn't read cards ({e})"
                cache[scan] = None
        return cache[scan]

    def todo(scan: Path) -> bool:
        return overwrite or not outputs_of(scan)

    done = 0

    def handle(scan: Path, other: Path | None) -> None:
        nonlocal done
        check()
        r = results[scan]
        r.partner = other
        if not todo(scan):
            r.skipped, r.outputs = True, outputs_of(scan)
        else:
            progress(f"Editing {scan.name}…", done, total)
            d = detect(scan)
            if d is not None:
                _edit(scan, d, cache.get(other) if other else None, r)
        done += 1
        progress(r.summary(), done, total)

    def _edit(scan: Path, d, other_d, r: ScanResult) -> None:
        img, cards = d
        if other_d:
            cards, r.recovered = recover_from_partner(img, cards, other_d[1], margin_frac)
        cards, r.filled, unclear = fill_grid(img, cards, margin_frac)
        lightness, colourful = backdrop_colour(img)
        if colourful < 10 and lightness > 30:
            r.warnings.append("light grey/white background: cards are found much more reliably on a coloured "
                              "sheet (green, blue or black). Check every card was cut out.")
        if unclear:
            r.warnings.append(f"{unclear} spot{'s' if unclear != 1 else ''} in the card grid had something in it that "
                              "couldn't be cut out as a card. Check the scan.")
        if not cards:
            r.error = "no cards found"
            r.preview = outline_preview(img, [])
            return
        r.preview = outline_preview(img, cards)
        images = [c.image for c in cards]
        turns = None
        if orient is not None:
            check()
            progress(f"Turning {scan.name}'s cards upright…", done, total)
            try:
                turns = orient(images)
            except Exception as e:  # the AI check is optional: never lose the edit over it
                progress(f"{scan.name}: couldn't check orientation ({e}); cards kept as scanned", done, total)
        for old in outputs_of(scan):  # overwrite: clear the previous set first
            old.unlink()
        for n, image in enumerate(images, start=1):
            out = scan.with_name(f"{scan.stem}_scan{n}.jpg")
            save(image, out)
            r.outputs.append(out)
            if turns and n - 1 < len(turns) and turns[n - 1] % 360:
                rotate_file(out, turns[n - 1])
                r.turned += 1

    # Walk the scans in order; a scan and the next one are a pair if their layouts match (front + back).
    # Only the current pair is kept in memory.
    i = 0
    while i < len(scans):
        a = scans[i]
        b = scans[i + 1] if i + 1 < len(scans) else None
        if b is not None and (todo(a) or todo(b)):
            check()
            progress(f"Looking at {a.name} and {b.name}…", done, total)
            da, db = detect(a), detect(b)
            if da and db and layouts_match(da[1], db[1]):
                handle(a, b)
                handle(b, a)
                ra, rb = results[a], results[b]
                if not (ra.skipped or rb.skipped or ra.error or rb.error) and len(ra.outputs) != len(rb.outputs):
                    msg = (f"{a.name} has {len(ra.outputs)} cards but its other side {b.name} has "
                           f"{len(rb.outputs)}: a card may be missing. Check both scans.")
                    ra.warnings.append(msg)
                    rb.warnings.append(msg)
                    progress(f"Check: {msg}", done, total)
                cache.pop(a, None)
                cache.pop(b, None)
                i += 2
                continue
        handle(a, None)
        cache.pop(a, None)
        i += 1
    return [results[s] for s in scans]


def apply_turns(turns: dict[Path, int]) -> int:
    """Turn finished images clockwise by 90/180/270 (from the review grid). Returns how many changed."""
    n = 0
    for path, degrees in turns.items():
        if degrees % 360:
            rotate_file(path, degrees % 360)
            n += 1
    return n
