"""Bman Downloader - start the app from this file (and build the exe from it).

bman_core.py and bman_ui.py must sit next to it."""
from __future__ import annotations

import sys

import threading

from bman_core import Index, IndexLoadError, clean_pyinstaller_temp
from bman_ui import ArchiveGUI


def main() -> int:
    # Clear old %TEMP%\_MEI* folders in the background so start-up isn't slowed.
    threading.Thread(target=clean_pyinstaller_temp, daemon=True, name="mei-cleanup").start()

    # The app always opens, even if archive.org is down: it uses the saved game
    # list (or an empty one) and keeps retrying in the background.
    try:
        index = Index.load_combined()
    except Exception as exc:
        print(f"Could not load the game list: {exc}", file=sys.stderr)
        index = Index.offline()

    app = ArchiveGUI(index)
    app.mainloop()
    return 0

if __name__ == "__main__":
    if sys.platform == "win32" and not getattr(sys, 'frozen', False) and "__compiled__" not in globals():
        import ctypes
        try:
            kernel32 = ctypes.WinDLL('kernel32')
            user32 = ctypes.WinDLL('user32')
            hWnd = kernel32.GetConsoleWindow()
            if hWnd:
                user32.ShowWindow(hWnd, 0)
        except Exception:
            pass
    sys.exit(main())