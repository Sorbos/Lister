"""Phase 3: local review page.

`lister review FOLDER` serves a page on http://127.0.0.1 where you check and edit each card,
pick auction or fixed price, set prices, and approve cards for listing. Everything is saved to
FOLDER/review.json as you go, so you can close it and come back.

Uses only the Python standard library; it only listens on your own computer (127.0.0.1).
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape as html_escape
from importlib import resources
from pathlib import Path
from urllib.parse import unquote

from .title import MAX_LEN, build_title

EDITABLE = [
    "category", "sport", "league", "year", "manufacturer", "set_name", "subset_insert", "parallel",
    "card_number", "subject", "team", "rookie", "autograph", "memorabilia", "serial_number",
    "graded", "grader", "grade", "condition",
]
BOOL_FIELDS = {"rookie", "autograph", "memorabilia", "graded"}
AUCTION_DAYS = [1, 3, 5, 7, 10]

DEFAULT_LISTING = {
    "format": "auction",  # "auction" | "fixed_price"
    "price": None,  # fixed price
    "start_price": 0.99,  # auction
    "buy_it_now": None,  # auction, optional
    "reserve": None,  # auction, optional
    "duration_days": 7,  # auction
    "start_time": None,  # UTC ISO string ("2026-11-23T00:05:00Z"); None = start immediately
}
MAX_SCHEDULE_AHEAD = timedelta(days=21)  # eBay allows scheduling up to 3 weeks ahead
DEFAULT_SCHEDULE = {"first_start": None, "interval_minutes": 5}


def _blank_info() -> dict:
    info = {k: None for k in EDITABLE}
    info.update({k: False for k in BOOL_FIELDS})
    info.update(category="sports", condition="near_mint_or_better", subject="")
    return info


def _card_from_identified(c: dict) -> dict:
    """The Claude-provided parts of a review card, from one cards.json entry."""
    ident = c.get("info") or {}
    info = _blank_info()
    info.update({k: ident[k] for k in EDITABLE if k in ident})
    return {
        "info": info,
        "confidence": ident.get("confidence"),
        "uncertain": ident.get("uncertain_fields", []),
        "notes": ident.get("notes") or c.get("error") or "",
        "title": c.get("title") or "",
    }


def initial_state(folder: Path) -> dict:
    """Build review state from cards.json (identified) or manifest.json (images only)."""
    src = folder / "cards.json"
    cards = json.loads(src.read_text())["cards"] if src.exists() else json.loads((folder / "manifest.json").read_text())["cards"]
    out = []
    for c in cards:
        out.append({
            "position": c["position"],
            "front": c["front"],
            "back": c.get("back"),
            **_card_from_identified(c),
            "edited": False,
            "title_custom": False,
            "listing": dict(DEFAULT_LISTING),
            "skip": False,
            "approved": False,
        })
    return {"defaults": dict(DEFAULT_LISTING), "schedule": dict(DEFAULT_SCHEDULE), "cards": out}


def parse_time(v) -> datetime | None:
    """Parse an ISO time; naive times are taken as UTC. Returns None for empty/invalid."""
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stagger(cards: list[dict], first_start, interval_minutes: float) -> None:
    """Give each card that will be listed a start time: first_start, +interval, +2×interval, …

    Cards keep their order on the scanner. Skipped and already-approved cards are left alone.
    """
    first = parse_time(first_start)
    if first is None:
        raise ValueError("Pick when the first card should start")
    step = timedelta(minutes=max(0, float(interval_minutes or 0)))
    n = 0
    for card in sorted(cards, key=lambda c: c["position"]):
        if card["skip"] or card["approved"]:
            continue
        card["listing"]["start_time"] = iso(first + n * step)
        n += 1


def _num(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def validate_card(card: dict, now: datetime | None = None) -> list[str]:
    errs = []
    if card["skip"]:
        return errs
    title = card["title"].strip()
    if not title:
        errs.append("Title is empty")
    elif len(title) > MAX_LEN:
        errs.append(f"Title is {len(title)} characters (max {MAX_LEN})")
    if not (card["info"].get("subject") or "").strip():
        errs.append("Player/subject is empty")
    lst = card["listing"]
    if lst["format"] == "fixed_price":
        if not lst["price"] or lst["price"] <= 0:
            errs.append("Set a price")
    else:
        if not lst["start_price"] or lst["start_price"] <= 0:
            errs.append("Set a starting price")
        if lst["duration_days"] not in AUCTION_DAYS:
            errs.append("Pick an auction length")
        bin_, start = lst["buy_it_now"], lst["start_price"] or 0
        if bin_ and bin_ < round(start * 1.3, 2):
            errs.append("Buy It Now must be at least 30% above the starting price (eBay rule)")
        if lst["reserve"] and lst["reserve"] <= start:
            errs.append("Reserve must be above the starting price")
    start_time = parse_time(lst.get("start_time"))
    if start_time:
        now = now or datetime.now(timezone.utc)
        if start_time <= now:
            errs.append("Start time is in the past")
        elif start_time - now > MAX_SCHEDULE_AHEAD:
            errs.append("eBay allows scheduling at most 3 weeks ahead")
    return errs


def normalise(state: dict) -> dict:
    """Clean up incoming state, recompute automatic titles and validation."""
    for card in state["cards"]:
        info = card["info"]
        for k in EDITABLE:
            v = info.get(k)
            if k in BOOL_FIELDS:
                info[k] = bool(v)
            elif isinstance(v, str):
                info[k] = v.strip() or (None if k != "subject" else "")
        lst = card["listing"]
        for k in ("price", "start_price", "buy_it_now", "reserve"):
            lst[k] = _num(lst.get(k))
        lst["duration_days"] = int(lst.get("duration_days") or 7)
        t = parse_time(lst.get("start_time"))
        lst["start_time"] = iso(t) if t else None
        if lst.get("format") not in ("auction", "fixed_price"):
            lst["format"] = "auction"
        if not card.get("title_custom"):
            card["title"] = build_title(info) if info.get("subject") else ""
        card["errors"] = validate_card(card)
        if card["errors"] or card["skip"]:
            card["approved"] = False
    return state


class BatchState:
    """The review state of one batch folder (review.json), shared by the single and hub servers."""

    def __init__(self, folder: Path):
        self.folder = Path(folder).resolve()
        self.path = self.folder / "review.json"
        self.lock = threading.Lock()
        state = json.loads(self.path.read_text()) if self.path.exists() else initial_state(self.folder)
        state.setdefault("schedule", dict(DEFAULT_SCHEDULE))
        for c in state["cards"]:
            c["listing"].setdefault("start_time", None)
        if not self.path.exists() and (self.folder / "cards.json").exists():
            state["cards_json_mtime"] = (self.folder / "cards.json").stat().st_mtime
        self.state = normalise(state)
        self.merge_identified()
        self._save()
        self.images = {c[k] for c in self.state["cards"] for k in ("front", "back") if c.get(k)}

    def claude_cost(self) -> float | None:
        """What identifying this batch cost (from cards.json), if it was identified."""
        f = self.folder / "cards.json"
        try:
            return json.loads(f.read_text()).get("cost_usd") if f.exists() else None
        except (ValueError, OSError):
            return None

    def merge_identified(self) -> bool:
        """If Claude's results (cards.json) are newer than what the review last took in, fill them into
        every card you haven't edited or approved. Your own edits are never overwritten."""
        f = self.folder / "cards.json"
        try:
            mtime = f.stat().st_mtime
        except FileNotFoundError:
            return False
        if self.state.get("cards_json_mtime") == mtime:
            return False
        try:
            identified = {c["position"]: c for c in json.loads(f.read_text())["cards"]}
        except (ValueError, KeyError, OSError):
            return False  # half-written; try again next time
        for card in self.state["cards"]:
            new = identified.get(card["position"])
            if new is None or card.get("edited") or card.get("approved"):
                continue
            card.update(_card_from_identified(new))
            card["title_custom"] = False
        self.state["cards_json_mtime"] = mtime
        self.state = normalise(self.state)
        return True

    def payload(self) -> dict:
        with self.lock:
            if self.merge_identified():
                self._save()
        return {**self.state, "folder": self.folder.name, "max_len": MAX_LEN, "claude_cost_usd": self.claude_cost()}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def update(self, new_state: dict) -> dict:
        with self.lock:
            # Only accept edits to known cards; image paths can't be changed from the page.
            by_pos = {c["position"]: c for c in self.state["cards"]}
            for incoming in new_state.get("cards", []):
                card = by_pos.get(incoming.get("position"))
                if card is None:
                    continue
                new_info = {k: incoming["info"].get(k) for k in EDITABLE if k in incoming.get("info", {})}
                if any(new_info[k] != card["info"].get(k) for k in new_info) or (
                        incoming.get("title_custom") and incoming.get("title") != card.get("title")):
                    card["edited"] = True  # Claude's later results won't overwrite this card
                card["info"].update(new_info)
                card["listing"].update({k: incoming["listing"].get(k) for k in DEFAULT_LISTING if k in incoming.get("listing", {})})
                for k in ("title", "title_custom", "skip", "approved"):
                    if k in incoming:
                        card[k] = incoming[k]
            if "schedule" in new_state:
                sched = self.state.setdefault("schedule", dict(DEFAULT_SCHEDULE))
                sched.update({k: new_state["schedule"].get(k) for k in DEFAULT_SCHEDULE if k in new_state["schedule"]})
            if new_state.get("apply_schedule"):
                sched = self.state.get("schedule", DEFAULT_SCHEDULE)
                try:
                    stagger(self.state["cards"], sched["first_start"], sched["interval_minutes"])
                    self.state.pop("schedule_error", None)
                except ValueError as e:
                    self.state["schedule_error"] = str(e)
            if "defaults" in new_state:
                self.state["defaults"].update({k: new_state["defaults"].get(k) for k in DEFAULT_LISTING if k in new_state["defaults"]})
            self.state = normalise(self.state)
            self._save()
            return self.state


def batch_status(folder: Path) -> dict:
    """Written by `lister watch`: {"stage": "reading"|"ready"|"error", "message": ...}. No file = ready."""
    f = folder / "status.json"
    try:
        return json.loads(f.read_text()) if f.exists() else {"stage": "ready"}
    except (ValueError, OSError):
        return {"stage": "ready"}


class ReviewServer:
    """Serves one batch at / , or (hub mode) every batch under ROOT/batches at /b/<name>/."""

    def __init__(self, folder: str | Path, port: int = 8765, *, hub: bool = False):
        self.hub = hub
        self.folder = Path(folder).resolve()
        self.batches: dict[str, BatchState] = {}
        self._lock = threading.Lock()
        if not hub:
            self.single = BatchState(self.folder)
            self.path = self.single.path
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())

    # convenience for the single-batch case (and tests)
    @property
    def state(self) -> dict:
        return self.single.state

    def update(self, new_state: dict) -> dict:
        return self.single.update(new_state)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    @property
    def batches_dir(self) -> Path:
        return self.folder / "batches"

    def batch(self, name: str) -> BatchState | None:
        folder = self.batches_dir / name
        if "/" in name or "\\" in name or name.startswith(".") or not (folder / "manifest.json").exists():
            return None
        if batch_status(folder)["stage"] == "reading":
            return None
        with self._lock:
            b = self.batches.get(name)
            if b is None:
                b = self.batches[name] = BatchState(folder)
            return b

    def index_html(self) -> bytes:
        rows = []
        folders = sorted((p for p in self.batches_dir.glob("*") if (p / "manifest.json").exists()),
                         key=lambda p: p.stat().st_mtime, reverse=True) if self.batches_dir.exists() else []
        for f in folders:
            st = batch_status(f)
            n = len(json.loads((f / "manifest.json").read_text()).get("cards", []))
            approved = listed = 0
            if (f / "review.json").exists():
                cards = json.loads((f / "review.json").read_text())["cards"]
                approved = sum(1 for c in cards if c.get("approved"))
            if (f / "ebay.json").exists():
                envs = json.loads((f / "ebay.json").read_text()).get("environments", {})
                listed = sum(1 for e in envs.values() for c in e["cards"].values() if c.get("listing_id"))
            cost = None
            if (f / "cards.json").exists():
                cost = json.loads((f / "cards.json").read_text()).get("cost_usd")
            name = html_escape(f.name)
            note = html_escape(st.get("message") or "")
            if st["stage"] == "reading":
                status, link = f'<span class="chip warn">reading… {note}</span>', name
            elif st["stage"] == "error":
                status, link = f'<span class="chip err">{note or "problem"}</span>', f'<a href="b/{name}/">{name}</a>'
            else:
                bits = [f"{n} cards", f"{approved} approved"] + ([f"{listed} listed"] if listed else [])
                if note:
                    bits.append(note)
                status, link = '<span class="chip">' + " · ".join(bits) + "</span>", f'<a href="b/{name}/">{name}</a>'
            thumb = f'<img src="b/{name}/img/card_01_front.jpg" alt="">' if st["stage"] != "reading" else ""
            money = "" if cost is None else (f"${cost:.2f}" if cost >= 0.995 else f"{cost * 100:.1f}¢")
            rows.append(f"<li>{thumb}<div><b>{link}</b><br>{status} <small>{money}</small></div></li>")
        problems = []
        pdir = self.folder / "problems"
        if pdir.exists():
            for note in sorted(pdir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                problems.append(f"<li><b>{html_escape(note.stem)}</b>: {html_escape(note.read_text().strip())}</li>")
        page = resources.files("lister").joinpath("static/hub.html").read_text()
        page = page.replace("{{ROWS}}", "\n".join(rows) or "<p>No batches yet. Scan into the inbox folder.</p>")
        from . import settings

        page = page.replace("{{INBOX}}", html_escape(str(settings.inbox(self.folder))))
        page = page.replace("{{PROBLEMS}}", ("<h2>Problems</h2><ul class=problems>" + "".join(problems) + "</ul>")
                            if problems else "")
        return page.encode()

    def settings_payload(self) -> dict:
        from . import settings

        cfg = settings.load(self.folder)
        inbox = settings.inbox(self.folder)
        return {"inbox": str(inbox), "using_default": not cfg["inbox"], "exists": inbox.is_dir(),
                "waiting_images": settings.image_count(inbox), "wait_minutes": cfg["wait_minutes"]}

    def update_settings(self, body: dict) -> dict:
        from . import settings

        result, message = "ok", "Saved."
        if "inbox" in body:
            result, folder, message = settings.check_inbox(self.folder, str(body.get("inbox") or ""),
                                                           bool(body.get("confirm")))
            if result == "ok":
                settings.save(self.folder, {"inbox": str(folder) if folder else None})
        if "wait_minutes" in body:
            try:
                minutes = int(body["wait_minutes"])
            except (TypeError, ValueError):
                minutes = 0
            if 1 <= minutes <= 120:
                settings.save(self.folder, {"wait_minutes": minutes})
                message = f"Lone scans now wait {minutes} minute(s) for their back."
            else:
                result, message = "error", "Pick between 1 and 120 minutes."
        return {**self.settings_payload(), "result": result, "message": message}

    def _resolve(self, path: str) -> tuple[BatchState | None, str]:
        """Map a URL path to (batch, sub-path)."""
        path = path.split("?")[0]
        if not self.hub:
            return self.single, path
        if path.startswith("/b/"):
            name, _, rest = path[3:].partition("/")
            return self.batch(unquote(name)), "/" + rest
        return None, path

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the terminal quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def do_GET(self):
                if server.hub and self.path.split("?")[0] in ("/", "/index.html"):
                    return self._send(200, server.index_html(), "text/html; charset=utf-8")
                if server.hub and self.path.split("?")[0] == "/settings":
                    html = resources.files("lister").joinpath("static/settings.html").read_bytes()
                    return self._send(200, html, "text/html; charset=utf-8")
                if server.hub and self.path.split("?")[0] == "/api/settings":
                    return self._json(server.settings_payload())
                if server.hub and self.path.startswith("/b/") and self.path.count("/") == 2:
                    return self._send(301, b"", "text/plain", {"Location": self.path + "/"})
                batch, sub = server._resolve(self.path)
                if batch is None:
                    return self._send(404, b"not found (still being read?)", "text/plain")
                if sub in ("/", "/index.html"):
                    html = resources.files("lister").joinpath("static/review.html").read_bytes()
                    return self._send(200, html, "text/html; charset=utf-8")
                if sub == "/api/state":
                    return self._json({**batch.payload(), "hub": server.hub})
                if sub.startswith("/img/"):
                    name = unquote(sub[len("/img/"):])
                    if name in batch.images:  # only files from the manifest
                        f = batch.folder / name
                        return self._send(200, f.read_bytes(), mimetypes.guess_type(name)[0] or "image/jpeg")
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                # Reject requests from other websites (a page you visit can't edit your drafts or settings).
                origin = self.headers.get("Origin")
                if origin and origin.rstrip("/") + "/" != server.url:
                    return self._send(403, b"forbidden", "text/plain")
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                except (ValueError, json.JSONDecodeError):
                    return self._send(400, b"bad json", "text/plain")
                if server.hub and self.path.split("?")[0] == "/api/settings":
                    return self._json(server.update_settings(body if isinstance(body, dict) else {}))
                batch, sub = server._resolve(self.path)
                if batch is None or sub != "/api/state":
                    return self._send(404, b"not found", "text/plain")
                batch.update(body)
                self._json({**batch.payload(), "hub": server.hub})

        return Handler

    def serve(self, open_browser: bool = True) -> None:
        where = "all batches" if self.hub else f"edits are saved to {self.path}"
        print(f"Review page: {self.url}  (Ctrl+C to stop; {where})")
        if open_browser:
            threading.Timer(0.5, webbrowser.open, [self.url]).start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()
