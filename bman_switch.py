"""Runs Bman Downloader and the Retro Downloader in one window, one at a time.

The "Retro Downloader" button at the bottom of the main app's sidebar (and the
"Bman Downloader" button in the retro app) asks this module to switch. The
current app's files are unloaded - its window, timers, log file and modules are
thrown away - and the other app's files are loaded into a new window at the same
position and size:

    main  -> bman_core.py / bman_ui.py              (next to BmanDownloader.py)
    retro -> retro/bman_core.py / retro/bman_ui.py  (the retro subfolder)

Both use <exe folder>\\core: the main app's files are directly in it, the retro
app's in core\\retro\\, and the archive.org login (core\\archive_creds.json) is
shared. bman_archive.py (zip/7z/rar) is shared code and stays loaded.

If anything is downloading, extracting, converting or uploading, the user is
asked first. Switching anyway cancels the downloads (running and queued) and stops
bulk actions after the game they are on; an extraction / conversion / upload that
already started finishes on its own thread, and that app can't be reopened until
it has. Switching is always refused while an update is being installed.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import time

MODES = {
    "main": {"core": "bman_core", "ui": "bman_ui", "label": "Bman Downloader"},
    "retro": {"core": "retro.bman_core", "ui": "retro.bman_ui", "label": "Retro Downloader"},
}

_STATE = {
    "next": None,
    "current": None,       # mode of the app in the window now
    "background": {},      # mode -> (old window, its busy download rows) still finishing work
}


# ----------------------------------------------------------------------------- called from the apps

def available(mode: str) -> bool:
    """True if that app's files are present (the retro folder may be left out of a build)."""
    spec = MODES.get(mode)
    if not spec:
        return False
    if spec["ui"] in sys.modules:
        return True
    try:
        if importlib.util.find_spec(spec["ui"]) is not None:
            return True
    except Exception:
        pass
    # A packaged exe's importer may not answer find_spec; the build includes both
    # apps, and a failed switch is reported and leaves the current app open.
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def busy_reason(window) -> str | None:
    """Why the app in this window can't be closed right now, or None."""
    try:
        queue = getattr(window, "download_queue", None)
        if queue is not None and not queue.empty():
            return "games are still waiting in the download queue"
    except Exception:
        pass
    if getattr(window, "is_downloading", False) or getattr(window, "_dl_thread_running", False):
        return "a game is still downloading"
    if getattr(window, "_post_pending", 0):
        return "a downloaded game is still being extracted / converted / uploaded"
    if getattr(window, "_bulk_running", False):
        return "a bulk action (Extract All / GOD All / Upload All) is still running"
    if getattr(window, "_update_installing", False):
        return "an update is being installed"
    # Per-game buttons on the Downloads tab (Extract, Delete, Upload...).
    stack = [window]
    while stack:
        widget = stack.pop()
        if widget is not window and getattr(widget, "is_working", False):
            return "a game on the Downloads tab is still being worked on"
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            pass
    return None


def _working_rows(window) -> list:
    rows, stack = [], [window]
    while stack:
        widget = stack.pop()
        if widget is not window and getattr(widget, "is_working", False):
            rows.append(widget)
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            pass
    return rows


def background_reason(mode: str) -> str | None:
    """Why an app left running work behind when it was switched away from, or None.

    Only flags its threads set themselves are read - the old window is gone."""
    left = _STATE["background"].get(mode)
    if not left:
        return None
    window, rows = left
    reason = None
    try:
        queue = getattr(window, "download_queue", None)
        if queue is not None and not queue.empty():
            reason = "its cancelled downloads are still clearing"
    except Exception:
        pass
    if reason is None and (getattr(window, "is_downloading", False)
                           or getattr(window, "_dl_thread_running", False)):
        reason = "a cancelled download is still stopping"
    if reason is None and getattr(window, "_post_pending", 0):
        reason = "an extraction / conversion / upload is still finishing"
    if reason is None and getattr(window, "_bulk_running", False):
        reason = "a bulk action is finishing the game it was on"
    if reason is None and any(getattr(row, "is_working", False) for row in rows):
        reason = "a game on the Downloads tab is still being worked on"
    if reason is None:
        _STATE["background"].pop(mode, None)     # all done - let the old window go
    return reason


def _say(notify, title: str, message: str) -> None:
    if notify:
        notify(title, message)
    else:
        print(message)


def request_switch(window, target: str, notify=None, confirm=None) -> None:
    """Close this app and open the other one in its place.

    notify(title, text) shows a message; confirm(title, text) -> bool asks the user."""
    if target not in MODES or not available(target):
        return
    label = MODES[target]["label"]
    current = _STATE.get("current")
    here = MODES[current]["label"] if current in MODES else "This app"

    if getattr(window, "_update_installing", False):
        _say(notify, "Still busy", f"Can't switch to {label} while an update is being installed.")
        return

    leftover = background_reason(target)
    if leftover:
        _say(notify, "Still finishing",
             f"{label} is still finishing work from before you switched away - {leftover}.\n\n"
             f"Its progress shows in the Logs tab here. Try again in a moment.")
        return

    reason = busy_reason(window)
    if reason:
        message = (f"{here} is still working - {reason}.\n\n"
                   f"Switching to {label} will:\n"
                   f"  - cancel downloads that are running or waiting in the queue "
                   f"(they start over when queued again)\n"
                   f"  - stop bulk actions after the game they are on\n\n"
                   f"An extraction, conversion or upload that has already started can't be cut "
                   f"off safely, so it finishes in the background first. You can switch back "
                   f"once it's done.\n\nSwitch anyway?")
        if confirm is None or not confirm("Switch anyway?", message):
            return
        rows = _working_rows(window)
        stop = getattr(window, "stop_work_for_switch", None)
        if stop is not None:
            try:
                stop()
            except Exception as exc:
                print(f"Could not stop the running work: {exc}")
        if current in MODES:
            _STATE["background"][current] = (window, rows)

    _STATE["next"] = target
    window.quit()                  # leaves mainloop; run() takes it from there


# ----------------------------------------------------------------------------- process-wide hooks

def _capture() -> dict:
    """The stdout/stderr/excepthooks (and CustomTkinter patch) in force right now."""
    hooks = {
        "stdout": sys.stdout,
        "stderr": sys.stderr,
        "excepthook": sys.excepthook,
        "thread_excepthook": threading.excepthook,
    }
    ctk = sys.modules.get("customtkinter")
    if ctk is not None:
        hooks["ctk_set_appearance_mode"] = ctk.set_appearance_mode
    return hooks


def _restore(hooks: dict) -> None:
    sys.stdout = hooks["stdout"]
    sys.stderr = hooks["stderr"]
    sys.excepthook = hooks["excepthook"]
    threading.excepthook = hooks["thread_excepthook"]
    ctk = sys.modules.get("customtkinter")
    original = hooks.get("ctk_set_appearance_mode")
    if ctk is not None and original is not None:
        ctk.set_appearance_mode = original


def _close_log_file(core) -> None:
    handle = getattr(core, "_LOG_FILE_HANDLE", None)
    if handle is None:
        return
    try:
        core._LOG_FILE_HANDLE = None        # its tee objects stop writing to it
        handle.close()
    except Exception:
        pass


def _purge(mode: str) -> None:
    """Forget an app's modules so the next switch loads them fresh from disk."""
    spec = MODES[mode]
    for name in (spec["ui"], spec["core"]):
        sys.modules.pop(name, None)
        if "." in name:
            package, _, attr = name.rpartition(".")
            parent = sys.modules.get(package)
            if parent is not None and hasattr(parent, attr):
                try:
                    delattr(parent, attr)
                except Exception:
                    pass


def _cancel_timers(window) -> None:
    """Drop the old window's pending after() callbacks so they don't keep running."""
    try:
        for timer in window.tk.splitlist(window.tk.call("after", "info")):
            try:
                window.tk.call("after", "cancel", timer)
            except Exception:
                pass
    except Exception:
        pass


def _reset_ctk_trackers(window) -> None:
    """CustomTkinter runs its DPI / appearance checks from whichever window came
    first; with that window gone, let the new one restart them."""
    ctk = sys.modules.get("customtkinter")
    if ctk is None:
        return
    for name in ("ScalingTracker", "AppearanceModeTracker"):
        tracker = getattr(ctk, name, None)
        if tracker is None:
            continue
        try:
            apps = getattr(tracker, "app_list", None)
            if isinstance(apps, list) and window in apps:
                apps.remove(window)
            dpi = getattr(tracker, "window_dpi_scaling_dict", None)
            if isinstance(dpi, dict):
                dpi.pop(window, None)
            tracker.update_loop_running = False
        except Exception:
            pass


# ----------------------------------------------------------------------------- loading an app

def _load_index(mode: str, core):
    """The game list, the same way each app's own start-up did it."""
    if mode == "main":
        try:
            return core.Index.load_combined()
        except Exception as exc:
            print(f"Could not load the game list: {exc}", file=sys.stderr)
            return core.Index.offline()
    try:
        return core.Index.load_combined()
    except core.IndexLoadError as exc:
        # Open anyway: the window explains what went wrong and Settings can fix it.
        print(str(exc), file=sys.stderr)
        return core.Index.empty(str(exc))


def _import(mode: str):
    spec = MODES[mode]
    core = importlib.import_module(spec["core"])
    ui = importlib.import_module(spec["ui"])
    return core, ui


class _Session:
    def __init__(self, mode, core, ui, window, hooks_before):
        self.mode = mode
        self.core = core
        self.ui = ui
        self.window = window
        self.hooks_before = hooks_before    # what to restore when this app is unloaded


def _start_first(mode: str, baseline: dict) -> _Session:
    core, ui = _import(mode)
    if baseline.get("ctk_set_appearance_mode") is None:
        # customtkinter was installed by the first core import; keep its real function.
        baseline["ctk_set_appearance_mode"] = getattr(core, "_ctk_original_set_appearance_mode", None)
    if mode == "main" and hasattr(core, "clean_pyinstaller_temp"):
        # Clear old %TEMP%\_MEI* folders in the background so start-up isn't slowed.
        threading.Thread(target=core.clean_pyinstaller_temp, daemon=True, name="mei-cleanup").start()
    index = _load_index(mode, core)
    window = ui.ArchiveGUI(index)
    return _Session(mode, core, ui, window, baseline)


def _show_overlay(window, text: str, subtext: str = ""):
    """Covers the window while the other app loads, so nothing in it can be clicked.

    text is the big line, subtext the smaller grey line under it."""
    import tkinter as tk
    try:
        overlay = tk.Frame(window, bg="#1d1e1e", cursor="watch")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        tk.Label(overlay, text=text, bg="#1d1e1e", fg="#dce4ee",
                 font=("Segoe UI", 14, "bold")).place(relx=0.5, rely=0.45, anchor="center")
        if subtext:
            tk.Label(overlay, text=subtext, bg="#1d1e1e", fg="#8a8f94",
                     font=("Segoe UI", 11)).place(relx=0.5, rely=0.45, y=30, anchor="center")
        overlay.lift()
        overlay.grab_set()
        window.update_idletasks()
        return overlay
    except Exception:
        return None


def _hide_overlay(overlay) -> None:
    if overlay is None:
        return
    try:
        overlay.grab_release()
        overlay.destroy()
    except Exception:
        pass


def _report(window, title: str, text: str) -> None:
    print(f"{title}: {text}")
    try:
        from tkinter import messagebox
        messagebox.showerror(title, text, parent=window)
    except Exception:
        pass


def _switch(session: _Session, target: str, baseline: dict) -> _Session | None:
    """Swap the window to the other app. Returns the new session, the old one if the
    switch failed, or None if the window was closed while switching."""
    old = session.window
    label = MODES[target]["label"]
    overlay = _show_overlay(old, f"Opening {label}...", "Loading game list cache...")

    # 1. Load the other app's files and game list while the old window stays up.
    old_hooks = _capture()
    _restore(baseline)              # the new core installs its own log file / hooks
    result: dict = {}
    try:
        core, ui = _import(target)
    except Exception as exc:
        result["error"] = exc
    else:
        def work():
            try:
                result["index"] = _load_index(target, core)
            except Exception as exc:          # pragma: no cover - loaders already fall back
                result["error"] = exc

        loader = threading.Thread(target=work, daemon=True, name=f"{target}-index")
        loader.start()
        while loader.is_alive():
            try:
                old.update()
            except Exception:                  # window closed while loading
                loader.join()
                _restore(baseline)
                return None
            time.sleep(0.02)

    if "error" in result:
        _purge(target)
        _restore(old_hooks)
        _hide_overlay(overlay)
        left = _STATE["background"].get(session.mode)
        if left and left[0] is old:
            del _STATE["background"][session.mode]     # it stays open, so it isn't "in the background"
        _report(old, f"Could not open {label}", f"{type(result['error']).__name__}: {result['error']}")
        return session

    # 2. Unload the current app.
    geometry, zoomed = None, False
    try:
        geometry = old.geometry()
        zoomed = old.state() == "zoomed"
    except Exception:
        pass
    new_hooks_baseline = _capture()        # the new core's log tee, set up by its import
    _restore(baseline)
    _close_log_file(session.core)
    try:
        old.destroy()
    except Exception:
        pass
    _cancel_timers(old)
    _reset_ctk_trackers(old)
    _purge(session.mode)

    # 3. Load the new app's window where the old one was.
    _restore(new_hooks_baseline)
    try:
        window = ui.ArchiveGUI(result["index"])
    except Exception as exc:
        _restore(baseline)
        _close_log_file(core)
        _purge(target)
        print(f"Could not open {label}: {exc} - going back to {MODES[session.mode]['label']}.")
        fallback = _start_first(session.mode, baseline)
        _place(fallback.window, geometry, zoomed)
        _report(fallback.window, f"Could not open {label}", f"{type(exc).__name__}: {exc}")
        return fallback
    _place(window, geometry, zoomed)
    try:
        core.log_event(f"Switched from {MODES[session.mode]['label']} to {label}.")
    except Exception:
        pass
    return _Session(target, core, ui, window, baseline)


def _place(window, geometry: str | None, zoomed: bool) -> None:
    try:
        if geometry:
            window.geometry(geometry)
        if zoomed:
            window.after(50, lambda: window.state("zoomed"))
        window.after(60, window.lift)
        window.after(80, window.focus_force)
    except Exception:
        pass


# ----------------------------------------------------------------------------- entry point

def run(start_mode: str = "main") -> int:
    if start_mode not in MODES or not available(start_mode):
        start_mode = "main"
    try:
        import customtkinter  # noqa: F401  - so the untouched CustomTkinter functions are recorded
    except Exception:
        pass
    baseline = _capture()

    session = _start_first(start_mode, baseline)
    while session is not None:
        _STATE["next"] = None
        _STATE["current"] = session.mode
        session.window.mainloop()
        target = _STATE["next"]
        if not target:
            break                              # the window was closed
        if target == session.mode:
            continue
        session = _switch(session, target, baseline)
    return 0
