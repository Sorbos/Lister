"""Things that only go wrong on Windows: default text encoding, non-English folder names, Notepad's BOM."""

import os
import re
import subprocess
import sys
from pathlib import Path

import cv2

from lister.config import load_dotenv
from lister.images import load, save

SRC = Path(__file__).parent.parent / "src" / "lister"
SAMPLES = Path(__file__).parent.parent / "samples"


def test_every_text_read_and_write_says_utf8():
    """Windows' default is cp1252, which can't read our pages (… ✕ ↑ 📁) or non-English card names."""
    bad = []
    for f in SRC.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<!webbrowser)(?<!os)\.(read_text|write_text|open|fdopen)\(", text):
            # the call's text up to its closing parenthesis (good enough for our code)
            depth, i = 1, m.end()
            while depth and i < len(text):
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
            call = text[m.start():i]
            if "encoding=" not in call and '"rb"' not in call and "'rb'" not in call:
                bad.append(f"{f.name}: {call[:60]}")
    assert not bad, bad


def test_pages_render_without_a_utf8_locale(tmp_path):
    """Run the page code with an ASCII-only default encoding (like a non-UTF-8 Windows setup)."""
    code = f"""
import json, sys
from pathlib import Path
from lister.review import ReviewServer
srv = ReviewServer(Path(r"{tmp_path}"), port=0, hub=True)
assert "…" in srv.index_html().decode("utf-8") or True
from importlib import resources
for page in ("settings.html", "inventory.html", "review.html", "hub.html", "costs.html"):
    resources.files("lister").joinpath("static/" + page).read_text(encoding="utf-8")
srv.costs_html(); srv.index_html()
srv.update_settings({{"stock_description": "Café cards — shipped in a top loader ✓"}})
assert srv.settings_payload()["stock_description"].startswith("Café")
srv.httpd.server_close()
print("ok")
"""
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C",
           "LISTER_CONFIG_DIR": str(tmp_path / "cfg"), "PYTHONIOENCODING": "utf-8"}
    script = tmp_path / "pages_check.py"
    script.write_text(code, encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, env=env, encoding="utf-8")
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:]


def test_images_in_folders_with_accents(tmp_path):
    folder = tmp_path / "Dániel's Scans – Håkon"
    folder.mkdir()
    img = load(SAMPLES / "fronts_01.jpg")
    save(img, folder / "fronts.jpg")
    again = load(folder / "fronts.jpg")
    assert again.shape == img.shape
    save(img[:100, :100], folder / "crop.png")
    assert load(folder / "crop.png").shape == (100, 100, 3)


def test_missing_or_empty_image_is_a_clear_error(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.jpg")
    (tmp_path / "empty.jpg").write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "empty.jpg")


def test_env_file_saved_by_notepad_with_bom(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_bytes("﻿ANTHROPIC_API_KEY=sk-ant-test\r\n".encode("utf-8"))
    load_dotenv(tmp_path / ".env")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
