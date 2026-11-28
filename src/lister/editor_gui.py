"""Lister Scan Editor: the desktop window.

Choose a folder → Edit scans → each card saved as <scan>_scanN.jpg in the same folder → review
the cards as thumbnails, turn any that are wrong (one at a time or several selected), Save turns.
"""

from __future__ import annotations

import base64
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np

from . import orient
from .editor_core import Cancelled, apply_turns, find_scans, outputs_of, process_folder
from .images import load

APP = "Lister Scan Editor"
THUMB = 130  # thumbnail width in px


def _photo(img: np.ndarray, turns: int = 0) -> tk.PhotoImage:
    """numpy BGR image → Tk image (via PNG, so no extra libraries are needed)."""
    h, w = img.shape[:2]
    small = cv2.resize(img, (THUMB, max(1, round(THUMB * h / w))), interpolation=cv2.INTER_AREA)
    for _ in range(turns % 4):
        small = cv2.rotate(small, cv2.ROTATE_90_CLOCKWISE)
    ok, buf = cv2.imencode(".png", small)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode())


class CardTile:
    def __init__(self, app: "EditorApp", parent, path: Path):
        self.app, self.path, self.turns = app, path, 0
        self.selected = tk.BooleanVar(value=False)
        self.img = load(path)
        self.frame = ttk.Frame(parent, padding=4, relief="groove")
        self.photo = _photo(self.img)
        self.label = ttk.Label(self.frame, image=self.photo)
        self.label.grid(row=0, column=0, columnspan=3)
        self.label.bind("<Button-1>", lambda e: self.selected.set(not self.selected.get()))
        ttk.Checkbutton(self.frame, variable=self.selected).grid(row=1, column=0, sticky="w")
        ttk.Button(self.frame, text="Left", width=5, command=lambda: self.turn(-1)).grid(row=1, column=1)
        ttk.Button(self.frame, text="Right", width=5, command=lambda: self.turn(1)).grid(row=1, column=2)
        self.name = ttk.Label(self.frame, text=path.stem.rsplit("_", 1)[-1], foreground="#666")
        self.name.grid(row=2, column=0, columnspan=3)

    def turn(self, quarter: int) -> None:
        self.turns = (self.turns + quarter) % 4
        self.photo = _photo(self.img, self.turns)
        self.label.configure(image=self.photo)
        self.name.configure(foreground="#b45309" if self.turns else "#666",
                            text=self.path.stem.rsplit("_", 1)[-1] + (" (turned)" if self.turns else ""))
        self.app.update_pending()


class EditorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP)
        root.geometry("1000x760")
        root.minsize(640, 480)
        self.settings = orient.load_settings()
        self.folder = tk.StringVar(value=self.settings.get("last_folder", ""))
        self.overwrite = tk.BooleanVar(value=False)
        self.use_ai = tk.BooleanVar(value=bool(orient.api_key()) and self.settings.get("use_ai", True))
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.tiles: list[CardTile] = []
        self._build()
        self._refresh_ai()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)
        if self.folder.get() and Path(self.folder.get()).is_dir():
            root.after(200, self.load_review)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Folder with scans:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.folder, state="readonly").grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Choose folder…", command=self.choose_folder).grid(row=0, column=2)
        top.columnconfigure(1, weight=1)

        opts = ttk.Frame(top)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Checkbutton(opts, text="Redo scans that are already done (overwrite their cards)",
                        variable=self.overwrite).pack(side="left")
        self.ai_check = ttk.Checkbutton(opts, text="Turn cards upright with AI", variable=self.use_ai,
                                        command=self._remember_ai)
        self.ai_check.pack(side="left", padx=(16, 4))
        ttk.Button(opts, text="AI key…", command=self.set_key).pack(side="left")

        run = ttk.Frame(top)
        run.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.run_btn = ttk.Button(run, text="Edit scans", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(run, text="Stop", command=self.cancel.set, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(run, text="Open folder", command=self.open_folder).pack(side="left")
        self.bar = ttk.Progressbar(run, mode="determinate")
        self.bar.pack(side="left", fill="x", expand=True, padx=10)
        self.status = ttk.Label(top, text="Choose the folder your scans are in.")
        self.status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        self.log = tk.Text(self.root, height=6, wrap="word", state="disabled", relief="flat",
                           background=self.root.cget("background"))
        self.log.pack(fill="x", padx=10)

        # review grid
        bar = ttk.Frame(self.root, padding=(10, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Cards", font=("", 11, "bold")).pack(side="left")
        ttk.Button(bar, text="Select all", command=lambda: self._select(True)).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="Select none", command=lambda: self._select(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="Turn selected left", command=lambda: self._turn_selected(-1)).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="Turn selected right", command=lambda: self._turn_selected(1)).pack(side="left", padx=2)
        self.save_btn = ttk.Button(bar, text="Save turns", command=self.save_turns, state="disabled")
        self.save_btn.pack(side="right")

        holder = ttk.Frame(self.root)
        holder.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.canvas = tk.Canvas(holder, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.grid.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))

    # ------------------------------------------------------------ actions
    def say(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def choose_folder(self) -> None:
        start = self.folder.get() or str(Path.home())
        chosen = filedialog.askdirectory(title="Choose the folder with your scans", initialdir=start, mustexist=True)
        if chosen:
            self.folder.set(chosen)
            orient.save_settings({"last_folder": chosen})
            n = len(find_scans(Path(chosen)))
            self.status.configure(text=f"{n} scan{'s' if n != 1 else ''} in this folder.")
            self.load_review()

    def _refresh_ai(self) -> None:
        has_key = bool(orient.api_key())
        self.ai_check.configure(state="normal" if has_key else "disabled",
                                text="Turn cards upright with AI" + ("" if has_key else " (add an AI key first)"))
        if not has_key:
            self.use_ai.set(False)

    def _remember_ai(self) -> None:
        orient.save_settings({"use_ai": bool(self.use_ai.get())})

    def set_key(self) -> None:
        key = simpledialog.askstring(
            "AI key", "Anthropic API key (starts with sk-ant-).\nLeave empty to remove it.\n\n"
                      "It's saved on this computer only, in your user profile.", show="•", parent=self.root)
        if key is None:
            return
        orient.save_settings({"anthropic_api_key": key.strip() or None})
        self.use_ai.set(bool(key.strip()))
        self._refresh_ai()

    def open_folder(self) -> None:
        f = self.folder.get()
        if not f:
            return
        if sys.platform.startswith("win"):
            os.startfile(f)  # noqa: S606 - opens Explorer
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", f])

    def start(self) -> None:
        folder = self.folder.get()
        if not folder or not Path(folder).is_dir():
            return messagebox.showinfo(APP, "Choose the folder with your scans first.", parent=self.root)
        if self.pending() and not messagebox.askyesno(
                APP, "You have turns that aren't saved yet. Editing again will lose them. Continue?", parent=self.root):
            return
        orienter = orient.make_orienter() if self.use_ai.get() else None
        self.cancel.clear()
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.bar.configure(value=0, maximum=max(1, len(find_scans(Path(folder)))))
        self.say(f"Editing scans in {folder}" + (" (turning cards upright with AI)" if orienter else ""))

        def work():
            try:
                results = process_folder(folder, overwrite=self.overwrite.get(), orient=orienter, cancel=self.cancel,
                                         progress=lambda m, d, t: self.events.put(("progress", m, d, t)))
                self.events.put(("done", results, orienter.cost_usd if orienter else None))
            except Cancelled:
                self.events.put(("stopped",))
            except Exception as e:  # shown to the user, never a silent crash
                self.events.put(("error", str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "progress":
                    _, msg, done, total = ev
                    self.bar.configure(value=done, maximum=max(1, total))
                    self.status.configure(text=f"{done} of {total} scans")
                    if not msg.endswith("…"):
                        self.say(msg)
                    else:
                        self.status.configure(text=f"{done} of {total} scans · {msg}")
                elif kind == "done":
                    results, cost = ev[1], ev[2]
                    made = sum(len(r.outputs) for r in results if not r.skipped and not r.error)
                    skipped = sum(1 for r in results if r.skipped)
                    problems = [r for r in results if r.error]
                    text = f"Done: {made} card image{'s' if made != 1 else ''} saved"
                    if skipped:
                        text += f", {skipped} scan{'s' if skipped != 1 else ''} already done (skipped)"
                    if problems:
                        text += f", {len(problems)} with problems (see above)"
                    if cost is not None:
                        text += f". AI check cost about ${cost:.4f}"
                    self.say(text + ".")
                    self.status.configure(text=text)
                    self._finish()
                    self.load_review()
                elif kind == "stopped":
                    self.say("Stopped. Scans finished so far are saved; the rest will be done next time.")
                    self._finish()
                    self.load_review()
                elif kind == "error":
                    self.say(f"Something went wrong: {ev[1]}")
                    self._finish()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self) -> None:
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    # ------------------------------------------------------------ review grid
    def load_review(self) -> None:
        for child in self.grid.winfo_children():
            child.destroy()
        self.tiles = []
        folder = Path(self.folder.get())
        scans = [s for s in find_scans(folder) if outputs_of(s)] if folder.is_dir() else []
        if not scans:
            ttk.Label(self.grid, text="No edited cards in this folder yet. Click “Edit scans”.",
                      foreground="#666").grid(row=0, column=0, sticky="w", pady=8)
            self.update_pending()
            return
        self._load_rows(scans, 0)

    def _load_rows(self, scans: list[Path], i: int) -> None:
        """Add a few scans at a time so the window stays responsive with big folders."""
        for scan in scans[i:i + 3]:
            row = len(self.grid.winfo_children())
            box = ttk.LabelFrame(self.grid, text=scan.name, padding=6)
            box.grid(row=row, column=0, sticky="ew", pady=4)
            for n, out in enumerate(outputs_of(scan)):
                try:
                    tile = CardTile(self, box, out)
                except FileNotFoundError:
                    continue
                tile.frame.grid(row=n // 6, column=n % 6, padx=3, pady=3, sticky="n")
                self.tiles.append(tile)
        if i + 3 < len(scans):
            self.root.after(10, self._load_rows, scans, i + 3)
        self.update_pending()

    def _select(self, value: bool) -> None:
        for t in self.tiles:
            t.selected.set(value)

    def _turn_selected(self, quarter: int) -> None:
        chosen = [t for t in self.tiles if t.selected.get()]
        if not chosen:
            return messagebox.showinfo(APP, "Tick the cards to turn first (click a card or its box).", parent=self.root)
        for t in chosen:
            t.turn(quarter)

    def pending(self) -> list[CardTile]:
        return [t for t in self.tiles if t.turns]

    def update_pending(self) -> None:
        n = len(self.pending())
        self.save_btn.configure(state="normal" if n else "disabled",
                                text=f"Save turns ({n})" if n else "Save turns")

    def save_turns(self) -> None:
        changes = {t.path: t.turns * 90 for t in self.pending()}
        done = apply_turns(changes)
        for t in self.pending():
            t.img = load(t.path)
            t.turns = 0
            t.turn(0)
        self.say(f"Saved {done} turned card{'s' if done != 1 else ''}.")
        self.update_pending()

    def _close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP, "Scans are still being edited. Stop and close?", parent=self.root):
                return
            self.cancel.set()
        if self.pending() and not messagebox.askyesno(APP, "Close without saving your turns?", parent=self.root):
            return
        self.root.destroy()


# ---------------------------------------------------------------- first run on Windows: install + desktop icon
def offer_desktop_icon(root: tk.Tk) -> None:
    """Running as the standalone .exe on Windows: offer to install it and put an icon on the desktop."""
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("win")):
        return
    settings = orient.load_settings()
    if settings.get("icon_offered"):
        return
    orient.save_settings({"icon_offered": True})
    if not messagebox.askyesno(APP, "Put a “Lister Scan Editor” icon on your desktop?\n\n"
                                    "The program is copied to your user profile, so it keeps working even if "
                                    "you delete this download.", parent=root):
        return
    import shutil

    exe = Path(sys.executable)
    home = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "Lister"
    home.mkdir(parents=True, exist_ok=True)
    target = home / exe.name
    try:
        if exe.resolve() != target.resolve():
            shutil.copy2(exe, target)
        ps = ("$d=[Environment]::GetFolderPath('Desktop');"
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Lister Scan Editor.lnk'));"
              f"$s.TargetPath='{target}';$s.WorkingDirectory='{home}';$s.IconLocation='{target},0';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        messagebox.showinfo(APP, "Done: “Lister Scan Editor” is on your desktop.", parent=root)
    except Exception as e:  # never block the app over the shortcut
        messagebox.showwarning(APP, f"Couldn't add the desktop icon ({e}).\nYou can still use this program.",
                               parent=root)


def main() -> int:
    """Open the window (see lister.editor for the command line, including --self-test)."""
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    EditorApp(root)
    root.after(500, offer_desktop_icon, root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    from .editor import main as _main

    raise SystemExit(_main())
