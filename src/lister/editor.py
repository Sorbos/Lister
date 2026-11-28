"""Start Lister Scan Editor: `python -m lister.editor` (or ListerScanEditor.exe).

`--self-test FOLDER` edits FOLDER without opening a window and writes FOLDER/self-test.txt; the
Windows build uses it to check the finished .exe really works. Exit code 0 if every scan worked.
"""

from __future__ import annotations

import sys
from pathlib import Path


def self_test(folder: str) -> int:
    from .editor_core import process_folder

    results = process_folder(folder)
    lines = [r.summary() for r in results]
    ok = bool(results) and not any(r.error for r in results)
    Path(folder, "self-test.txt").write_text("\n".join(lines + ["OK" if ok else "FAILED"]) + "\n", encoding="utf-8")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "--self-test":
        return self_test(argv[1])
    from .editor_gui import main as window  # needs tkinter; only loaded when a window is wanted

    return window()


if __name__ == "__main__":
    raise SystemExit(main())
