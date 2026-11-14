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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

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
}


def _blank_info() -> dict:
    info = {k: None for k in EDITABLE}
    info.update({k: False for k in BOOL_FIELDS})
    info.update(category="sports", condition="near_mint_or_better", subject="")
    return info


def initial_state(folder: Path) -> dict:
    """Build review state from cards.json (identified) or manifest.json (images only)."""
    src = folder / "cards.json"
    cards = json.loads(src.read_text())["cards"] if src.exists() else json.loads((folder / "manifest.json").read_text())["cards"]
    out = []
    for c in cards:
        ident = c.get("info") or {}
        info = _blank_info()
        info.update({k: ident[k] for k in EDITABLE if k in ident})
        out.append({
            "position": c["position"],
            "front": c["front"],
            "back": c.get("back"),
            "info": info,
            "confidence": ident.get("confidence"),
            "uncertain": ident.get("uncertain_fields", []),
            "notes": ident.get("notes") or c.get("error") or "",
            "title": c.get("title") or "",
            "title_custom": False,
            "listing": dict(DEFAULT_LISTING),
            "skip": False,
            "approved": False,
        })
    return {"defaults": dict(DEFAULT_LISTING), "cards": out}


def _num(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def validate_card(card: dict) -> list[str]:
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
        if lst.get("format") not in ("auction", "fixed_price"):
            lst["format"] = "auction"
        if not card.get("title_custom"):
            card["title"] = build_title(info) if info.get("subject") else ""
        card["errors"] = validate_card(card)
        if card["errors"] or card["skip"]:
            card["approved"] = False
    return state


class ReviewServer:
    def __init__(self, folder: str | Path, port: int = 8765):
        self.folder = Path(folder).resolve()
        self.path = self.folder / "review.json"
        self.lock = threading.Lock()
        state = json.loads(self.path.read_text()) if self.path.exists() else initial_state(self.folder)
        self.state = normalise(state)
        self._save()
        self.images = {c[k] for c in self.state["cards"] for k in ("front", "back") if c.get(k)}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"

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
                card["info"].update({k: incoming["info"].get(k) for k in EDITABLE if k in incoming.get("info", {})})
                card["listing"].update({k: incoming["listing"].get(k) for k in DEFAULT_LISTING if k in incoming.get("listing", {})})
                for k in ("title", "title_custom", "skip", "approved"):
                    if k in incoming:
                        card[k] = incoming[k]
            if "defaults" in new_state:
                self.state["defaults"].update({k: new_state["defaults"].get(k) for k in DEFAULT_LISTING if k in new_state["defaults"]})
            self.state = normalise(self.state)
            self._save()
            return self.state

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the terminal quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    html = resources.files("lister").joinpath("static/review.html").read_bytes()
                    return self._send(200, html, "text/html; charset=utf-8")
                if self.path == "/api/state":
                    return self._json({**server.state, "folder": server.folder.name, "max_len": MAX_LEN})
                if self.path.startswith("/img/"):
                    name = self.path[len("/img/"):].split("?")[0]
                    if name in server.images:  # only files from the manifest
                        f = server.folder / name
                        return self._send(200, f.read_bytes(), mimetypes.guess_type(name)[0] or "image/jpeg")
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path != "/api/state":
                    return self._send(404, b"not found", "text/plain")
                # Reject requests from other websites (a page you visit can't edit your drafts).
                origin = self.headers.get("Origin")
                if origin and origin.rstrip("/") + "/" != server.url:
                    return self._send(403, b"forbidden", "text/plain")
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                except (ValueError, json.JSONDecodeError):
                    return self._send(400, b"bad json", "text/plain")
                self._json({**server.update(body), "folder": server.folder.name, "max_len": MAX_LEN})

        return Handler

    def serve(self, open_browser: bool = True) -> None:
        print(f"Review page: {self.url}  (Ctrl+C to stop; edits are saved to {self.path})")
        if open_browser:
            threading.Timer(0.5, webbrowser.open, [self.url]).start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()
