"""Bman Downloader - start the app from this file (and build the exe from it).

bman_core.py, bman_ui.py and bman_switch.py must sit next to it, and the Retro
Downloader's files in the retro\\ folder beside them. Start with --retro to open
straight into the Retro Downloader.

The window can switch between the two apps from the button at the bottom of the
sidebar; bman_switch.py does the switching."""
from __future__ import annotations

import sys

import bman_switch


def main() -> int:
    start = "retro" if "--retro" in sys.argv[1:] else "main"
    return bman_switch.run(start)

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
