"""Settings you change from the browser (Settings page), stored in ROOT/settings.json.

- inbox: the folder Lister watches for scans (default ROOT/inbox)
- wait_minutes: how long a lone scan waits for its back scan
- listing_defaults: format/prices/length new inventory cards start with (None = auction, $0.99, 7 days),
  plus shipping_cost: the stock shipping cost for every card that doesn't set its own
- stock_description: text at the end of every listing's description
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

STOCK_DESCRIPTION = ("The photos are scans of the actual card you will receive. "
                     "Please look at them closely for corners, edges and surface.")
DEFAULTS = {"inbox": None, "wait_minutes": 10, "listing_defaults": None, "stock_description": STOCK_DESCRIPTION}
BUSY_FOLDER = 20  # this many images already in a folder → ask before watching it
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def path(root: Path) -> Path:
    return Path(root) / "settings.json"


def load(root: Path) -> dict:
    try:
        data = json.loads(path(root).read_text())
    except (FileNotFoundError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(root: Path, values: dict) -> dict:
    data = {**load(root), **{k: v for k, v in values.items() if k in DEFAULTS}}
    p = path(root)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)
    return data


def inbox(root: Path) -> Path:
    custom = load(root)["inbox"]
    return Path(custom) if custom else Path(root) / "inbox"


def to_local_path(text: str) -> Path:
    """Accept Windows paths everywhere; under WSL, C:\\x becomes /mnt/c/x."""
    text = text.strip().strip('"').strip("'")
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if m and os.name != "nt" and Path(f"/mnt/{m.group(1).lower()}").is_dir():
        rest = m.group(2).replace("\\", "/")
        return Path(f"/mnt/{m.group(1).lower()}") / rest
    return Path(os.path.expanduser(text))


def image_count(folder: Path) -> int:
    try:
        return sum(1 for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXT)
    except OSError:
        return 0


def _roots() -> list[dict]:
    """Quick links for the folder browser: drives, home, Pictures."""
    out = []
    if os.name == "nt":
        import string

        out += [{"name": f"{d}:", "path": f"{d}:\\"} for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    else:
        out += [{"name": f"{d.name.upper()}: (Windows)", "path": str(d)}
                for d in sorted(Path("/mnt").glob("?")) if d.is_dir()]
        out.append({"name": "/", "path": "/"})
    home = Path.home()
    out.append({"name": "Home", "path": str(home)})
    for sub in ("Pictures", "Documents", "Desktop"):
        if (home / sub).is_dir():
            out.append({"name": sub, "path": str(home / sub)})
    return out


def list_folders(text: str | None, root: Path) -> dict:
    """What the folder browser shows for one folder: its sub-folders, parent and scan count."""
    folder = to_local_path(text) if text and text.strip() else inbox(root)
    if not folder.is_dir():
        folder = folder.parent if folder.parent.is_dir() else Path.home()
    folder = folder.resolve()
    subs = []
    try:
        for p in folder.iterdir():
            try:
                if p.is_dir() and not p.name.startswith((".", "$")) and p.name not in ("System Volume Information",):
                    subs.append({"name": p.name, "path": str(p)})
            except OSError:
                continue
    except OSError as e:
        return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else None,
                "folders": [], "images": 0, "roots": _roots(), "error": f"Can't open this folder ({e.strerror})"}
    subs.sort(key=lambda f: f["name"].lower())
    return {"path": str(folder), "parent": str(folder.parent) if folder.parent != folder else None,
            "folders": subs[:1000], "images": image_count(folder), "roots": _roots()}


def make_folder(parent_text: str, name: str) -> Path:
    """Create a new folder inside parent for the folder browser's New folder button."""
    name = (name or "").strip()
    if not name or any(c in name for c in '\\/:*?"<>|') or name in (".", ".."):
        raise ValueError("Use a folder name without \\ / : * ? \" < > |")
    parent = to_local_path(parent_text)
    if not parent.is_dir():
        raise ValueError("That folder doesn't exist any more")
    new = parent / name
    if new.exists():
        raise ValueError(f"There's already a folder called {name} here")
    new.mkdir()
    return new


def check_inbox(root: Path, text: str, confirmed: bool = False) -> tuple[str, Path | None, str]:
    """Validate a new scans folder. Returns (result, folder, message); result is ok|confirm|error."""
    if not text.strip():
        return "ok", None, "Using the default folder."
    folder = to_local_path(text)
    if not folder.is_absolute():
        return "error", None, "Enter the full path, e.g. C:\\Users\\you\\Pictures\\Scans"
    if not folder.is_dir():
        return "error", None, f"That folder doesn't exist: {text.strip()}"
    folder = folder.resolve()
    root = Path(root).resolve()
    risky = {Path(folder.anchor), Path.home().resolve(), *(Path(f"/mnt/{d}") for d in "cdefgh")}
    if folder in risky or folder.parent == Path("/mnt"):
        return "error", None, "That's a whole drive or your home folder; pick a folder just for scans."
    for name in ("batches", "done", "problems"):
        inner = root / name
        if folder == inner or inner in folder.parents:
            return "error", None, f"That's inside Lister's own {name} folder; pick a different one."
    if folder == root:
        return "error", None, "That's the Lister folder itself; use a folder inside it, like inbox."
    n = image_count(folder)
    if n >= BUSY_FOLDER and not confirmed:
        return "confirm", folder, (f"This folder already has {n} images. Lister will move every image in it "
                                   "into its own folders as it processes them. Use it anyway?")
    return "ok", folder, f"Watching {folder}" + (f" ({n} image(s) waiting)" if n else "")
