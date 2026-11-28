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
    "shipping_cost": None,  # None = the stock shipping cost from Settings (or the shipping policy's own)
}
MAX_SCHEDULE_AHEAD = timedelta(days=21)  # eBay allows scheduling up to 3 weeks ahead


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
            "skip": False,
            "approved": False,
        })
    return {"cards": out}


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


def _num(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def validate_details(card: dict) -> list[str]:
    """What a batch card needs before it can be approved (into the inventory)."""
    errs = []
    if card.get("skip"):
        return errs
    title = (card.get("title") or "").strip()
    if not title:
        errs.append("Title is empty")
    elif len(title) > MAX_LEN:
        errs.append(f"Title is {len(title)} characters (max {MAX_LEN})")
    if not (card["info"].get("subject") or "").strip():
        errs.append("Player/subject is empty")
    return errs


def validate_card(card: dict, now: datetime | None = None) -> list[str]:
    """Details plus listing settings: what an inventory card needs before it can be listed."""
    errs = validate_details(card)
    if card.get("skip"):
        return errs
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
    if lst.get("shipping_cost") is not None and lst["shipping_cost"] < 0:
        errs.append("Shipping can't be negative")
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
        card.pop("listing", None)  # listing settings live in the inventory now
        if not card.get("title_custom"):
            card["title"] = build_title(info) if info.get("subject") else ""
        card["errors"] = validate_details(card)
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
        for k in ("defaults", "schedule", "schedule_error"):  # from older versions
            state.pop(k, None)
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
        if self.state.get("finished"):
            return False
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

    @property
    def root(self) -> Path | None:
        """The Lister folder, when this batch lives in ROOT/batches/<name>."""
        return self.folder.parent.parent if self.folder.parent.name == "batches" else None

    def payload(self) -> dict:
        with self.lock:
            if not self.state.get("finished") and self.merge_identified():
                self._save()
        return {**self.state, "folder": self.folder.name, "max_len": MAX_LEN, "claude_cost_usd": self.claude_cost(),
                "has_inventory": self.root is not None}

    def _finish_if_done(self) -> None:
        """Every card approved or skipped: send the approved ones to the inventory; the batch is then read-only."""
        cards = self.state["cards"]
        if self.state.get("finished") or not cards or not all(c.get("approved") or c.get("skip") for c in cards):
            return
        sent = []
        if self.root is not None:
            from . import inventory, settings

            sent = inventory.send_batch(self.folder, cards, self.root, settings.load(self.root).get("listing_defaults"))
        approved = sum(1 for c in cards if c.get("approved") and not c.get("skip"))
        skipped = len(cards) - approved
        self.state["finished"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  "to_inventory": approved, "skipped": skipped, "ids": sent}
        msg = f"{approved} card(s) in inventory" + (f", {skipped} skipped" if skipped else "")
        (self.folder / "status.json").write_text(json.dumps({"stage": "done", "message": msg}))

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def _rotate(self, req: dict) -> None:
        """{"positions": [...], "side": "front"|"back", "degrees": 90|180|270} — turn photos clockwise."""
        from .images import rotate_file

        side = req.get("side") if req.get("side") in ("front", "back") else "front"
        degrees = int(req.get("degrees") or 0) % 360
        wanted = {int(p) for p in req.get("positions") or []}
        for card in self.state["cards"]:
            if card["position"] in wanted and card.get(side) and degrees in (90, 180, 270):
                rotate_file(self.folder / card[side], degrees)
                card["img_v"] = card.get("img_v", 0) + 1

    def update(self, new_state: dict) -> dict:
        with self.lock:
            if self.state.get("finished"):
                return self.state  # read-only once its cards are in the inventory
            if new_state.get("rotate"):
                self._rotate(new_state["rotate"])
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
                for k in ("title", "title_custom", "skip", "approved"):
                    if k in incoming:
                        card[k] = incoming[k]
            self.state = normalise(self.state)
            self._finish_if_done()
            self._save()
            return self.state


def batch_status(folder: Path) -> dict:
    """Written by `lister watch`: {"stage": "reading"|"ready"|"error", "message": ...}. No file = ready."""
    f = folder / "status.json"
    try:
        return json.loads(f.read_text()) if f.exists() else {"stage": "ready"}
    except (ValueError, OSError):
        return {"stage": "ready"}


NAV_PAGES = [("batches", "/", "Batches"), ("inventory", "/inventory", "Inventory"), ("listed", "/listed", "Listed"),
             ("settings", "/settings", "Settings"), ("costs", "/costs", "Costs")]
NAV_CSS = """<style>
.nav { display: flex; align-items: center; gap: 2px; background: var(--panel); border-bottom: 1px solid var(--line);
       padding: 0 12px; overflow-x: auto; }
.nav .brand { font-weight: 700; color: var(--text); padding: 10px 10px 10px 4px; }
.nav a { padding: 10px 12px; color: var(--muted); text-decoration: none; font-weight: 600; font-size: 14px;
         border-bottom: 2px solid transparent; white-space: nowrap; }
.nav a:hover { color: var(--text); }
.nav a.on { color: var(--text); border-bottom-color: var(--accent); }
</style>"""


def nav_html(active: str) -> str:
    links = "".join(
        f'<a href="{href}" class="on">{label}</a>' if key == active else f'<a href="{href}">{label}</a>'
        for key, href, label in NAV_PAGES)
    return f'{NAV_CSS}<nav class="nav"><span class="brand">Lister</span>{links}</nav>'


class ReviewServer:
    """Serves one batch at / , or (hub mode) every batch under ROOT/batches at /b/<name>/."""

    def __init__(self, folder: str | Path, port: int = 8765, *, hub: bool = False):
        self.hub = hub
        self.folder = Path(folder).resolve()
        self.batches: dict[str, BatchState] = {}
        self._lock = threading.Lock()
        self.job: dict | None = None  # the current/last listing run started from the Inventory page
        self._job_lock = threading.Lock()
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
        if batch_status(folder)["stage"] in ("reading", "queued"):
            return None
        with self._lock:
            b = self.batches.get(name)
            if b is None:
                b = self.batches[name] = BatchState(folder)
            return b

    def batch_image(self, url_path: str) -> Path | None:
        """/b/<batch>/img/<file> → the file, only if it's a card picture listed in that batch's manifest."""
        name, _, rest = unquote(url_path.split("?")[0])[3:].partition("/img/")
        folder = self.batches_dir / name
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        try:
            manifest = json.loads((folder / "manifest.json").read_text())
        except (FileNotFoundError, ValueError):
            return None
        allowed = {c[k] for c in manifest.get("cards", []) for k in ("front", "back") if c.get(k)}
        return folder / rest if rest in allowed else None

    def activity_html(self, folders: list[Path]) -> str:
        """What's happening right now: scans waiting, current step, cards being read."""
        import time

        try:
            act = json.loads((self.folder / "activity.json").read_text())
        except (FileNotFoundError, ValueError):
            act = None
        if act is None or time.time() - act.get("time", 0) > 15:
            return ('<div class="activity off">Lister isn&#39;t watching the scans folder right now. '
                    "Start it with <b>Lister Watch</b> (or <code>lister watch</code>) to process new scans.</div>")
        lines = []
        waiting = act.get("waiting", 0)
        lines.append(f"<b>{waiting}</b> scan{'s' if waiting != 1 else ''} waiting in the scans folder" if waiting
                     else "Scans folder is empty. Waiting for new scans.")
        if act.get("now"):
            lines.append(f'<span class="spin"></span>{html_escape(act["now"])}')
        reading = [f for f in folders if batch_status(f)["stage"] == "reading"]
        queued = [f for f in folders if batch_status(f)["stage"] == "queued"]
        if reading or queued:
            done = sum(batch_status(f).get("done", 0) for f in reading)
            total = sum(batch_status(f).get("total", 0) for f in reading)
            bits = [f"Claude is reading {len(reading)} batch{'es' if len(reading) != 1 else ''}"
                    + (f" (card {min(done + 1, total)} of {total})" if total else "")] if reading else []
            if queued:
                bits.append(f"{len(queued)} more batch{'es' if len(queued) != 1 else ''} in line")
            lines.append('<span class="spin"></span>' + " · ".join(bits))
        return '<div class="activity">' + "<br>".join(lines) + "</div>"

    def index_html(self, partial: bool = False) -> bytes:
        rows = []
        # Newest first, in a fixed order (names start with date, time and scan name), so rows don't jump.
        folders = sorted((p for p in self.batches_dir.glob("*") if (p / "manifest.json").exists()),
                         key=lambda p: p.name, reverse=True) if self.batches_dir.exists() else []
        queue = sorted((f for f in folders if batch_status(f)["stage"] == "queued"),
                       key=lambda f: batch_status(f).get("queued_at", 0))
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
            thumb = f'<img src="b/{name}/img/card_01_front.jpg" alt="">'
            if st["stage"] == "reading":
                total = st.get("total") or n
                done = st.get("done", 0)
                pct = round(100 * done / total) if total else 0
                label = f"Reading card {min(done + 1, total)} of {total}" if done < total else "Finishing up"
                if "done" not in st:
                    label, pct = "Cutting out cards", 0
                status = (f'<div class="progress"><div class="bar"><span style="width:{pct}%"></span></div>'
                          f'<small>{label}</small></div>')
                link = name
            elif st["stage"] == "queued":
                pos = queue.index(f) + 1 if f in queue else 1
                nth = {1: "next", 2: "2nd in line", 3: "3rd in line"}.get(pos, f"{pos}th in line")
                status, link = f'<span class="chip warn">{n} cards · waiting to be read ({nth})</span>', name
            elif st["stage"] == "done":
                status = (f'<span class="chip ok">Done · {note}</span> <a class="small" href="/inventory">'
                          "Go to inventory →</a>")
                link = f'<a href="b/{name}/">{name}</a>'
            elif st["stage"] == "error":
                status, link = f'<span class="chip err">{note or "problem"}</span>', f'<a href="b/{name}/">{name}</a>'
            else:
                bits = [f"{n} cards", f"{approved} approved"] + ([f"{listed} listed"] if listed else [])
                if note:
                    bits.append(note)
                status, link = '<span class="chip">' + " · ".join(bits) + "</span>", f'<a href="b/{name}/">{name}</a>'
            money = "" if cost is None else (f"${cost:.2f}" if cost >= 0.995 else f"{cost * 100:.1f}¢")
            rows.append(f'<li>{thumb}<div class="grow"><b>{link}</b><br>{status} <small>{money}</small></div></li>')
        problems = []
        pdir = self.folder / "problems"
        if pdir.exists():
            for note in sorted(pdir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                problems.append(f"<li><b>{html_escape(note.stem)}</b>: {html_escape(note.read_text().strip())}</li>")
        live = (self.activity_html(folders)
                + "<ul>" + ("\n".join(rows) or "<p>No batches yet. Scan into the scans folder.</p>") + "</ul>"
                + (("<h2>Problems</h2><ul class=problems>" + "".join(problems) + "</ul>") if problems else ""))
        if partial:
            return live.encode()
        from . import settings

        page = resources.files("lister").joinpath("static/hub.html").read_text()
        page = page.replace("{{NAV}}", nav_html("batches"))
        page = page.replace("{{INBOX}}", html_escape(str(settings.inbox(self.folder))))
        page = page.replace("{{LIVE}}", live)
        return page.encode()

    # ------------------------------------------------------------ inventory
    def ebay_status(self) -> dict:
        from .config import EbaySettings

        if not EbaySettings.has_keys():
            return {"ready": False, "environment": None,
                    "message": "eBay isn't connected yet (no eBay keys). You can still preview."}
        try:
            from .ebay_api import EbayClient, NotSignedIn

            settings = EbaySettings.load()
            try:
                EbayClient(settings).access_token()
            except NotSignedIn:
                return {"ready": False, "environment": settings.environment,
                        "message": "Not signed in to eBay. Run: lister ebay login"}
            missing = [k for k in ("fulfillment_policy_id", "payment_policy_id", "return_policy_id", "location_key")
                       if not getattr(settings, k)]
            if missing:
                return {"ready": False, "environment": settings.environment,
                        "message": "eBay setup isn't finished. Run: lister ebay setup"}
            return {"ready": True, "environment": settings.environment, "message": ""}
        except (SystemExit, Exception) as e:  # noqa: BLE001 - shown to the user, never fatal
            return {"ready": False, "environment": None, "message": f"eBay problem: {e}"}

    def inventory_payload(self, stage: str = "inventory") -> dict:
        from . import inventory

        cards = []
        for c in inventory.load(self.folder, stage):
            pub = {k: c.get(k) for k in ("id", "title", "title_custom", "listing", "source", "ebay", "added_at",
                                         "sold_at", "description")}
            pub["info"] = {k: c["info"].get(k) for k in ("subject", "year", "set_name", "card_number", "team",
                                                          "condition", "graded", "grader", "grade", "rookie")}
            pub["errors"] = c.get("errors", [])
            pub["relisted"] = len(c.get("listings") or [])
            v = c.get("img_v", 0)
            pub["front"] = f"/inv/{stage}/{c['id']}/front.jpg?v={v}" if c.get("front") else None
            pub["back"] = f"/inv/{stage}/{c['id']}/back.jpg?v={v}" if c.get("back") else None
            cards.append(pub)
        with self._job_lock:
            job = dict(self.job) if self.job else None
        from . import settings

        cfg = settings.load(self.folder)
        stock = {"shipping_cost": (cfg["listing_defaults"] or {}).get("shipping_cost"),
                 "has_description": bool((cfg["stock_description"] or "").strip())}
        return {"stage": stage, "cards": cards, "counts": inventory.counts(self.folder), "max_len": MAX_LEN, "stock": stock,
                "ebay": self.ebay_status() if stage == "inventory" else None, "job": job}

    def inventory_action(self, body: dict) -> dict:
        from . import inventory

        action, ids = body.get("action"), [str(i) for i in body.get("ids") or []]
        stage = "listed" if action in ("sold", "back") else "inventory"
        try:
            if action == "update":
                inventory.update(self.folder, body.get("cards") or [])
            elif action == "bulk":
                inventory.bulk(self.folder, ids, body.get("listing") or {})
            elif action == "stagger":
                inventory.stagger(self.folder, ids, body.get("first_start"), body.get("interval_minutes"))
            elif action == "delete":
                inventory.delete(self.folder, ids)
            elif action == "rotate":
                inventory.rotate(self.folder, ids, body.get("side", "front"), body.get("degrees", 90))
            elif action == "clear_start":
                inventory.clear_start_times(self.folder, ids)
            elif action == "sold":
                inventory.mark_sold(self.folder, ids)
            elif action == "back":
                inventory.back_to_inventory(self.folder, ids)
            elif action == "list":
                self.start_listing(ids, bool(body.get("dry_run")))
            else:
                return {**self.inventory_payload(stage), "error": f"unknown action {action}"}
        except inventory.InventoryError as e:
            return {**self.inventory_payload(stage), "error": str(e)}
        return self.inventory_payload(stage)

    def start_listing(self, ids: list[str], dry_run: bool) -> None:
        from . import inventory

        with self._job_lock:
            if self.job and self.job.get("running"):
                raise inventory.InventoryError("A listing run is already going; wait for it to finish.")
            self.job = {"running": True, "dry_run": dry_run, "done": 0, "total": len(ids), "log": [],
                        "error": None, "finished": None}
        threading.Thread(target=self._run_listing, args=(ids, dry_run), daemon=True).start()

    def _run_listing(self, ids: list[str], dry_run: bool) -> None:
        from .config import EbaySettings
        from .listing import list_cards

        def log(msg):
            with self._job_lock:
                self.job["log"] = (self.job["log"] + [msg])[-200:]

        def on_card(done, total):
            with self._job_lock:
                self.job.update(done=done, total=total)

        try:
            client = None
            if dry_run and not EbaySettings.has_keys():
                settings = EbaySettings.offline()
                log("No eBay keys yet: preview uses placeholder policies.")
            else:
                from .ebay_api import EbayClient, NotSignedIn

                settings = EbaySettings.load()
                client = EbayClient(settings)
                if dry_run:
                    try:
                        client.access_token()
                    except NotSignedIn:
                        client = None
            list_cards(self.folder, ids, settings, client, dry_run=dry_run, progress=log, on_card=on_card)
        except (SystemExit, Exception) as e:  # noqa: BLE001 - reported on the page
            with self._job_lock:
                self.job["error"] = str(e)
        finally:
            with self._job_lock:
                self.job["running"] = False
                self.job["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def costs_html(self) -> bytes:
        from .costs import ledger_path, money, summary

        s = summary()
        tiles = [("Last 7 days", money(s["last_7_days"])), ("This month", money(s["this_month"])),
                 ("All time", money(s["all_time"])),
                 ("Per card", money(s["all_time"] / s["cards"]) if s["cards"] else "–")]
        tiles_html = "".join(f'<div class="tile"><small>{k}</small><b>{v}</b></div>' for k, v in tiles)
        rows = "".join(f"<tr><td>{html_escape(f)}</td><td>{money(v)}</td></tr>"
                       for f, v in reversed(list(s["by_folder"].items())[-30:]))
        models = "".join(f"<tr><td>{html_escape(m)}</td><td>{money(v)}</td></tr>"
                         for m, v in sorted(s["by_model"].items(), key=lambda kv: -kv[1]))
        page = resources.files("lister").joinpath("static/costs.html").read_text()
        for key, val in {"{{NAV}}": nav_html("costs"), "{{TILES}}": tiles_html,
                         "{{CARDS}}": str(s["cards"]), "{{ROWS}}": rows or '<tr><td colspan=2>Nothing yet</td></tr>',
                         "{{MODELS}}": models or '<tr><td colspan=2>Nothing yet</td></tr>',
                         "{{UNPRICED}}": (f"<p>{s['unpriced']} call(s) used a model with no known price and aren't "
                                          "counted.</p>") if s["unpriced"] else "",
                         "{{LOG}}": html_escape(str(ledger_path()))}.items():
            page = page.replace(key, val)
        return page.encode()

    def settings_payload(self) -> dict:
        from . import settings

        cfg = settings.load(self.folder)
        inbox = settings.inbox(self.folder)
        return {"inbox": str(inbox), "using_default": not cfg["inbox"], "exists": inbox.is_dir(),
                "waiting_images": settings.image_count(inbox), "wait_minutes": cfg["wait_minutes"],
                "stock_description": cfg["stock_description"],
                "listing_defaults": {**DEFAULT_LISTING, **(cfg["listing_defaults"] or {})}}

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
        if "stock_description" in body:
            text = str(body["stock_description"] or "")
            if len(text) > 4000:
                result, message = "error", "Keep the stock description under 4000 characters."
            else:
                settings.save(self.folder, {"stock_description": text})
                message = "Stock description saved. It's added to the end of every listing's description."
        if "listing_defaults" in body:
            from .inventory import _clean_listing

            d = _clean_listing({**DEFAULT_LISTING, **{k: v for k, v in (body["listing_defaults"] or {}).items()
                                                      if k in DEFAULT_LISTING}, "start_time": None})
            errs = validate_card({"title": "x", "info": {"subject": "x"}, "listing": d})
            if errs:
                result, message = "error", "; ".join(errs)
            else:
                d.pop("start_time", None)
                settings.save(self.folder, {"listing_defaults": d})
                ship = "the shipping policy's own cost" if d["shipping_cost"] is None else f"${d['shipping_cost']:.2f}"
                message = (f"Saved. New inventory cards start with these settings; stock shipping is {ship} "
                           "for every card without its own shipping cost.")
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
                    partial = "partial=1" in self.path
                    return self._send(200, server.index_html(partial), "text/html; charset=utf-8")
                if server.hub and self.path.split("?")[0] == "/settings":
                    html = resources.files("lister").joinpath("static/settings.html").read_text()
                    return self._send(200, html.replace("{{NAV}}", nav_html("settings")).encode(),
                                      "text/html; charset=utf-8")
                path_only = self.path.split("?")[0]
                if server.hub and path_only in ("/inventory", "/listed"):
                    html = resources.files("lister").joinpath("static/inventory.html").read_text()
                    html = html.replace("{{NAV}}", nav_html(path_only[1:])).replace("{{STAGE}}", path_only[1:])
                    return self._send(200, html.encode(), "text/html; charset=utf-8")
                if server.hub and path_only == "/api/inventory":
                    stage = "listed" if "stage=listed" in self.path else "sold" if "stage=sold" in self.path \
                        else "inventory"
                    return self._json(server.inventory_payload(stage))
                if server.hub and path_only.startswith("/inv/"):
                    from . import inventory

                    parts = unquote(path_only).split("/")  # ['', 'inv', stage, id, file]
                    f = inventory.image(server.folder, *parts[2:5]) if len(parts) == 5 else None
                    if f is not None:
                        return self._send(200, f.read_bytes(), "image/jpeg")
                    return self._send(404, b"not found", "text/plain")
                if server.hub and self.path.split("?")[0] == "/costs":
                    return self._send(200, server.costs_html(), "text/html; charset=utf-8")
                if server.hub and self.path.split("?")[0] == "/api/settings":
                    return self._json(server.settings_payload())
                if server.hub and self.path.startswith("/b/") and self.path.count("/") == 2:
                    return self._send(301, b"", "text/plain", {"Location": self.path + "/"})
                if server.hub and self.path.startswith("/b/") and "/img/" in self.path:
                    # Pictures are shown as soon as cards are cut out, even while Claude is still reading.
                    img = server.batch_image(self.path)
                    if img is not None:
                        return self._send(200, img.read_bytes(), mimetypes.guess_type(img.name)[0] or "image/jpeg")
                    return self._send(404, b"not found", "text/plain")
                batch, sub = server._resolve(self.path)
                if batch is None:
                    return self._send(404, b"not found (still being read?)", "text/plain")
                if sub in ("/", "/index.html"):
                    html = resources.files("lister").joinpath("static/review.html").read_bytes()
                    return self._send(200, html, "text/html; charset=utf-8")
                if sub == "/api/state":
                    return self._json({**batch.payload(), "hub": server.hub, "nav": nav_html("batches") if server.hub else ""})
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
                if server.hub and self.path.split("?")[0] == "/api/inventory":
                    return self._json(server.inventory_action(body if isinstance(body, dict) else {}))
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
