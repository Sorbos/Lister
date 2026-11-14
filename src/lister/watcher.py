"""`lister watch`: turn scans that land in ROOT/inbox into batches automatically.

ROOT/inbox/     scanner saves here
ROOT/batches/   one folder per batch (cut-out cards, cards.json, review.json)
ROOT/done/      original scans, moved here once their batch is made
ROOT/problems/  scans Lister couldn't use, each with a .txt note saying why

Scans are taken in file-name order (IMG_..._0045 before _0046). A scan is used once its size
has stopped changing. Each scan is paired with the next one if the cards line up and Claude
agrees they are fronts and backs of the same cards; otherwise it becomes a fronts-only batch.
A lone last scan waits (default 10 minutes) for a partner before going ahead on its own.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .images import Card, DetectionError, detect_cards, load, write_batch
from .pairing import check_sheets, shapes_match

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def natural_key(p: Path):
    """IMG_2 before IMG_10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


@dataclass
class Seen:
    size: int
    mtime: float
    stable_since: float


class Watcher:
    def __init__(self, root: str | Path, *, client=None, model: str | None = None, auto_identify: bool = True,
                 pair_timeout: float = 600, settle: float = 3.0, clock=time.time, progress=print,
                 identify=None, background: bool = True):
        from .identify import DEFAULT_MODEL, identify_batch

        self.root = Path(root)
        self.inbox, self.batches = self.root / "inbox", self.root / "batches"
        self.done, self.problems = self.root / "done", self.root / "problems"
        for d in (self.inbox, self.batches, self.done, self.problems):
            d.mkdir(parents=True, exist_ok=True)
        self.client, self.model = client, model or DEFAULT_MODEL
        self.auto_identify, self.pair_timeout, self.settle = auto_identify, pair_timeout, settle
        self.clock, self.progress = clock, progress
        self.identify = identify or identify_batch
        self.seen: dict[Path, Seen] = {}
        self.cards: dict[Path, list[Card]] = {}
        self.pool = ThreadPoolExecutor(max_workers=1) if background else None
        self.jobs = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------ files
    def _scan_inbox(self) -> tuple[list[Path], bool]:
        """Return (ready files in order, whether any file is still being written)."""
        now = self.clock()
        files = [p for p in self.inbox.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT]
        for p in list(self.seen):
            if p not in files:
                self.seen.pop(p, None)
                self.cards.pop(p, None)
        ready, busy = [], False
        for p in files:
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            s = self.seen.get(p)
            if s is None or (s.size, s.mtime) != (st.st_size, st.st_mtime):
                self.seen[p] = Seen(st.st_size, st.st_mtime, now)
                busy = True
            elif now - s.stable_since >= self.settle and st.st_size > 0:
                ready.append(p)
            else:
                busy = True
        return sorted(ready, key=natural_key), busy

    def _cards(self, p: Path) -> list[Card]:
        if p not in self.cards:
            cards = detect_cards(load(p), expected=None)
            if not cards:
                raise DetectionError("No cards found in this scan")
            self.cards[p] = cards
        return self.cards[p]

    def _problem(self, p: Path, why: str) -> None:
        dest = _unique(self.problems / p.name)
        shutil.move(str(p), dest)
        dest.with_suffix(".txt").write_text(why + "\n")
        self.seen.pop(p, None)
        self.cards.pop(p, None)
        self.progress(f"Problem with {p.name}: {why} (moved to {self.problems})")

    # ------------------------------------------------------------ decisions
    def decide(self, a: Path, b: Path) -> tuple[str, str]:
        """Return (decision, note): decision is 'pair', 'swap' (b is fronts), 'alone' or 'a_is_backs'."""
        ok, why = shapes_match(self._cards(a), self._cards(b))
        if not ok:
            return "alone", why
        if self.client is None:
            return "pair", "cards line up (front/back not double-checked)"
        try:
            from .costs import record

            check, served, usd = check_sheets(load(a), load(b), client=self.client, model=self.model)
            record(served, usd, "pairing", 0)
        except Exception as e:  # network/API trouble: trust the shapes and the order
            return "pair", f"cards line up; Claude check failed ({type(e).__name__}), please confirm front/back"
        if check is None:
            return "pair", "cards line up; Claude check gave no answer, please confirm front/back"
        if not check.same_cards:
            return ("a_is_backs" if check.first == "backs" else "alone"), check.reason
        if check.first == "backs" and check.second == "fronts":
            return "swap", "backs were scanned first; swapped"
        if check.first == "fronts" and check.second == "backs":
            return "pair", ""
        return "pair", f"cards line up but front/back unclear ({check.reason}); please confirm"

    # ------------------------------------------------------------ main step
    def poll(self) -> None:
        """Look at the inbox once and make whatever batches are ready."""
        ready, busy = self._scan_inbox()
        while ready:
            a = ready[0]
            try:
                self._cards(a)
            except Exception as e:
                self._problem(a, f"Couldn't find cards: {e}")
                ready.pop(0)
                continue
            if len(ready) >= 2:
                b = ready[1]
                try:
                    self._cards(b)
                except Exception as e:
                    self._problem(b, f"Couldn't find cards: {e}")
                    ready.pop(1)
                    continue
                decision, note = self.decide(a, b)
                if decision == "pair":
                    self.make_batch(a, b, note)
                    del ready[:2]
                elif decision == "swap":
                    self.make_batch(b, a, note)
                    del ready[:2]
                elif decision == "a_is_backs":
                    self._problem(a, f"Looks like backs with no matching front scan ({note})")
                    ready.pop(0)
                else:
                    self.make_batch(a, None, f"fronts only ({note})")
                    ready.pop(0)
            else:
                waited = self.clock() - self.seen[a].stable_since
                if not busy and waited >= self.pair_timeout:
                    self.make_batch(a, None, "fronts only (no back scan arrived)")
                    ready.pop(0)
                break

    def make_batch(self, front: Path, back: Path | None, note: str = "") -> Path:
        name = f"{datetime.fromtimestamp(self.clock()):%Y-%m-%d_%H%M}_{front.stem}"
        folder = _unique(self.batches / name)
        fronts, backs = self._cards(front), (self._cards(back) if back else None)
        _status(folder, "reading", "cutting out cards")
        write_batch(fronts, backs, folder, {"fronts": front.name, "backs": back.name if back else None})
        keep = _unique(self.done / folder.name)
        keep.mkdir(parents=True)
        for p in (front, back):
            if p:
                shutil.move(str(p), keep / p.name)
                self.seen.pop(p, None)
                self.cards.pop(p, None)
        kind = "fronts + backs" if back else "fronts only"
        self.progress(f"New batch {folder.name}: {len(fronts)} cards, {kind}" + (f" — {note}" if note else ""))
        if self.auto_identify:
            _status(folder, "reading", "reading cards with Claude")
            job = (self.pool.submit if self.pool else _run_now)(self._identify, folder, note)
            self.jobs.append(job)
        else:
            _status(folder, "ready", note)
        return folder

    def _identify(self, folder: Path, note: str) -> None:
        try:
            kw = {"client": self.client} if self.client is not None else {}
            results = self.identify(folder, model=self.model, progress=lambda m: None, **kw)
            failed = sum(1 for r in results if r.get("error"))
            msg = note + (f"; {failed} card(s) couldn't be read" if failed else "")
            _status(folder, "ready", msg.strip("; "))
            self.progress(f"Batch {folder.name}: cards read, ready to review")
        except Exception as e:  # e.g. no API key, no internet: batch is still reviewable by hand
            _status(folder, "error", f"couldn't read cards ({e}); fill in by hand or try again")
            self.progress(f"Batch {folder.name}: couldn't read cards: {e}")

    def run(self, interval: float = 2.0, stop: threading.Event | None = None) -> None:
        self.progress(f"Watching {self.inbox} (Ctrl+C to stop)")
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.poll()
            except Exception as e:  # keep watching whatever happens to one scan
                self.progress(f"error: {e}")
            stop.wait(interval)


def _run_now(fn, *args):
    fn(*args)


def _status(folder: Path, stage: str, message: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "status.json").write_text(json.dumps({"stage": stage, "message": message}))


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"too many files named like {path}")
