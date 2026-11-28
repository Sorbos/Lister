"""Entry point for the standalone ListerScanEditor.exe (PyInstaller).

A windowed .exe has no console, and an unhandled error would otherwise end in a generic dialog that
blocks forever when nobody is watching (e.g. the build's self-test). So errors are caught here:
in --self-test mode they're written to FOLDER/self-test.txt; in normal use they're shown in a
message box with the details.
"""
import sys
import traceback
from pathlib import Path


def _report(exc_text: str) -> None:
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--self-test":
        try:
            Path(args[1], "self-test.txt").write_text("ERROR\n" + exc_text, encoding="utf-8")
        except OSError:
            pass
        return
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Lister Scan Editor", "Something went wrong:\n\n" + exc_text[-1500:])
        root.destroy()
    except Exception:
        pass


try:
    from lister.editor import main

    code = main()
except SystemExit:
    raise
except BaseException:
    _report(traceback.format_exc())
    code = 2
raise SystemExit(code)
