"""Settings page: changing the scans folder and wait time from the browser."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lister import settings
from lister.review import ReviewServer
from lister.watcher import Watcher

SAMPLES = Path(__file__).parent.parent / "samples"


def test_check_inbox_rules(tmp_path, monkeypatch):
    root = tmp_path / "Lister"
    for d in ("batches", "done", "problems", "inbox"):
        (root / d).mkdir(parents=True)
    scans = tmp_path / "Card scans"
    scans.mkdir()
    assert settings.check_inbox(root, str(scans))[0] == "ok"
    assert settings.check_inbox(root, f'"{scans}"')[0] == "ok"  # pasted with quotes
    assert settings.check_inbox(root, "")[:2] == ("ok", None)  # back to default
    for bad, why in [(tmp_path / "typo", "doesn't exist"), (root / "done", "done folder"),
                     (root / "batches" / "x", "doesn't exist"), (root, "Lister folder itself"),
                     (Path("/"), "whole drive"), ("relative/folder", "full path")]:
        if isinstance(bad, Path) and bad.name == "x":
            bad.mkdir()
            why = "batches folder"
        result, _, msg = settings.check_inbox(root, str(bad))
        assert result == "error" and why in msg, (bad, msg)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "home folder" in settings.check_inbox(root, str(tmp_path))[2]


def test_busy_folder_needs_confirmation(tmp_path):
    root = tmp_path / "Lister"
    root.mkdir()
    photos = tmp_path / "Pictures"
    photos.mkdir()
    for i in range(settings.BUSY_FOLDER):
        (photos / f"holiday_{i}.jpg").write_bytes(b"x")
    result, _, msg = settings.check_inbox(root, str(photos))
    assert result == "confirm" and "20 images" in msg and "move" in msg
    assert settings.check_inbox(root, str(photos), confirmed=True)[0] == "ok"


def test_windows_paths_under_wsl(monkeypatch, tmp_path):
    if not Path("/mnt/c").is_dir():
        monkeypatch.setattr(Path, "is_dir", lambda self: str(self) == "/mnt/c" or Path.__dict__["exists"](self))
    assert settings.to_local_path(r"C:\Users\Daniel\Card scans") == Path("/mnt/c/Users/Daniel/Card scans")


def test_watcher_follows_new_folder(tmp_path):
    root = tmp_path / "Lister"
    clock = [1_800_000_000.0]
    w = Watcher(root, clock=lambda: clock[0], progress=lambda *_: None, identify=lambda *a, **k: [],
                background=False, auto_identify=False)
    scans = tmp_path / "Card scans"
    scans.mkdir()
    settings.save(root, {"inbox": str(scans), "wait_minutes": 1})
    assert w.inbox == scans and w.pair_timeout == 60
    shutil.copy(SAMPLES / "fronts_01.jpg", scans / "IMG_1.jpg")
    w.poll()
    clock[0] += 61
    w.poll()  # settled, but waits for a back
    clock[0] += 61
    w.poll()
    [batch] = list((root / "batches").iterdir())
    assert not list(scans.iterdir())  # moved to done/
    assert (root / "done" / batch.name / "IMG_1.jpg").exists()


@pytest.fixture
def hub(tmp_path):
    srv = ReviewServer(tmp_path / "Lister", port=0, hub=True)
    (tmp_path / "Lister").mkdir(exist_ok=True)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


def call(srv, path, body=None, origin=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
    req = urllib.request.Request(srv.url + path, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req) as r:
        return r.read()


def test_settings_page_and_api(hub, tmp_path):
    assert b"Scans folder" in call(hub, "settings")
    assert b'href="settings"' in call(hub, "")
    s = json.loads(call(hub, "api/settings"))
    assert s["using_default"] and s["inbox"].endswith("inbox") and s["wait_minutes"] == 10

    scans = tmp_path / "Card scans"
    scans.mkdir()
    out = json.loads(call(hub, "api/settings", {"inbox": str(scans)}, origin=hub.url.rstrip("/")))
    assert out["result"] == "ok" and out["inbox"] == str(scans.resolve()) and not out["using_default"]
    assert settings.inbox(hub.folder) == scans.resolve()
    assert str(scans.resolve()).encode() in call(hub, "")  # batch list says what it's watching

    out = json.loads(call(hub, "api/settings", {"inbox": str(tmp_path / "nope")}))
    assert out["result"] == "error" and settings.inbox(hub.folder) == scans.resolve()  # unchanged

    out = json.loads(call(hub, "api/settings", {"wait_minutes": 3}))
    assert out["result"] == "ok" and settings.load(hub.folder)["wait_minutes"] == 3
    assert json.loads(call(hub, "api/settings", {"wait_minutes": 0}))["result"] == "error"

    out = json.loads(call(hub, "api/settings", {"inbox": ""}))
    assert out["using_default"]


def test_other_websites_cannot_change_settings(hub, tmp_path):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(hub, "api/settings", {"inbox": str(tmp_path)}, origin="https://evil.example")
    assert e.value.code == 403
    assert settings.load(hub.folder)["inbox"] is None
