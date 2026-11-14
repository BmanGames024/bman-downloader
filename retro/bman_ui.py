"""Bman Downloader - the interface: dialogs, the list widgets and the main window.

All the logic it calls lives in bman_core.py."""
from __future__ import annotations

import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from collections import deque
from pathlib import Path
from urllib.parse import quote

from .bman_core import (
    normalize_search_text,
    sources_editable,
    ACCENT_PRESETS, ALL_CONSOLES_LABEL, APP_VERSION, APP_WINDOW, ARCHIVE_S3_KEYS_URL,
    COLLECTION_EXPORT_EXT, COMMUNITY_CACHE_TTL_SECONDS, EMBEDDED_IN_MAIN_APP, CONSOLE_KEYS, CONSOLE_NAMES,
    CONSOLE_SETTING_KEY, CommunityEntry, DEFAULT_ACCENT, DELETE_AFTER_EXTRACT_KEY,
    DOWNLOAD_DIR, DOWNLOAD_SETTINGS, DownloadSetting, Downloader, EXTRACT_DIR, Files,
    GameCollection, IS_FROZEN, Index, LOG_QUEUE, POST_MODES, POST_MODE_AUTO, POST_MODE_EXTRACT,
    POST_MODE_KEEP, POST_MODE_LABELS, REGION_FILTER_ALL, REGION_FILTER_DEFAULT,
    REGION_FILTER_LABELS, REGION_SETTING_KEY, RUN_DIR, SOURCES_FILE,
    STEAMGRIDDB_API_KEY, STEAMGRIDDB_BASE_URL,
    TextRedirector, UI_PUMP_MS, _BULK_STATUS_SLOT, _HOVER_FACTOR, _drain_ui_pending, _shade,
    _write_archive_credentials, accent_fg, archive_support, accent_hover, apply_download_settings, apply_update,
    check_for_update, clean_incomplete_downloads, cleanup_update_leftovers,
    community_can_share, community_entry_stamp, console_display_name, console_is_disc,
    console_key_for_label, contrast_text_for, curl_cffi_requests, current_accent,
    delete_after_extract_enabled, delete_collection, download_dir_for, download_key,
    download_key_for_path, download_update, downloaded_files, drop_ui_slot,
    entry_matches_region, entry_title, export_collection, extract_dir_for,
    fetch_community_collection, fetch_community_index, find_unextracted_archives, format_size, get_collection,
    get_download_settings, grab_archive_credentials, handle_post_download, has_archive_keys,
    is_archive_file, is_extracted, load_cached_community_collection, load_collections,
    load_community_cache, load_settings, log_event, normalize_hex, normalize_setting, post_ui,
    post_ui_latest, prune_community_list_cache, publish_community_collection,
    read_collection_file, refresh_games_against_index, region_filter_for_label, relaunch_app,
    resolve_post_mode, retheme_widgets, safe_collection_filename,
    save_accent, save_archive_credentials, save_archive_s3_keys, save_community_cache,
    save_post_mode, save_settings, saved_post_mode, set_accent, sources_path,
    unique_collection_name, unshare_community_collection, upsert_collection,
    write_sources_template,
)

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk


def fit_dialog_to_content(dialog) -> None:
    """Make a fixed-size dialog taller when its content needs more room than it was given.

    These dialogs set a height up front and pack their buttons last, so anything
    that makes the text a little taller (an extra note, a long message, a different
    font) used to push the buttons out of the window.
    """
    try:
        dialog.update_idletasks()
        width, height = (int(part) for part in dialog.geometry().split("+")[0].split("x"))
        scale = float(ctk.ScalingTracker.get_window_scaling(dialog)) or 1.0
        needed = int(dialog.winfo_reqheight() / scale) + 2
        if needed > height:
            dialog.geometry(f"{width}x{needed}")
    except Exception:
        pass


class InfoDialog(ctk.CTkToplevel):

    def __init__(self, master, title: str, message: str, ok_text: str = "OK"):
        super().__init__(master)
        self.title(title)
        self.geometry("470x210")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=16, weight="bold"),
                     anchor="w", justify="left").pack(anchor="w", padx=20, pady=(20, 6))
        ctk.CTkLabel(self, text=message, wraplength=420, justify="left",
                     anchor="w").pack(anchor="w", fill="x", padx=20, pady=(0, 10))

        ctk.CTkButton(self, text=ok_text, width=110, fg_color="#2b8a3e",
                      hover_color="#236c31", command=self._close).pack(side="bottom", pady=(0, 20))

        fit_dialog_to_content(self)
        self.after(120, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def show_info_popup(master, title: str, message: str) -> None:
    try:
        dialog = InfoDialog(master, title, message)
        master.wait_window(dialog)
    except Exception as exc:
        log_event(f"Popup failed ({title}): {exc}")


class ConfirmDialog(ctk.CTkToplevel):

    def __init__(self, master, title: str, message: str,
                 confirm_text: str = "Yes", cancel_text: str = "Not now"):
        super().__init__(master)
        self.result = False
        self.title(title)
        self.geometry("500x260")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=16, weight="bold"),
                     anchor="w", justify="left").pack(anchor="w", padx=20, pady=(20, 6))
        ctk.CTkLabel(self, text=message, wraplength=450, justify="left",
                     anchor="w").pack(anchor="w", fill="x", padx=20, pady=(0, 10))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(side="bottom", pady=(0, 20))
        ctk.CTkButton(buttons, text=cancel_text, width=120, fg_color="#444",
                      hover_color="#333", command=self._cancel).pack(side="left", padx=6)
        ctk.CTkButton(buttons, text=confirm_text, width=150, fg_color="#2b8a3e",
                      hover_color="#236c31", command=self._confirm).pack(side="left", padx=6)

        fit_dialog_to_content(self)
        self.after(120, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _finish(self, result: bool):
        self.result = result
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _confirm(self):
        self._finish(True)

    def _cancel(self):
        self._finish(False)


def ask_confirm_popup(master, title: str, message: str,
                      confirm_text: str = "Yes", cancel_text: str = "Not now") -> bool:
    try:
        dialog = ConfirmDialog(master, title, message, confirm_text, cancel_text)
        master.wait_window(dialog)
        return bool(dialog.result)
    except Exception as exc:
        log_event(f"Confirm popup failed ({title}): {exc}")
        return False




class CollectionEditDialog(ctk.CTkToplevel):

    def __init__(self, master, title: str = "New Collection",
                 name: str = "", description: str = "", ok_text: str = "Create"):
        super().__init__(master)
        self.result: tuple[str, str] | None = None
        self._existing = {c.name.lower() for c in load_collections()}
        self._original = (name or "").lower()

        self.title(title)
        self.geometry("460x280")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=20, pady=(20, 10))

        ctk.CTkLabel(self, text="Name", anchor="w").pack(fill="x", padx=20)
        self.name_entry = ctk.CTkEntry(self, placeholder_text="e.g. Co-op night")
        self.name_entry.pack(fill="x", padx=20, pady=(2, 10))
        self.name_entry.insert(0, name or "")
        self.name_entry.bind("<Return>", lambda e: self._on_ok())

        ctk.CTkLabel(self, text="Description (optional)", anchor="w").pack(fill="x", padx=20)
        self.desc_entry = ctk.CTkEntry(self, placeholder_text="What's in here / who it's for")
        self.desc_entry.pack(fill="x", padx=20, pady=(2, 6))
        self.desc_entry.insert(0, description or "")

        self.error_lbl = ctk.CTkLabel(self, text="", text_color="#d97070",
                                      font=ctk.CTkFont(size=11), anchor="w")
        self.error_lbl.pack(fill="x", padx=20)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(10, 20), side="bottom")
        ctk.CTkButton(row, text=ok_text, fg_color="#2b8a3e", hover_color="#236c31",
                      command=self._on_ok).pack(side="right")
        ctk.CTkButton(row, text="Cancel", width=90, fg_color="transparent", border_width=1,
                      text_color=("black", "white"), command=self._on_cancel).pack(side="right", padx=(0, 8))

        fit_dialog_to_content(self)
        self.after(120, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
            self.name_entry.focus_set()
        except Exception:
            pass

    def _on_ok(self):
        name = self.name_entry.get().strip()
        if not name:
            self.error_lbl.configure(text="Give it a name first.")
            return
        if name.lower() != self._original and name.lower() in self._existing:
            self.error_lbl.configure(text="You already have a collection with that name.")
            return
        self.result = (name, self.desc_entry.get().strip())
        self._close()

    def _on_cancel(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class CollectionPickerDialog(ctk.CTkToplevel):

    def __init__(self, master, game_name: str, game_names: list | None = None):
        super().__init__(master)
        self.result: str | None = None
        self._master = master
        self._game_names = list(game_names) if game_names else None

        self.title("Add to Collection")
        self.geometry("480x420")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        ctk.CTkLabel(self, text="Add to collection", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=20, pady=(20, 2))
        ctk.CTkLabel(self, text=game_name, text_color="gray", wraplength=430,
                     justify="left", anchor="w").pack(fill="x", padx=20, pady=(0, 12))

        self.list_scroll = ScrollFrame(self, label_text="Your collections")
        self.list_scroll.pack(fill="both", expand=True, padx=20)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(12, 20))
        ctk.CTkButton(row, text="+ New Collection", fg_color="#2b8a3e", hover_color="#236c31",
                      command=self._new_collection).pack(side="left")
        ctk.CTkButton(row, text="Cancel", width=90, fg_color="transparent", border_width=1,
                      text_color=("black", "white"), command=self._on_cancel).pack(side="right")

        self._render(game_name)
        self.after(120, self._grab)

    def _render(self, game_name: str):
        for widget in self.list_scroll.winfo_children():
            widget.destroy()

        collections = load_collections()
        if not collections:
            ctk.CTkLabel(self.list_scroll, text="No collections yet - make one below.",
                         text_color="gray").pack(pady=20)
            return

        names = [(name or "").lower() for name in (self._game_names or [game_name])]
        for collection in collections:
            in_here = collection.name_set()
            present = sum(1 for name in names if name in in_here)
            already = present == len(names)
            count = len(collection.games)
            label = f"{collection.name}   ({count} game{'s' if count != 1 else ''})"
            if already:
                label += "  - already in here"
            elif present:
                label += f"  - {present} of {len(names)} already in here"
            btn = ctk.CTkButton(
                self.list_scroll, text=label, anchor="w",
                fg_color="transparent" if not already else ("gray80", "gray28"),
                text_color=("black", "white"), hover_color=("gray70", "gray30"),
                state="disabled" if already else "normal",
                command=lambda n=collection.name: self._choose(n),
            )
            btn.pack(fill="x", pady=2)

    def _new_collection(self):
        dialog = CollectionEditDialog(self, title="New Collection")
        self.wait_window(dialog)
        if not dialog.result:
            self._grab()
            return
        name, description = dialog.result
        collection = GameCollection(name=name, description=description,
                                    created=time.strftime("%Y-%m-%d %H:%M:%S"))
        upsert_collection(collection)
        self._choose(name)

    def _choose(self, name: str):
        self.result = name
        self._close()

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _on_cancel(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class CollectionQueueDialog(ctk.CTkToplevel):

    def __init__(self, master, collection_name: str, game_count: int,
                 intro: str | None = None, window_title: str = "Download Collection"):
        super().__init__(master)
        self.result: dict | None = None

        self.title(window_title)
        self.geometry("560x300")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        ctk.CTkLabel(self, text=collection_name, font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=20, pady=(20, 2))
        ctk.CTkLabel(
            self, text=intro or f"{game_count} game{'s' if game_count != 1 else ''} "
                                f"will be added to the download queue.",
            text_color="gray", wraplength=510, justify="left", anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 14))

        ctk.CTkLabel(self, text="After each game downloads:",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=20)
        self.mode_var = ctk.StringVar(value=saved_post_mode())
        for value, label in POST_MODES:
            ctk.CTkRadioButton(self, text=label, variable=self.mode_var, value=value).pack(
                anchor="w", padx=30, pady=3)
        ctk.CTkLabel(self, text="Games are saved in downloads/<console>/, and anything extracted "
                                "goes to extracted/<console>/<game>/.",
                     text_color="gray", font=ctk.CTkFont(size=11), wraplength=510,
                     justify="left", anchor="w").pack(fill="x", padx=30, pady=(2, 12))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(18, 20), side="bottom")
        ctk.CTkButton(row, text="Start", fg_color="#2b8a3e", hover_color="#236c31",
                      command=self._on_ok).pack(side="right")
        ctk.CTkButton(row, text="Cancel", width=90, fg_color="transparent", border_width=1,
                      text_color=("black", "white"), command=self._on_cancel).pack(side="right", padx=(0, 8))

        fit_dialog_to_content(self)
        self.after(120, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _on_ok(self):
        self.result = {"mode": self.mode_var.get()}
        self._close()

    def _on_cancel(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class ApiKeysRequiredDialog(ctk.CTkToplevel):

    def __init__(self, master):
        super().__init__(master)
        self.title("API Keys Needed")
        self.geometry("500x265")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._grab)

        ctk.CTkLabel(self, text="API Keys Needed", font=ctk.CTkFont(size=17, weight="bold"),
                     anchor="w", justify="left").pack(anchor="w", padx=20, pady=(20, 8))
        ctk.CTkLabel(
            self,
            text=("Archive.org requires an account to download these items, so this app "
                  "needs your archive.org API keys before it can be used.\n\n"
                  "Open the settings below, click Grab API Keys to get them, then paste "
                  "both keys in and hit Save. They do not expire -- this is a one-time "
                  "setup."),
            wraplength=450, justify="left", anchor="w",
        ).pack(anchor="w", fill="x", padx=20, pady=(0, 14))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(row, text="Open API Key Settings", width=190, fg_color="#2b8a3e",
                      hover_color="#236c31", command=self._open_settings).pack(side="left")
        ctk.CTkButton(row, text="Quit", width=90, fg_color="transparent", border_width=1,
                      text_color=("black", "white"), command=self._quit).pack(side="right")

        fit_dialog_to_content(self)
        self.after(120, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _open_settings(self):
        master = self.master
        self._release()
        try:
            master.open_api_key_settings()
        except Exception as exc:
            log_event(f"Could not open API key settings: {exc}")

    def _quit(self):
        master = self.master
        self._release()
        try:
            master.destroy()
        except Exception:
            os._exit(0)

    def _release(self):
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass



class VirtualResultsList(ctk.CTkFrame):

    ROW_HEIGHT = 30
    WHEEL_ROWS = 3

    def __init__(self, master, on_selection_change=None, on_activate=None,
                 label_text: str = "Search Results", **kwargs):
        super().__init__(master, **kwargs)

        self._on_selection_change = on_selection_change
        self._on_activate = on_activate
        self._label_text = label_text

        self.items: list = []
        self._labels: list[str] = []
        self._selected: set[int] = set()
        self._anchor: int | None = None
        self._primary: int | None = None
        self._offset = 0
        self._rows: list = []
        self._hover_row = None
        self._empty_showing = False

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.header_lbl = ctk.CTkLabel(self, text=label_text, anchor="w",
                                       font=ctk.CTkFont(size=12, weight="bold"))
        self.header_lbl.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 0))

        self.note_lbl = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                     text_color="gray", font=ctk.CTkFont(size=11))
        self.note_lbl.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12)
        self.note_lbl.grid_remove()

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="nsew", padx=(6, 0), pady=(4, 8))

        self.scrollbar = ctk.CTkScrollbar(self, command=self._on_scrollbar)
        self.scrollbar.grid(row=2, column=1, sticky="ns", padx=(2, 6), pady=(4, 8))
        self.scrollbar.grid_remove()
        self._scrollbar_shown = False

        self.empty_lbl = ctk.CTkLabel(self.body, text="", wraplength=620, justify="left",
                                      anchor="nw", text_color="gray")

        self.body.bind("<Configure>", self._on_configure)
        self._bind_wheel(self.body)
        self._bind_wheel(self)


    def set_items(self, items, labels=None, note: str = "", empty_text: str = "") -> None:
        self.items = list(items)
        self._labels = list(labels) if labels is not None else [str(i) for i in self.items]
        self._selected.clear()
        self._anchor = None
        self._primary = None
        self._offset = 0
        self._hover_row = None

        if note:
            self.note_lbl.configure(text=note)
            self.note_lbl.grid()
        else:
            self.note_lbl.grid_remove()

        self._empty_showing = bool(empty_text) and not self.items
        if self._empty_showing:
            self.empty_lbl.configure(text=empty_text)
            self.empty_lbl.place(x=12, y=10)
            self.empty_lbl.lift()
        else:
            self.empty_lbl.place_forget()

        self.refresh()
        self._emit()
        self.after_idle(self._on_configure)

    def clear(self) -> None:
        self.set_items([])


    def selected_indices(self) -> list[int]:
        return sorted(i for i in self._selected if 0 <= i < len(self.items))

    def selected_items(self) -> list:
        return [self.items[i] for i in self.selected_indices()]

    def primary_item(self):
        if self._primary is not None and 0 <= self._primary < len(self.items):
            return self.items[self._primary]
        return None

    def select_all(self) -> None:
        if not self.items:
            return
        self._selected = set(range(len(self.items)))
        if self._anchor is None:
            self._anchor = 0
        if self._primary is None:
            self._primary = 0
        self.refresh()
        self._emit()

    def clear_selection(self) -> None:
        if not self._selected and self._primary is None:
            return
        self._selected.clear()
        self._anchor = None
        self._primary = None
        self.refresh()
        self._emit()

    def _emit(self) -> None:
        if self._on_selection_change is not None:
            try:
                self._on_selection_change(self.selected_items(), self.primary_item())
            except Exception as exc:
                print(f"Selection callback failed: {exc}")


    def _make_row(self):
        row = ctk.CTkFrame(self.body, fg_color="transparent", corner_radius=6,
                           height=self.ROW_HEIGHT - 2)
        label = ctk.CTkLabel(row, text="", anchor="w", justify="left",
                             text_color=("black", "white"), font=ctk.CTkFont(size=12))
        label.place(x=10, rely=0.5, anchor="w")
        row._label = label
        row._index = None

        for widget in (row, label):
            widget.bind("<Button-1>", lambda e, r=row: self._on_row_click(r, e))
            widget.bind("<Double-Button-1>", lambda e, r=row: self._on_row_double(r, e))
            widget.bind("<Enter>", lambda e, r=row: self._on_row_enter(r))
            widget.bind("<Leave>", lambda e, r=row: self._on_row_leave(r))
            self._bind_wheel(widget)
        return row

    def _resize_pool(self, count: int) -> None:
        while len(self._rows) > count:
            row = self._rows.pop()
            if row is self._hover_row:
                self._hover_row = None
            row.destroy()
        while len(self._rows) < count:
            self._rows.append(self._make_row())
        for slot, row in enumerate(self._rows):
            row.place(x=0, y=slot * self.ROW_HEIGHT, relwidth=1.0)

    def _visible_rows(self) -> int:
        return max(1, len(self._rows) - 1)

    def _on_configure(self, event=None) -> None:
        height = self.body.winfo_height()
        if height <= 1:
            return
        needed = max(1, int(height // self.ROW_HEIGHT) + 1)
        if needed != len(self._rows):
            self._resize_pool(needed)
        self.refresh()


    def refresh(self) -> None:
        total = len(self.items)
        page = self._visible_rows()
        max_offset = max(0, total - page)
        self._offset = max(0, min(self._offset, max_offset))

        for slot, row in enumerate(self._rows):
            index = self._offset + slot
            row._index = index if index < total else None
            if row._index is None:
                row._label.configure(text="")
            else:
                row._label.configure(text=self._labels[index])
            self._paint_row(row)

        if total <= page:
            self.scrollbar.set(0, 1)
            if self._scrollbar_shown:
                self.scrollbar.grid_remove()
                self._scrollbar_shown = False
        else:
            self.scrollbar.set(self._offset / total, min(1.0, (self._offset + page) / total))
            if not self._scrollbar_shown:
                self.scrollbar.grid()
                self._scrollbar_shown = True

        if not total and self._empty_showing:
            try:
                self.empty_lbl.lift()
            except Exception:
                pass

        self._update_header()

    def _paint_row(self, row) -> None:
        index = row._index
        if index is not None and index in self._selected:
            accent = current_accent()
            row.configure(fg_color=accent)
            row._label.configure(text_color=contrast_text_for(accent))
        elif index is not None and row is self._hover_row:
            row.configure(fg_color=("gray70", "gray30"))
            row._label.configure(text_color=("black", "white"))
        else:
            row.configure(fg_color="transparent")
            row._label.configure(text_color=("black", "white"))

    def _update_header(self) -> None:
        total = len(self.items)
        if not total:
            self.header_lbl.configure(text=self._label_text)
            return
        text = f"{self._label_text}  -  {total:,} item{'s' if total != 1 else ''}"
        chosen = len(self._selected)
        if chosen:
            text += f"   |   {chosen:,} selected"
        else:
            text += "   |   Ctrl-click, Shift-click or Ctrl+A to pick several"
        self.header_lbl.configure(text=text)


    def _set_offset(self, value: int) -> str:
        page = self._visible_rows()
        value = max(0, min(int(value), max(0, len(self.items) - page)))
        if value != self._offset:
            self._offset = value
            self.refresh()
        return "break"

    def scroll_to_top(self) -> None:
        self._set_offset(0)

    def _on_scrollbar(self, *args):
        if not args:
            return
        how = args[0]
        try:
            if how == "moveto":
                self._set_offset(round(float(args[1]) * len(self.items)))
            elif how == "scroll":
                amount = int(float(args[1]))
                what = args[2] if len(args) > 2 else "units"
                step = amount * (self._visible_rows() if what == "pages" else 1)
                self._set_offset(self._offset + step)
        except (ValueError, IndexError):
            pass

    def _bind_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._on_wheel)
        widget.bind("<Button-4>", lambda e: self._set_offset(self._offset - self.WHEEL_ROWS))
        widget.bind("<Button-5>", lambda e: self._set_offset(self._offset + self.WHEEL_ROWS))

    def _on_wheel(self, event):
        delta = getattr(event, "delta", 0)
        if not delta:
            return "break"
        steps = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        return self._set_offset(self._offset + steps * self.WHEEL_ROWS)


    def _on_row_enter(self, row) -> None:
        previous, self._hover_row = self._hover_row, row
        if previous is not None and previous is not row:
            try:
                self._paint_row(previous)
            except Exception:
                pass
        self._paint_row(row)

    def _on_row_leave(self, row) -> None:
        if self._hover_row is row:
            self._hover_row = None
            self._paint_row(row)

    def _on_row_click(self, row, event):
        try:
            self.focus_set()
        except Exception:
            pass

        index = row._index
        if index is None:
            self.clear_selection()
            return "break"

        ctrl = bool(event.state & 0x0004)
        shift = bool(event.state & 0x0001)

        if shift and self._anchor is not None:
            low, high = sorted((self._anchor, index))
            block = set(range(low, high + 1))
            self._selected = (self._selected | block) if ctrl else block
            self._primary = index
        elif ctrl:
            if index in self._selected:
                self._selected.discard(index)
            else:
                self._selected.add(index)
            self._anchor = index
            self._primary = index if index in self._selected else None
        else:
            self._selected = {index}
            self._anchor = index
            self._primary = index

        self.refresh()
        self._emit()
        return "break"

    def _on_row_double(self, row, event):
        if row._index is None or self._on_activate is None:
            return "break"
        try:
            self._on_activate(self.items[row._index])
        except Exception as exc:
            print(f"Activate callback failed: {exc}")
        return "break"


_RDW_ALLCHILDREN = 0x0080
_RDW_UPDATENOW = 0x0100
_redraw_window = None
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        _redraw_window = ctypes.windll.user32.RedrawWindow
        _redraw_window.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
        _redraw_window.restype = wintypes.BOOL
    except Exception:
        _redraw_window = None


class ScrollFrame(ctk.CTkScrollableFrame):
    """A CTkScrollableFrame whose contents stay painted while it is being scrolled.

    Scrolling slides the picture already on screen and leaves the strip that
    just came into view to be painted later. Windows only asks for that paint
    when no mouse or wheel messages are waiting, so while the scrollbar is
    being dragged it never comes: every step slides the unpainted strip along
    with the rest and the list smears until the mouse stops.

    Here each scroll step has Windows ask for the paint straight away
    (RedrawWindow), and a 1 ms timer then lets the widgets draw it without
    waiting for the mouse to go quiet. Other systems paint on their own, so
    there this is a plain CTkScrollableFrame.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paint_after = None
        if _redraw_window is not None and self._orientation == "vertical":
            self._parent_canvas.configure(yscrollcommand=self._on_scrolled)

    def _on_scrolled(self, first, last):
        self._scrollbar.set(first, last)
        self.paint_scrolled()

    def paint_scrolled(self) -> None:
        """Call after the view has moved. Does nothing off Windows."""
        if _redraw_window is None:
            return
        try:
            _redraw_window(self._parent_canvas.winfo_id(), None, None,
                           _RDW_UPDATENOW | _RDW_ALLCHILDREN)
            if self._paint_after is None:
                self._paint_after = self.after(1, self._finish_paint)
        except Exception:
            pass

    def _finish_paint(self) -> None:
        self._paint_after = None
        try:
            self._parent_canvas.update_idletasks()
        except Exception:
            pass

    def destroy(self):
        pending, self._paint_after = getattr(self, "_paint_after", None), None
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        super().destroy()


def settle_scrollable(frame) -> None:
    """Re-sync a scrollable list after things were removed from it.

    When the content gets shorter while the list is scrolled down, the canvas is
    left showing where the content used to be: the list looks empty or cut off
    and will not scroll back. Refreshing the scroll region puts the view back on
    the content, at the top if it now all fits.
    """
    try:
        canvas = frame._parent_canvas
        frame.update_idletasks()
        bbox = canvas.bbox("all")
        if not bbox:
            return
        canvas.configure(scrollregion=bbox)
        if bbox[3] - bbox[1] <= canvas.winfo_height():
            canvas.yview_moveto(0.0)
        else:
            canvas.yview_moveto(canvas.yview()[0])
    except Exception:
        pass
    finally:
        refresh_game_rows(frame)


_NO_CARD = object()
_CARD_PACK = {"fill": "x", "pady": 6, "padx": 4}


def sync_cards(scroll, cache: dict, items: dict, visible: list, build,
               budget_ms: float = 45.0, min_build: int = 4) -> bool:
    """Make `scroll` show the cards for `visible`, reusing the ones already built.

    cache    {key: (signature, card)} - kept between calls by the caller.
    items    {key: signature} for every card that may exist right now.
    visible  the keys to show, in order.
    build    build(key) -> card (created but not packed).

    A card is rebuilt only when its signature changed. Returns False when the
    time budget ran out before every visible card was built; calling again
    carries on from there.
    """
    for key in list(cache):
        signature, card = cache[key]
        alive = False
        try:
            alive = bool(card.winfo_exists())
        except Exception:
            pass
        if not alive or items.get(key, _NO_CARD) != signature:
            try:
                card.destroy()
            except Exception:
                pass
            del cache[key]

    started = time.perf_counter()
    built = 0
    complete = True
    for key in visible:
        if key in cache:
            continue
        if built >= min_build and (time.perf_counter() - started) * 1000.0 >= budget_ms:
            complete = False
            break
        cache[key] = (items[key], build(key))
        built += 1

    order = [cache[key][1] for key in visible if key in cache]
    showing = set(order)
    cards = {card for _signature, card in cache.values()}
    packed = []
    for widget in scroll.pack_slaves():
        if widget not in cards:
            continue
        if widget in showing:
            packed.append(widget)
        else:
            widget.pack_forget()

    # Repack from the first card that is out of place. Plain pack() calls only:
    # CustomTkinter replays a widget's last pack() when the display scaling
    # changes, so the arguments must never point at another widget.
    start = 0
    while start < len(packed) and start < len(order) and packed[start] is order[start]:
        start += 1
    for card in packed[start:]:
        card.pack_forget()
    for card in order[start:]:
        card.pack(**_CARD_PACK)
    return complete


def watch_game_rows(scroll) -> bool:
    """Have a scrollable list tell its game-row canvases when what is on screen changes.

    Returns False if that list cannot be watched; its rows are then all drawn up
    front, the way they used to be.
    """
    if scroll is None:
        return False
    if getattr(scroll, "_game_rows", None) is not None:
        return True
    try:
        canvas, bar = scroll._parent_canvas, scroll._scrollbar

        paint_scrolled = getattr(scroll, "paint_scrolled", None)

        def on_view_change(first, last):
            bar.set(first, last)
            refresh_game_rows(scroll)
            if paint_scrolled is not None:
                paint_scrolled()

        def later(_event=None):
            # A card moved or the list was resized. Wait for the layout to finish,
            # and do it once however many cards moved.
            if not scroll._game_rows or scroll._game_rows_pending:
                return
            scroll._game_rows_pending = True
            scroll.after_idle(run_later)

        def run_later():
            scroll._game_rows_pending = False
            refresh_game_rows(scroll)

        scroll._game_rows = set()
        scroll._game_rows_pending = False
        canvas.configure(yscrollcommand=on_view_change)             # scrolling
        canvas.bind("<Configure>", later, add="+")                  # the list itself resized
        tk.Misc.bind(scroll, "<Configure>", later, "+")             # cards opened, closed or moved
    except Exception:
        scroll._game_rows = None
        return False
    return True


def refresh_game_rows(scroll) -> None:
    """Draw the rows that are on screen now, in every opened game list of `scroll`."""
    for rows in list(getattr(scroll, "_game_rows", None) or ()):
        try:
            rows.show_visible()
        except Exception:
            pass


class GameRowsCanvas(tk.Canvas):
    """The game rows of one opened collection, drawn on a single canvas.

    The rows used to be a frame, a label and a button per game. That is several
    native windows per game, which is what made long lists slow to open and
    smear while scrolling. Sizes, fonts and colours are taken from the same
    CustomTkinter theme those widgets used, so it looks the same.

    The canvas is as tall as the whole list, but only the rows in view (plus a
    few either side) exist as canvas items; the rest are added as they scroll
    into view and dropped as they leave. Opening a list therefore costs the
    same whether it holds twenty games or five thousand.
    """

    ROW_HEIGHT = 28
    ROW_PAD = 2
    BUTTON_WIDTH = 70
    BUTTON_HEIGHT = 24
    BUTTON_GAP = 8
    SIDE_PAD = 8
    LABEL_FONT_SIZE = 12
    MAX_ROWS = 400      # keeps one canvas well under Tk's 32,767 pixel limit, even at 250% scaling
    OVERSCAN = 12       # rows kept drawn beyond each edge of the visible part
    _SUPERSAMPLE = 4

    def __init__(self, master, labels, button_text: str = "", on_button=None,
                 bg=("gray92", "gray17"), grid_row: int = 0, scroll=None):
        self._labels = [str(label) for label in labels]
        self._grid_row = grid_row
        self._on_button = on_button
        self._button_text = button_text if on_button is not None else ""
        self._bg_spec = bg
        self._hover = None
        self._images: dict = {}
        self._button_x = 0
        self._button_w = self._button_h = 0
        self._row_h = self._pad = self._pitch = 1
        self._scroll = scroll if watch_game_rows(scroll) else None
        # Inside a watched list the rows are drawn once it has been laid out and
        # it is known which of them are in view (settle_scrollable does that).
        self._waiting_for_layout = self._scroll is not None
        self._drawn = range(0)
        try:
            self._scale = float(ctk.ScalingTracker.get_widget_scaling(master))
        except Exception:
            self._scale = 1.0

        super().__init__(master, highlightthickness=0, borderwidth=0, width=1, height=1,
                         takefocus=0, bg=self._color(bg))
        self.bind("<Configure>", self._on_resize)
        if self._button_text:
            self.bind("<Motion>", self._on_motion)
            self.bind("<Leave>", lambda _event: self._set_hover(None))
            self.bind("<Button-1>", self._on_click)
        try:
            ctk.ScalingTracker.add_widget(self._on_scaling, self)
        except Exception:
            pass
        self._draw()
        self._place()

        if self._scroll is not None:
            self._scroll._game_rows.add(self)
            # A safety net: whatever else happens, a canvas that comes on screen
            # checks that the rows it should be showing are there.
            self.bind("<Map>", lambda _event: self.show_visible(), add="+")
            self.bind("<Expose>", lambda _event: self.show_visible(), add="+")

    def _place(self) -> None:
        # A plain canvas is not scaled by CustomTkinter, so the side padding the
        # rows had is applied here in real pixels.
        self.grid(row=self._grid_row, column=0, sticky="ew", padx=self._px(self.SIDE_PAD))

    def destroy(self):
        try:
            ctk.ScalingTracker.remove_widget(self._on_scaling, self)
        except Exception:
            pass
        if self._scroll is not None:
            self._scroll._game_rows.discard(self)
        super().destroy()

    @staticmethod
    def _color(spec):
        if isinstance(spec, (list, tuple)):
            return spec[1] if ctk.get_appearance_mode() == "Dark" else spec[0]
        return spec

    def _px(self, value) -> int:
        # Half-up, the way Tk rounds the scaled sizes CustomTkinter hands it.
        return int(value * self._scale + 0.5)

    def _rgb(self, color) -> tuple:
        return tuple(channel >> 8 for channel in self.winfo_rgb(color))

    def _on_scaling(self, widget_scaling, _window_scaling=None):
        try:
            self._scale = float(widget_scaling)
            self._images.clear()
            self._draw()
            self._place()
        except Exception:
            pass

    def _button_image(self, fill: str):
        theme = ctk.ThemeManager.theme["CTkButton"]
        border = self._color(theme["border_color"])
        background = self._color(self._bg_spec)
        key = (fill, border, background, self._scale)
        image = self._images.get(key)
        if image is None:
            k = self._SUPERSAMPLE
            width, height = self._button_w, self._button_h
            # CustomTkinter truncates these two when it scales a button.
            line = max(1, int(self._scale))
            radius = min(int(theme["corner_radius"] * self._scale), height // 2)
            canvas = Image.new("RGB", (width * k, height * k), self._rgb(background))
            pen = ImageDraw.Draw(canvas)

            def rounded(inset: int, corner: int, color) -> None:
                x0, y0 = inset * k, inset * k
                x1, y1 = (width - inset) * k - 1, (height - inset) * k - 1
                r = max(0, corner) * k
                pen.rectangle((x0 + r, y0, x1 - r, y1), fill=color)
                pen.rectangle((x0, y0 + r, x1, y1 - r), fill=color)
                if r:
                    for cx, cy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
                        pen.ellipse((cx, cy, cx + 2 * r, cy + 2 * r), fill=color)

            rounded(0, radius, self._rgb(border))
            rounded(line, radius - line, self._rgb(fill))
            smooth = getattr(Image, "Resampling", Image).BOX
            image = ImageTk.PhotoImage(canvas.resize((width, height), smooth), master=self)
            self._images[key] = image
        return image

    def _draw(self) -> None:
        """Work out the sizes and colours, then draw whichever rows are in view."""
        self.delete("all")
        self._hover = None
        self._drawn = range(0)
        px = self._px
        family = ctk.CTkFont().cget("family")
        self._font = tkfont.Font(root=self, family=family,
                                 size=round(-self.LABEL_FONT_SIZE * self._scale))
        self._row_h = px(self.ROW_HEIGHT)
        self._pad = px(self.ROW_PAD)
        self._pitch = self._row_h + 2 * self._pad
        background = self._color(self._bg_spec)
        self._ink = self._color(ctk.ThemeManager.theme["CTkLabel"]["text_color"])

        # Placed the way a label centres its text, so rows sit on the same pixels.
        self._text_top = self._pad + (self._row_h - self._font.metrics("linespace")) // 2

        total = len(self._labels) * self._pitch
        self.configure(height=max(1, total), bg=background)
        if self._button_text:
            self._button_font = tkfont.Font(
                root=self, family=family,
                size=round(-abs(ctk.ThemeManager.theme["CTkFont"]["size"]) * self._scale))
            self._button_w = int(self.BUTTON_WIDTH * self._scale)
            self._button_h = int(self.BUTTON_HEIGHT * self._scale)
            self._button_x = left = max(0, self.winfo_width() - self._button_w)
            # Hides the end of a long name where it would run under the buttons.
            self.create_rectangle(left - px(self.BUTTON_GAP), 0, left + self._button_w + 1, total,
                                  fill=background, width=0, tags=("btn", "strip"))
            self._button_ink = self._color(("black", "white"))
            self._caption_top = (self._button_h - self._button_font.metrics("linespace")) // 2
        if not self._waiting_for_layout:
            self.show_visible()

    def _rows_in_view(self) -> range:
        total = len(self._labels)
        scroll = self._scroll
        if scroll is None:
            return range(total)
        try:
            offset, widget = 0, self
            while widget is not scroll:
                offset += widget.winfo_y()
                widget = widget.master
                if widget is None:
                    return range(total)
            viewport = scroll._parent_canvas
            height = viewport.winfo_height()
            if height <= 1:                         # the list has never been on screen yet
                height = self.winfo_screenheight()
            top = viewport.canvasy(0) - offset
        except Exception:
            return range(total)
        first = max(0, int(top // self._pitch) - self.OVERSCAN)
        last = min(total, int((top + height) // self._pitch) + 1 + self.OVERSCAN)
        return range(first, last) if last > first else range(0)

    def show_visible(self) -> None:
        """Bring the drawn rows in line with what is on screen now."""
        self._waiting_for_layout = False
        try:
            wanted = self._rows_in_view()
            drawn = self._drawn
            if wanted == drawn:
                return
            if not drawn or not wanted or wanted.start >= drawn.stop or wanted.stop <= drawn.start:
                if drawn:
                    self.delete("row")
                fresh = wanted
            else:
                for index in range(drawn.start, wanted.start):
                    self.delete(f"r{index}")
                for index in range(wanted.stop, drawn.stop):
                    self.delete(f"r{index}")
                fresh = [*range(wanted.start, drawn.start), *range(drawn.stop, wanted.stop)]
            self._drawn = wanted
            if fresh:
                self._draw_rows(fresh)
        except tk.TclError:
            pass

    def _draw_rows(self, indexes) -> None:
        labels, pitch = self._labels, self._pitch
        for index in indexes:
            self.create_text(0, index * pitch + self._text_top, text=labels[index], anchor="nw",
                             font=self._font, fill=self._ink, tags=("row", "label", f"r{index}"))
        if not self._button_text:
            return
        self.tag_lower("label", "strip")
        normal = self._button_image(self._color(self._bg_spec))
        left, half = self._button_x, self._button_w // 2
        for index in indexes:
            top = self._button_top(index)
            image = normal
            if index == self._hover:
                image = self._button_image(
                    self._color(ctk.ThemeManager.theme["CTkButton"]["hover_color"]))
            self.create_image(left, top, image=image, anchor="nw",
                              tags=("row", "btn", f"r{index}", f"b{index}"))
            self.create_text(left + half, top + self._caption_top,
                             text=self._button_text, anchor="n", font=self._button_font,
                             fill=self._button_ink, tags=("row", "btn", f"r{index}"))

    def _button_top(self, index: int) -> int:
        return index * self._pitch + self._pad + (self._row_h - self._button_h) // 2

    def button_center(self, index: int) -> tuple:
        return (self._button_x + self._button_w // 2,
                self._button_top(index) + self._button_h // 2)

    def _on_resize(self, event) -> None:
        if not self._button_text:
            return
        left = max(0, event.width - self._button_w)
        if left != self._button_x:
            self.move("btn", left - self._button_x, 0)
            self._button_x = left

    def _button_at(self, x: int, y: int):
        if not (self._button_x <= x < self._button_x + self._button_w):
            return None
        index = int(y // self._pitch)
        if not 0 <= index < len(self._labels):
            return None
        top = self._button_top(index)
        return index if top <= y < top + self._button_h else None

    def _set_hover(self, index) -> None:
        if index == self._hover:
            return
        try:
            if self._hover is not None:
                self.itemconfigure(f"b{self._hover}",
                                   image=self._button_image(self._color(self._bg_spec)))
            if index is not None:
                hover = self._color(ctk.ThemeManager.theme["CTkButton"]["hover_color"])
                self.itemconfigure(f"b{index}", image=self._button_image(hover))
            self.configure(cursor="hand2" if index is not None else "")
        except tk.TclError:
            pass
        self._hover = index

    def _on_motion(self, event) -> None:
        self._set_hover(self._button_at(event.x, event.y))

    def _on_click(self, event):
        index = self._button_at(event.x, event.y)
        if index is not None and self._on_button is not None:
            self._on_button(index)
        return "break"


def build_game_rows(parent, labels, button_text: str = "", on_button=None, scroll=None) -> None:
    """Fill `parent` with the game rows, one canvas per MAX_ROWS games.

    `scroll` is the scrollable list the rows live in; with it, only the rows on
    screen are drawn.
    """
    step = GameRowsCanvas.MAX_ROWS
    for start in range(0, len(labels), step):
        handler = None
        if on_button is not None:
            handler = lambda index, offset=start: on_button(offset + index)
        GameRowsCanvas(parent, labels[start:start + step], button_text=button_text,
                       on_button=handler, grid_row=start // step, scroll=scroll)


DOWNLOAD_QUEUE_ROWS = 15     # queued downloads that get a row on the Downloads tab ahead of time
DOWNLOAD_PAGE_SIZE = 15      # downloaded files listed per page once there are more than this
DOWNLOAD_ROW_SLACK = 0      # just-finished rows allowed to linger before the page is tidied

DOWNLOAD_AHEAD_KEY = "download_ahead"    # settings.json: download the next game while this one extracts
DOWNLOAD_AHEAD_LIMIT = 5    # finished downloads allowed to wait for their turn before downloading pauses


class QueuedDownload:
    """One queued download.

    It only gets a row on the Downloads tab when it is near the front of the
    queue. A row is about 25 native windows and Windows allows a program 10,000,
    so a row per queued item made a very large queue freeze and then crash.
    """

    __slots__ = ("entry", "conv_mode", "cancel_event", "row", "ready", "started", "done")

    def __init__(self, entry, conv_mode):
        self.entry = entry
        self.conv_mode = conv_mode              # what to do once downloaded: auto / extract / keep
        self.cancel_event = threading.Event()
        self.row = None
        self.ready = threading.Event()
        self.started = False
        self.done = False


class ArchiveGUI(ctk.CTk):
    def report_callback_exception(self, exc_type, exc, tb):
        # Errors in button clicks / timers: log them (Logs tab + log file) instead of losing them.
        import traceback
        log_event("UNEXPECTED ERROR in the window:\n"
                  + "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())

    def __init__(self, index: Index):
        super().__init__()
        self.title("Bman Retro Downloader")
        self.geometry("1050x700")

        self.index = index
        self.downloader = None
        try:
            self.downloader = Downloader(self.index)
        except Exception:
            pass

        self.download_queue = queue.Queue()
        self.is_downloading = False
        self._dl_waiting = deque()
        self._dl_live: list = []
        self._dl_footer = None
        self._dl_page = 0
        self._dl_paged = False
        self._dl_page_names: list = []
        self._dl_total = 0              # downloaded files on disk, as of the last refresh
        self._dl_pages = 1
        self._dl_view = None            # "active" / "finished" while downloads run, "all" when idle
        self._dl_refresh_after = None
        self._dl_settle_pending = False
        self._pending_update = None
        self._update_checking = False
        self._update_installing = False
        self._last_input = 0.0          # when the mouse or keyboard was last used (time.monotonic)
        self._queue_start_lock = threading.Lock()
        # Downloading and the work after it (extracting) are two
        # stages. Each handles one game at a time; the "download ahead" setting
        # decides whether the next download may start while the stage after it
        # is still busy with an earlier game.
        self.download_ahead = bool(load_settings().get(DOWNLOAD_AHEAD_KEY, False))
        self._dl_thread_running = False
        self._post_queue = queue.Queue()
        self._post_cond = threading.Condition()
        self._post_pending = 0          # downloaded, extraction not finished yet
        self._post_thread = None
        self._api_locked = False
        self._api_dialog_open = False
        self._setting_save_jobs = {}

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(9, weight=1)

        # Two lines: on one line the title is wider than "Bman Downloader" and stretches the sidebar.
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Bman Retro\nDownloader", justify="center",
                                       font=ctk.CTkFont(size=18, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        self.sidebar_btn_library = ctk.CTkButton(self.sidebar_frame, text="Library / Search", command=lambda: self.select_tab("library"))
        self.sidebar_btn_library.grid(row=1, column=0, padx=20, pady=10)

        self.sidebar_btn_downloads = ctk.CTkButton(self.sidebar_frame, text="Downloads", command=lambda: self.select_tab("downloads"))
        self.sidebar_btn_downloads.grid(row=2, column=0, padx=20, pady=10)

        self.sidebar_btn_collections = ctk.CTkButton(self.sidebar_frame, text="Collections", command=lambda: self.select_tab("collections"))
        self.sidebar_btn_collections.grid(row=3, column=0, padx=20, pady=10)

        self.sidebar_btn_community = ctk.CTkButton(self.sidebar_frame, text="Community Collections",
                                                   command=lambda: self.select_tab("community"))
        self.sidebar_btn_community.grid(row=4, column=0, padx=20, pady=10)

        self.sidebar_btn_logs = ctk.CTkButton(self.sidebar_frame, text="Logs", command=lambda: self.select_tab("logs"))
        self.sidebar_btn_logs.grid(row=5, column=0, padx=20, pady=10)

        self.sidebar_btn_settings = ctk.CTkButton(self.sidebar_frame, text="Settings", command=lambda: self.select_tab("settings"))
        self.sidebar_btn_settings.grid(row=6, column=0, padx=20, pady=10)

        self._setup_app_switch_button(row=7, target="main", text="Bman Downloader")

        self._bulk_running = False
        self._stop_for_switch = False     # set when the user switches apps mid-work
        self.bulk_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.bulk_frame.grid(row=8, column=0, padx=20, pady=(10, 0), sticky="ew")

        ctk.CTkLabel(self.bulk_frame, text="Bulk Actions", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="gray").pack(anchor="w", pady=(0, 4))

        self.bulk_extract_btn = ctk.CTkButton(self.bulk_frame, text="Extract All", fg_color=accent_fg(),
                                              hover_color=accent_hover(), command=self.start_bulk_extract)
        self.bulk_extract_btn.pack(fill="x")

        self.open_downloads_btn = ctk.CTkButton(self.bulk_frame, text="Open Downloads Folder",
                                                fg_color="transparent", border_width=1,
                                                text_color=("black", "white"),
                                                command=self.open_downloads_folder)
        self.open_downloads_btn.pack(fill="x", pady=(6, 0))

        self.bulk_status_lbl = ctk.CTkLabel(self.bulk_frame, text="", text_color="gray",
                                            font=ctk.CTkFont(size=11), wraplength=160,
                                            justify="left", anchor="w")
        self.bulk_status_lbl.pack(fill="x", pady=(6, 0))

        self.bulk_frame.grid_remove()
        self.after(10, self._lock_sidebar_width)

        self.update_banner_btn = ctk.CTkButton(
            self.sidebar_frame, text="Update Available!", fg_color="#2b8a3e",
            hover_color="#236c31", command=self.open_update_settings,
        )
        self.update_banner_btn.grid(row=10, column=0, padx=20, pady=(10, 20))
        self.update_banner_btn.grid_remove()

        self.content_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.content_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.content_frame.grid_rowconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(0, weight=1)

        self.current_tab = "library"

        self.setup_library_view()
        self.setup_downloads_view()
        self.setup_collections_view()
        self.setup_community_view()
        self.setup_logs_view()
        self.setup_settings_view()

        sys.stdout = TextRedirector(self.log_textbox, sys.stdout)
        sys.stderr = TextRedirector(self.log_textbox, sys.stderr)
        self._drain_log_queue()
        self._pump_ui_tick()

        # Nothing is downloading yet, so anything still in downloads/incomplete was
        # cut off when the app last closed or crashed.
        clean_incomplete_downloads()

        self.select_tab("library")

        for sequence in ("<KeyPress>", "<ButtonPress>", "<MouseWheel>", "<Motion>"):
            self.bind_all(sequence, self._note_input, add="+")
        self.bind_all("<Control-a>", self._on_select_all_key)
        self.bind_all("<Control-A>", self._on_select_all_key)

        self.after(1500, lambda: self._start_update_check(manual=False))

        APP_WINDOW["ref"] = self
        self.after(2200, self._check_archive_support_at_startup)

        self.after(250, self._enforce_api_keys)
        self.after(3000, self._prepare_lists)
        threading.Thread(target=cleanup_update_leftovers, daemon=True,
                         name="update-cleanup").start()


    # ----- switching between Bman Downloader and the Retro Downloader -----------
    def _lock_sidebar_width(self) -> None:
        """Bulk Actions only shows on the Downloads tab, and its "Open Downloads Folder"
        button is the widest thing in the sidebar. Keep the sidebar as wide as it is with
        Bulk Actions showing, so the page doesn't shift when that tab opens."""
        try:
            hidden = not self.bulk_frame.winfo_manager()
            if hidden:
                self.bulk_frame.grid()
            self.sidebar_frame.update_idletasks()
            width = self.sidebar_frame.winfo_reqwidth()
            if hidden:
                self.bulk_frame.grid_remove()
            self.grid_columnconfigure(0, minsize=width)
        except Exception:
            pass

    def _setup_app_switch_button(self, row: int, target: str, text: str) -> None:
        """The button under Settings that swaps this window to the other app."""
        try:
            import bman_switch
            if not bman_switch.available(target):
                return
        except Exception:
            return
        self.app_switch_btn = ctk.CTkButton(
            self.sidebar_frame, text=text, fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=lambda: self.switch_app_action(target))
        self.app_switch_btn._accent_locked = True
        self.app_switch_btn.grid(row=row, column=0, padx=20, pady=(14, 10))

    def switch_app_action(self, target: str) -> None:
        import bman_switch
        bman_switch.request_switch(
            self, target,
            notify=lambda title, text: show_info_popup(self, title, text),
            confirm=lambda title, text: ask_confirm_popup(
                self, title, text, confirm_text="Switch anyway", cancel_text="Stay"))

    def stop_work_for_switch(self) -> None:
        """The user chose to switch apps with work still running: stop what can be stopped.

        Downloads (running or queued) are cancelled and bulk actions stop after the
        game they are on. An extraction / conversion / upload that already started
        can't be cut off safely, so it finishes on its own thread; bman_switch keeps
        this app from being reopened until it has.
        """
        self._stop_for_switch = True
        jobs = list(self._dl_waiting) + list(self._dl_live)
        for q in (self.download_queue, self._post_queue):
            try:
                with q.mutex:
                    items = list(q.queue)
            except Exception:
                items = []
            for item in items:
                jobs.append(item[0] if isinstance(item, tuple) else item)
        cancelled = 0
        for job in jobs:
            event = getattr(job, "cancel_event", None)
            if event is not None and not event.is_set() and not getattr(job, "done", False):
                event.set()
                cancelled += 1
        if cancelled:
            log_event(f"Switching apps: cancelled {cancelled} download(s).")
        if self._bulk_running:
            log_event("Switching apps: the bulk action stops after the game it is on.")

    def _check_archive_support_at_startup(self):
        # .7z/.rar are unpacked in-process (py7zr / unrar2-cffi): no 7-Zip, nothing to install.
        try:
            for kind, problem in archive_support().items():
                if problem:
                    log_event(f"Archive support: .{kind} downloads cannot be extracted - {problem}")
        except Exception as exc:
            log_event(f"Archive support check failed: {exc}")

    def _pump_ui_tick(self):
        try:
            for _key, (func, args, kwargs) in _drain_ui_pending():
                try:
                    func(*args, **kwargs)
                except tk.TclError:
                    pass
                except Exception as exc:
                    print(f"UI update failed: {exc}")
        finally:
            try:
                self.after(UI_PUMP_MS, self._pump_ui_tick)
            except Exception:
                pass

    def _drain_log_queue(self):
        chunks = []
        try:
            while True:
                chunks.append(LOG_QUEUE.get_nowait())
        except queue.Empty:
            pass

        if chunks:
            try:
                self.log_textbox.configure(state="normal")
                self.log_textbox.insert("end", "".join(chunks))
                self.log_textbox.see("end")
                self.log_textbox.configure(state="disabled")
            except Exception:
                pass

        self.after(150, self._drain_log_queue)


    def _enforce_api_keys(self, show_popup: bool = True) -> bool:
        if has_archive_keys():
            self._set_locked(False)
            return True
        self._set_locked(True)
        if show_popup:
            self.show_api_keys_dialog()
        return False

    def _set_locked(self, locked: bool) -> None:
        self._api_locked = locked
        state = "disabled" if locked else "normal"
        for btn in (getattr(self, "sidebar_btn_library", None),
                    getattr(self, "sidebar_btn_downloads", None),
                    getattr(self, "sidebar_btn_collections", None),
                    getattr(self, "sidebar_btn_community", None)):
            try:
                btn.configure(state=state)
            except Exception:
                pass
        if locked:
            try:
                self.select_tab("settings")
            except Exception:
                pass

    def show_api_keys_dialog(self) -> None:
        if getattr(self, "_api_dialog_open", False):
            return
        self._api_dialog_open = True
        try:
            ApiKeysRequiredDialog(self)
        except Exception as exc:
            log_event(f"Could not show the API key prompt: {exc}")
            self._api_dialog_open = False

    def open_api_key_settings(self) -> None:
        self._api_dialog_open = False
        try:
            self.select_tab("settings", force=True)
            self.update_idletasks()
            self._scroll_settings_to(getattr(self, "api_keys_header_lbl", None))
            self.s3_access_entry.focus_set()
        except Exception as exc:
            log_event(f"Could not focus the API key settings: {exc}")

    def _scroll_settings_to(self, widget) -> None:
        if widget is None:
            return
        try:
            self.settings_view.update_idletasks()
            canvas = self.settings_view._parent_canvas
            total = max(self.settings_view.winfo_height(), 1)
            fraction = min(max(widget.winfo_y() / total, 0.0), 1.0)
            canvas.yview_moveto(fraction)
        except Exception:
            pass

    def require_api_keys(self) -> bool:
        if has_archive_keys():
            return True
        self._enforce_api_keys(show_popup=True)
        return False

    def select_tab(self, name: str, force: bool = False):
        if getattr(self, "_api_locked", False) and not force and name in (
                "library", "downloads", "collections", "community"):
            self.show_api_keys_dialog()
            return

        self.current_tab = name

        for frame in (self.library_view, self.downloads_view, self.collections_view,
                      self.community_view, self.logs_view, self.settings_view):
            frame.grid_forget()

        if name == "downloads":
            self.bulk_frame.grid()
        else:
            self.bulk_frame.grid_remove()

        if name == "library":
            self.library_view.grid(row=0, column=0, sticky="nsew")
        elif name == "downloads":
            self.downloads_view.grid(row=0, column=0, sticky="nsew")
            self.refresh_downloaded_items()
        elif name == "collections":
            self.collections_view.grid(row=0, column=0, sticky="nsew")
            self.refresh_collections_view()
        elif name == "community":
            self.community_view.grid(row=0, column=0, sticky="nsew")
            self.open_community_tab()
        elif name == "logs":
            self.logs_view.grid(row=0, column=0, sticky="nsew")
        elif name == "settings":
            self.settings_view.grid(row=0, column=0, sticky="nsew")

    def _on_select_all_key(self, event=None):
        if getattr(self, "current_tab", "") != "library":
            return None
        widget = getattr(event, "widget", None)
        if isinstance(widget, (tk.Entry, tk.Text)):
            return None
        try:
            self.results_list.select_all()
        except Exception:
            return None
        return "break"

    def setup_library_view(self):
        self.library_view = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.library_view.grid_rowconfigure(2, weight=1)
        self.library_view.grid_columnconfigure(0, weight=1)

        search_row = ctk.CTkFrame(self.library_view, fg_color="transparent")
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        search_row.grid_columnconfigure(0, weight=1)

        saved = load_settings()

        self.console_var = ctk.StringVar(value=ALL_CONSOLES_LABEL)
        self.console_menu = ctk.CTkOptionMenu(
            search_row, values=self._console_menu_values(), variable=self.console_var,
            width=190, command=self.on_console_change,
        )
        self.console_menu.grid(row=0, column=0, padx=(0, 10))
        saved_console = saved.get(CONSOLE_SETTING_KEY)
        if saved_console in self._console_menu_values():
            self.console_var.set(saved_console)

        self.search_entry = ctk.CTkEntry(
            search_row, placeholder_text="Search games - or pick a console and press Search to list them all")
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self.search_entry.bind("<Return>", lambda e: self.perform_search())
        search_row.grid_columnconfigure(0, weight=0)
        search_row.grid_columnconfigure(1, weight=1)

        search_btn = ctk.CTkButton(search_row, text="Search", width=100, command=self.perform_search)
        search_btn.grid(row=0, column=2)

        saved_region = saved.get(REGION_SETTING_KEY, REGION_FILTER_DEFAULT)
        if saved_region not in REGION_FILTER_LABELS:
            saved_region = REGION_FILTER_DEFAULT
        self.region_var = ctk.StringVar(value=saved_region)

        region_lbl = ctk.CTkLabel(search_row, text="Region:", font=ctk.CTkFont(size=12))
        region_lbl.grid(row=0, column=3, padx=(12, 6))

        self.region_menu = ctk.CTkOptionMenu(
            search_row, values=REGION_FILTER_LABELS, variable=self.region_var,
            width=175, command=self.on_region_change,
        )
        self.region_menu.grid(row=0, column=4)


        self.details_frame = ctk.CTkFrame(self.library_view, height=140)
        self.details_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.details_frame.grid_columnconfigure(1, weight=1)

        self.cover_label = ctk.CTkLabel(self.details_frame, text="No Cover", width=120, height=120, fg_color=("gray85", "gray25"))
        self.cover_label.grid(row=0, column=0, rowspan=4, padx=10, pady=10)

        self.game_title_lbl = ctk.CTkLabel(self.details_frame, text="Select an item to view details", font=ctk.CTkFont(size=16, weight="bold"))
        self.game_title_lbl.grid(row=0, column=1, sticky="w", padx=10, pady=(10, 0))

        self.game_meta_lbl = ctk.CTkLabel(self.details_frame, text="", font=ctk.CTkFont(size=12))
        self.game_meta_lbl.grid(row=1, column=1, sticky="w", padx=10, pady=(0, 10))

        # height=1: an empty CTkFrame is 200px tall by default, which stretched this
        # panel when nothing was selected. The row keeps one line of space either way.
        self.action_sub = ctk.CTkFrame(self.details_frame, fg_color="transparent", width=1, height=1)
        self.action_sub.grid(row=2, column=1, sticky="w", padx=10)
        try:
            row_scale = ctk.ScalingTracker.get_widget_scaling(self.details_frame)
        except Exception:
            row_scale = 1.0
        self.details_frame.grid_rowconfigure(2, minsize=round(28 * row_scale))   # grid minsize is raw pixels

        self.conversion_var = ctk.StringVar(value=saved_post_mode())
        self.conversion_widgets = []
        
        action_row = ctk.CTkFrame(self.details_frame, fg_color="transparent")
        action_row.grid(row=3, column=1, sticky="w", padx=10, pady=(5, 10))

        self.download_btn = ctk.CTkButton(action_row, text="Download Item", state="disabled", command=self.start_selected_download)
        self.download_btn.pack(side="left")

        self.add_collection_btn = ctk.CTkButton(
            action_row, text="+ Add to Collection", state="disabled", width=160,
            fg_color="#7a4bbd", hover_color="#623a99", command=self.add_selected_to_collection,
        )
        self.add_collection_btn._accent_locked = True
        self.add_collection_btn.pack(side="left", padx=(8, 0))

        self.collection_hint_lbl = ctk.CTkLabel(action_row, text="", text_color="#00FF40",
                                                font=ctk.CTkFont(size=11))
        self.collection_hint_lbl.pack(side="left", padx=(10, 0))

        self.results_list = VirtualResultsList(
            self.library_view, label_text="Search Results",
            on_selection_change=self._on_results_selection,
            on_activate=lambda entry: self.start_selected_download(),
        )
        self.results_list.grid(row=2, column=0, sticky="nsew")

        self.current_results: list[Files] = []
        self.selected_file: Files | None = None
        self.selected_files: list[Files] = []
        self.after(400, self._show_library_intro)

    def _console_menu_values(self) -> list:
        present = self.index.consoles_present() if self.index is not None else []
        keys = present or list(CONSOLE_KEYS)
        return [ALL_CONSOLES_LABEL] + [console_display_name(k) for k in keys]

    def _refresh_console_menu(self) -> None:
        values = self._console_menu_values()
        try:
            self.console_menu.configure(values=values)
            if self.console_var.get() not in values:
                self.console_var.set(ALL_CONSOLES_LABEL)
        except Exception:
            pass

    def _show_library_intro(self) -> None:
        """What the results list says before the first search."""
        if self.current_results:
            return
        if self.index is not None and self.index.load_error:
            text = (self.index.load_error.replace("\n\n", "  ")
                    + "  Then press Reload sources in Settings.")
        elif self.index is None or not self.index.sources:
            text = (f"No sources yet - add archive.org items to {SOURCES_FILE} next to the .py files, "
                    f"then press Reload sources." if sources_editable()
                    else "This version has no game sources built in. Check for an update in Settings.")
        elif not self.index.entries:
            text = "None of the game sources could be loaded - see the Logs tab."
        else:
            counts = {}
            for entry in self.index.entries:
                counts[entry.console] = counts.get(entry.console, 0) + 1
            parts = [f"{console_display_name(k)} {counts[k]:,}" for k in CONSOLE_KEYS if k in counts]
            text = (f"{len(self.index.entries):,} games indexed - " + ", ".join(parts)
                    + ".  Type a name, or pick a console and press Search to list everything.")
            if self.index.failed:
                text += f"  ({len(self.index.failed)} source(s) failed to load - see Logs.)"
        try:
            self.results_list.set_items([], [], empty_text=text)
        except Exception:
            pass

    def on_console_change(self, _choice=None):
        data = load_settings()
        data[CONSOLE_SETTING_KEY] = self.console_var.get()
        save_settings(data)

        # New console = fresh search: clear the box and list that console's games
        self.search_entry.delete(0, "end")
        try:
            self.search_entry._activate_placeholder()   # show the hint text again
        except Exception:
            pass
        self.perform_search()
        self._reset_results_scroll()

    def _reset_results_scroll(self):
        try:
            self.results_list.scroll_to_top()
        except Exception:
            pass

    def perform_search(self):
        query = self.search_entry.get().strip()
        console = console_key_for_label(self.console_var.get())
        if not query and console is None:
            self.current_results = []
            self._show_library_intro()
            return

        matches = self.index.search(query, console) if query else self.index.browse(console)

        region_label = self.region_var.get()
        wanted_regions = region_filter_for_label(region_label)
        hidden_region = 0
        if wanted_regions:
            filtered = [e for e in matches if entry_matches_region(e, wanted_regions)]
            hidden_region = len(matches) - len(filtered)
            matches = filtered

        notes = []
        if hidden_region:
            notes.append(f"{hidden_region} result{'s' if hidden_region != 1 else ''} "
                         f"hidden by the '{region_label}' filter")

        self.current_results = matches

        empty_text = ""
        if not self.current_results:
            empty_text = "No matching games found."
            if console is not None:
                empty_text += f"  (Only {console_display_name(console)} was searched.)"
            if notes:
                empty_text += "  (" + ", ".join(notes) + ".)"
                if hidden_region:
                    empty_text += f"  Set Region to '{REGION_FILTER_ALL}' to see everything."
            notes = []

        labels = [
            f"[{console_display_name(entry.console)}] {entry_title(entry)}  "
            f"({format_size(entry.size)})"
            for entry in self.current_results
        ]

        self.results_list.set_items(
            self.current_results, labels,
            note=(", ".join(notes) + "." if notes else ""),
            empty_text=empty_text,
        )

    def on_region_change(self, _choice=None):
        data = load_settings()
        data[REGION_SETTING_KEY] = self.region_var.get()
        save_settings(data)
        self.perform_search()
        self._reset_results_scroll()

    def _on_results_selection(self, entries: list, primary):
        self.selected_files = list(entries)
        if not entries:
            self.selected_file = None
            self._show_no_selection()
        elif len(entries) == 1:
            self.select_game_item(entries[0])
        else:
            self.selected_file = primary or entries[0]
            self._show_multi_selection(entries)

    def _clear_conversion_widgets(self):
        for widget in self.conversion_widgets:
            try:
                widget.destroy()
            except Exception:
                pass
        self.conversion_widgets.clear()

    def _cancel_pending_cover(self):
        self._cover_token = getattr(self, "_cover_token", 0) + 1
        return self._cover_token

    def _clear_cover(self):
        if getattr(self.cover_label, "image", None) is None and self.cover_label.cget("text") == "No Cover":
            return
        self.cover_label.configure(image="", text="No Cover")
        self.cover_label.image = None

    def _show_no_selection(self):
        self._cancel_pending_cover()
        self._clear_conversion_widgets()
        self.game_title_lbl.configure(text="Select an item to view details")
        self.game_meta_lbl.configure(text="")
        self.download_btn.configure(text="Download Item", state="disabled")
        try:
            self.add_collection_btn.configure(text="+ Add to Collection", state="disabled")
            self.collection_hint_lbl.configure(text="")
        except Exception:
            pass
        self._clear_cover()

    def _show_multi_selection(self, entries: list):
        self._cancel_pending_cover()
        self._clear_conversion_widgets()

        count = len(entries)
        known = [e.size for e in entries if e.size]
        total = sum(known) if known else None
        size_text = format_size(total) if total else "unknown size"
        if known and len(known) != count:
            size_text += f" (from the {len(known)} of {count} that report one)"

        consoles = sorted({console_display_name(e.console) for e in entries})
        console_text = consoles[0] if len(consoles) == 1 else f"{len(consoles)} consoles"

        self.game_title_lbl.configure(text=f"{count:,} games selected")
        self.game_meta_lbl.configure(text=f"Console: {console_text} | Total size: {size_text}")

        note = ctk.CTkLabel(
            self.action_sub,
            text="What to do after downloading is asked once, for the whole selection.",
            text_color="#00FF40",
        )
        note.pack(side="left", padx=5)
        self.conversion_widgets.append(note)

        self.download_btn.configure(text=f"Download {count:,} Games", state="normal")
        try:
            self.add_collection_btn.configure(text=f"+ Add {count:,} to Collection", state="normal")
            self.collection_hint_lbl.configure(text="")
        except Exception:
            pass
        self._clear_cover()

    def select_game_item(self, entry: Files):
        self.selected_file = entry
        self.selected_files = [entry]
        self.download_btn.configure(text="Download Item")
        try:
            self.add_collection_btn.configure(text="+ Add to Collection")
        except Exception:
            pass
        self.game_title_lbl.configure(text=entry_title(entry))
        self.game_meta_lbl.configure(
            text=f"{console_display_name(entry.console)} | Size: {format_size(entry.size)} | "
                 f"Source: {entry.archive_identifier} | Date: {entry.date or 'N/A'}")
        self.download_btn.configure(state="normal")
        try:
            self.add_collection_btn.configure(state="normal")
            self.collection_hint_lbl.configure(text="")
        except Exception:
            pass

        for widget in self.conversion_widgets:
            widget.destroy()
        self.conversion_widgets.clear()

        lbl = ctk.CTkLabel(self.action_sub, text="After download:")
        lbl.pack(side="left", padx=(0, 5))
        self.conversion_widgets.append(lbl)
        modes = ((POST_MODE_AUTO, "Auto"), (POST_MODE_EXTRACT, "Extract"),
                 (POST_MODE_KEEP, "Keep zipped"))
        for i, (value, text) in enumerate(modes):
            # width=0: CTkRadioButton is 100px wide by default, which padded short labels out.
            rb = ctk.CTkRadioButton(self.action_sub, text=text, variable=self.conversion_var,
                                    value=value, command=self._on_post_mode_pick, width=0)
            rb.pack(side="left", padx=((5, 12) if i == 0 else (0, 12)))
            self.conversion_widgets.append(rb)
        auto_does = "extracts" if console_is_disc(entry.console) else "keeps it zipped"
        hint = ctk.CTkLabel(self.action_sub, text=f"(Auto {auto_does} for {console_display_name(entry.console)})",
                            text_color="gray", font=ctk.CTkFont(size=11))
        hint.pack(side="left", padx=5)
        self.conversion_widgets.append(hint)

        token = self._cancel_pending_cover()
        threading.Thread(target=self.fetch_cover_art, args=(entry_title(entry), token), daemon=True).start()

    def _on_post_mode_pick(self):
        save_post_mode(self.conversion_var.get())

    def fetch_cover_art(self, game_name: str, token: int | None = None):
        def stale() -> bool:
            return token is not None and token != getattr(self, "_cover_token", token)

        session = curl_cffi_requests.Session(impersonate="chrome124")
        try:
            # "Super Mario Bros. 3 (USA) (Rev 1).zip" -> "Super Mario Bros. 3"
            clean_name = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", Path(game_name).stem).replace("_", " ").strip()
            headers = {"Authorization": f"Bearer {STEAMGRIDDB_API_KEY}"}
            
            search_url = f"{STEAMGRIDDB_BASE_URL}/search/autocomplete/{quote(clean_name)}"
            resp = session.get(search_url, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("data"):
                    game_id = data["data"][0]["id"]
                    
                    grids_url = f"{STEAMGRIDDB_BASE_URL}/grids/game/{game_id}"
                    grids_resp = session.get(grids_url, headers=headers, timeout=10)
                    
                    if grids_resp.status_code == 200:
                        grids_data = grids_resp.json()
                        if grids_data.get("success") and grids_data.get("data"):
                            image_url = grids_data["data"][0]["url"]
                            img_resp = session.get(image_url, timeout=10)
                            
                            if img_resp.status_code == 200:
                                img_data = img_resp.content
                                def _set_cover(data=img_data):
                                    if stale():
                                        return
                                    ctk_img = ImageTk.PhotoImage(Image.open(io.BytesIO(data)).resize((100, 100)))
                                    self.cover_label.configure(image=ctk_img, text="")
                                    self.cover_label.image = ctk_img
                                post_ui(_set_cover)
                                return
        except Exception:
            pass
        finally:
            session.close()

        def _no_cover():
            if stale():
                return
            self._clear_cover()

        post_ui(_no_cover)

    def _settle_downloads_soon(self) -> None:
        if self._dl_settle_pending:
            return
        self._dl_settle_pending = True

        def run():
            self._dl_settle_pending = False
            settle_scrollable(self.downloads_scroll)

        try:
            self.after_idle(run)
        except Exception:
            self._dl_settle_pending = False

    def _download_rows(self) -> list:
        # Every row, oldest first - including the ones the checkbox has hidden for now.
        return [w for w in self.downloads_scroll.winfo_children() if isinstance(w, DownloadItemFrame)]

    def _live_download_rows(self) -> set:
        return {job.row for job in self._dl_live if not job.done and job.row is not None}

    @staticmethod
    def _download_row_idle(row, live_rows) -> bool:
        return not (row in live_rows or row.is_active or getattr(row, "is_working", False))

    def _destroy_download_row(self, row) -> None:
        for slot in ("dl-progress", "dl-status", "row-status"):
            drop_ui_slot((slot, id(row)))
        try:
            row.destroy()
        except Exception:
            pass

    def _downloads_running(self) -> bool:
        """True while the queue still has something to download or extract."""
        for job in self._dl_live:
            if not job.done and not (job.cancel_event.is_set() and not job.started):
                return True
        return any(not job.done and not job.cancel_event.is_set() for job in self._dl_waiting)

    def _downloads_view_mode(self) -> str:
        """Which rows the tab lists.

        While downloads are running the checkbox splits the tab in two: "active"
        (the queue) or "finished" (the downloaded files). Once the queue is
        empty there is nothing to split and the tab goes back to "all".
        """
        if not self._downloads_running():
            return "all"
        return "finished" if self.downloads_finished_var.get() else "active"

    def _apply_downloads_view(self, mode: str) -> None:
        """Match the checkbox, the list title and the pager to the view."""
        if mode == self._dl_view:
            return
        previous, self._dl_view = self._dl_view, mode
        if mode == "all":
            # Next time something is queued the tab opens on the queue again.
            self.downloads_finished_var.set(False)
            self.downloads_finished_check.grid_remove()
        else:
            self.downloads_finished_check.grid()
        title = {"active": "Active Downloads",
                 "finished": "Finished Downloads"}.get(mode, "Active & Downloaded Files")
        self.downloads_scroll.configure(label_text=title)
        self._update_downloads_pager(self._dl_total, self._dl_pages)
        if previous is not None:
            try:
                self.downloads_scroll._parent_canvas.yview_moveto(0.0)
            except Exception:
                pass

    def on_downloads_finished_toggle(self) -> None:
        self._arrange_download_rows()

    def _arrange_download_rows(self) -> None:
        """Show the rows that belong in the current view, in order, with the queue footer in its place.

        With few downloaded files nothing moves: rows stay in the order they were
        added and the footer sits last. Once the downloaded files are paged, the
        queue comes first so the active download is not buried under a full page.

        While downloads are running only one half is listed - the queue, or the
        finished files when the checkbox is ticked (see _downloads_view_mode).
        The other half is hidden, not destroyed, so switching is instant.
        """
        mode = self._downloads_view_mode()
        self._apply_downloads_view(mode)

        rows = self._download_rows()
        footer = [self._dl_footer] if self._dl_footer is not None else []
        live_rows = self._live_download_rows()
        busy = [r for r in rows if not self._download_row_idle(r, live_rows)]
        idle = [r for r in rows if self._download_row_idle(r, live_rows)]
        if self._dl_paged:
            position = {name: i for i, name in enumerate(self._dl_page_names)}
            recent = [r for r in idle if r.raw_filename not in position]
            on_page = sorted((r for r in idle if r.raw_filename in position),
                             key=lambda r: position[r.raw_filename])
            idle = recent + on_page

        if mode == "active":
            order = busy + footer
        elif mode == "finished":
            order = idle
        elif self._dl_paged:
            order = busy + footer + idle
        else:
            order = rows + footer

        wanted = set(order)
        hidden = False
        for widget in self.downloads_scroll.pack_slaves():
            if widget not in wanted and (widget in footer or isinstance(widget, DownloadItemFrame)):
                widget.pack_forget()
                hidden = True
        if hidden:
            self._settle_downloads_soon()   # the list just got shorter

        packed = [w for w in self.downloads_scroll.pack_slaves() if w in wanted]
        shown = set(packed)
        for widget in order:
            if widget not in shown:
                # A plain pack() call - CustomTkinter replays it when the scaling changes.
                widget.pack(fill="x", pady=5, padx=5)
                packed.append(widget)

        # Rows out of place are moved, never taken out and put back: un-packing a
        # row hides it until the next redraw, so re-packing everything below the
        # first change made the whole tab flash each time a download finished.
        # tk.Pack.pack_configure only changes the order; it leaves the options
        # CustomTkinter remembered from pack() alone.
        for index, widget in enumerate(order):
            if packed[index] is widget:
                continue
            packed.remove(widget)
            if index == 0:
                tk.Pack.pack_configure(widget, before=packed[0])
            else:
                tk.Pack.pack_configure(widget, after=order[index - 1])
            packed.insert(index, widget)

    def _update_queue_footer(self) -> None:
        hidden = len(self._dl_waiting)
        footer = self._dl_footer
        if not hidden:
            if footer is not None:
                self._dl_footer = None
                try:
                    footer.destroy()
                except Exception:
                    pass
            return
        if footer is None:
            footer = ctk.CTkFrame(self.downloads_scroll, fg_color="transparent")
            footer.grid_columnconfigure(0, weight=1)
            footer.count_lbl = ctk.CTkLabel(footer, text="", text_color="gray", anchor="w")
            footer.count_lbl.grid(row=0, column=0, sticky="w", padx=10)
            ctk.CTkButton(footer, text="Cancel All Queued", width=130, height=24,
                          fg_color="#b53737", hover_color="#8c2929",
                          command=self.cancel_queued_downloads).grid(row=0, column=1, sticky="e", padx=10)
            footer.pack(fill="x", pady=5, padx=5)
            self._dl_footer = footer
        footer.count_lbl.configure(
            text=f"+ {hidden:,} more in the queue - each one shows up here as the queue reaches it.")

    def _give_job_a_row(self, job) -> None:
        if job.row is not None or job.done:
            job.ready.set()
            return
        try:
            self._dl_waiting.remove(job)
        except ValueError:
            pass

        entry = job.entry
        row = None
        key = download_key(entry)
        for widget in self._download_rows():
            if widget.raw_filename == key:
                row = widget
                break

        if row is not None:
            row.entry = entry
            if row.is_active and row.cancel_event is not None and row in self._live_download_rows():
                job.cancel_event = row.cancel_event      # queued twice: both share the row's Cancel
            else:
                row.cancel_event = job.cancel_event
            row.is_active = True
        else:
            row = DownloadItemFrame(self.downloads_scroll, key, entry=entry,
                                    cancel_callback=job.cancel_event)
            row.pack(fill="x", pady=5, padx=5)

        job.row = row
        self._dl_live.append(job)
        job.ready.set()

    def _fill_queue_rows(self) -> None:
        """Give rows to the next few queued downloads, up to DOWNLOAD_QUEUE_ROWS at a time."""
        try:
            self._dl_live = [job for job in self._dl_live
                             if not job.done and not (job.cancel_event.is_set() and not job.started)]
            while self._dl_waiting and len(self._dl_live) < DOWNLOAD_QUEUE_ROWS:
                job = self._dl_waiting[0]
                if job.done or job.cancel_event.is_set():
                    self._dl_waiting.popleft()
                    continue
                self._give_job_a_row(job)
            self._update_queue_footer()
            self._arrange_download_rows()
        except tk.TclError:
            pass

    def _row_for_job(self, job):
        """Called by the download thread: the job's row, made on the UI thread if it has none yet."""
        for _ in range(120):
            if job.row is not None:
                return job.row
            post_ui(lambda j=job: (self._give_job_a_row(j), self._fill_queue_rows()))
            if job.ready.wait(1.0):
                return job.row
        return None

    def _after_download_job(self) -> None:
        self._fill_queue_rows()
        try:
            live_rows = self._live_download_rows()
            idle = sum(1 for row in self._download_rows() if self._download_row_idle(row, live_rows))
            if idle > DOWNLOAD_PAGE_SIZE + DOWNLOAD_ROW_SLACK:
                self.refresh_downloaded_items()
        except tk.TclError:
            pass

    def cancel_queued_downloads(self):
        def not_started():
            return [job for job in self._dl_live
                    if not job.started and not job.done and not job.cancel_event.is_set()]

        count = len(self._dl_waiting) + len(not_started())
        if not count:
            return
        if not ask_confirm_popup(
            self, "Cancel Queued Downloads",
            f"Take all {count:,} downloads that have not started yet out of the queue?\n\n"
            f"The one downloading right now keeps going - use its own Cancel button to stop it.",
            confirm_text="Cancel them", cancel_text="Keep the queue",
        ):
            return

        waiting = list(self._dl_waiting)
        self._dl_waiting.clear()
        for job in waiting:
            job.cancel_event.set()
        shown = not_started()
        for job in shown:
            try:
                job.row.cancel_download()
            except Exception:
                job.cancel_event.set()
        self._fill_queue_rows()
        log_event(f"Cancelled {len(waiting) + len(shown):,} queued download(s).")

    def _queue_entries(self, entries: list, mode: str) -> int:
        queued = 0
        for entry in entries:
            if entry is None:
                continue
            job = QueuedDownload(entry, mode)
            self._dl_waiting.append(job)
            self.download_queue.put(job)
            queued += 1

        if queued:
            self._fill_queue_rows()
            with self._queue_start_lock:
                self.is_downloading = True
                if not self._dl_thread_running:
                    self._dl_thread_running = True
                    threading.Thread(target=self._process_download_queue, daemon=True).start()
        return queued

    def start_bulk_download(self, entries: list):
        if not self.require_api_keys():
            return
        entries = [e for e in entries if e is not None]
        if not entries:
            return

        count = len(entries)
        dialog = CollectionQueueDialog(
            self, f"{count:,} selected games", count,
            intro=f"{count:,} selected game{'s' if count != 1 else ''} will be added to the "
                  f"download queue.",
            window_title="Download Selected",
        )
        self.wait_window(dialog)
        if not dialog.result:
            return

        mode = dialog.result.get("mode", POST_MODE_AUTO)
        queued = self._queue_entries(entries, mode)
        log_event(f"Queued {queued} selected game(s) ({POST_MODE_LABELS.get(mode, mode)}).")
        self.select_tab("downloads")

    def start_selected_download(self):
        if not self.require_api_keys():
            return
        chosen = [e for e in getattr(self, "selected_files", []) if e is not None]
        if len(chosen) > 1:
            self.start_bulk_download(chosen)
            return
        if not self.selected_file:
            return
        if self._queue_entries([self.selected_file], self.conversion_var.get()):
            self.select_tab("downloads")

    def _process_download_queue(self):
        """Download stage: fetches the queued games one at a time.

        A finished download is handed to the processing stage, which extracts
        it if needed. Whether the next download waits for that to
        finish is up to _wait_for_download_turn.
        """
        while True:
            with self._queue_start_lock:
                if self.download_queue.empty():
                    self._dl_thread_running = False
                    with self._post_cond:
                        still_processing = self._post_pending > 0
                    self.is_downloading = still_processing
                    return

            job = self.download_queue.get()
            handed_over = False
            try:
                self._wait_for_download_turn(job)
                dest = self._download_job(job)
                if dest is not None:
                    self._hand_to_processing(job, dest)
                    handed_over = True
            except Exception as exc:
                # Never let one bad job stop the queue: log it and carry on with the next.
                import traceback
                name = getattr(getattr(job, "entry", None), "name", "?")
                log_event(f"ERROR downloading '{name}': {exc} - moving on to the next download.\n"
                          f"{traceback.format_exc().rstrip()}")
                if job.row is not None:
                    post_ui(lambda d=job.row, e=exc: d.update_status(f"Error: {e}"))
            finally:
                self.download_queue.task_done()
                if not handed_over:
                    self._finish_job(job)

    def _wait_for_download_turn(self, job) -> None:
        """Hold the next download back until it is allowed to start.

        Setting off: one game at a time, start to finish - wait until the game
        before it has been extracted.
        Setting on: start straight away, unless DOWNLOAD_AHEAD_LIMIT finished
        downloads are already waiting for their turn.
        """
        with self._post_cond:
            while not job.cancel_event.is_set():
                if self.download_ahead:
                    if self._post_pending <= DOWNLOAD_AHEAD_LIMIT:
                        return
                elif self._post_pending == 0:
                    return
                self._post_cond.wait(0.5)

    def _hand_to_processing(self, job, dest) -> None:
        with self._post_cond:
            ahead = self._post_pending
            self._post_pending += 1
        if ahead and not job.cancel_event.is_set():
            post_ui(lambda d=job.row: d.update_status("Downloaded - waiting for its turn..."))
        self._post_queue.put((job, dest))
        if self._post_thread is None:
            self._post_thread = threading.Thread(target=self._process_downloaded_jobs, daemon=True)
            self._post_thread.start()

    def _process_downloaded_jobs(self):
        """Processing stage: extraction, one downloaded game at a time, in order."""
        while True:
            job, dest = self._post_queue.get()
            try:
                self._process_job(job, dest)
            except Exception as exc:
                import traceback
                log_event(f"ERROR processing '{job.entry.name}': {exc} - moving on to the next game.\n"
                          f"{traceback.format_exc().rstrip()}")
                if job.row is not None:
                    post_ui(lambda d=job.row, e=exc: d.update_status(f"Error: {e}"))
            finally:
                self._finish_job(job)
                with self._post_cond:
                    self._post_pending -= 1
                    self._post_cond.notify_all()
                with self._queue_start_lock:
                    with self._post_cond:
                        still_processing = self._post_pending > 0
                    self.is_downloading = self._dl_thread_running or still_processing

    def _finish_job(self, job) -> None:
        job.done = True
        if job.row is not None:
            self._after_job_on_ui()

    def _after_job_on_ui(self) -> None:
        """Tidy the Downloads tab, and wait for the window to catch up before the next job.

        Without the wait, a run of jobs that end instantly (offline, say) would
        ask for rows faster than the window can retire the finished ones.
        """
        if self._stop_for_switch:
            return                  # app switched: no window to catch up with
        caught_up = threading.Event()

        def tidy():
            try:
                self._after_download_job()
            finally:
                caught_up.set()

        post_ui(tidy)
        caught_up.wait(5.0)

    def _download_job(self, job):
        """Download one queued game. Returns the downloaded file, or None if there is nothing to process."""
        if job.cancel_event.is_set() and job.row is None:
            return None                 # cancelled before it ever had a row: nothing to update

        dl_item = self._row_for_job(job)
        if dl_item is None:
            log_event(f"Skipped '{job.entry.name}': the Downloads tab did not respond.")
            return None
        job.started = True
        entry, cancel_event = job.entry, job.cancel_event

        if cancel_event.is_set():
            post_ui(lambda d=dl_item: d.update_status("Cancelled"))
            return None

        post_ui(lambda d=dl_item: d.update_status("Queued / Downloading..."))
        try:
            dl_slot = ("dl-progress", id(dl_item))

            def prog_cb(received, total, speed, final=False):
                if total and total > 0:
                    pct = int(min(received / total * 100, 100))
                    text = f"{pct}% | {speed:.2f} MB/s"
                else:
                    pct = 0
                    text = f"{received / (1024**2):.1f} MB | {speed:.2f} MB/s"
                post_ui_latest(dl_slot, lambda p=pct, t=text: dl_item.update_progress(p, t))

            dest = self.downloader.fetch(entry, download_dir_for(entry), progress_callback=prog_cb,
                                         cancel_event=cancel_event)

            if cancel_event.is_set():
                post_ui(lambda d=dl_item: d.update_status("Cancelled"))
                log_event(f"Download cancelled: {entry.name}")
                return None
            return dest
        except Exception as exc:
            if cancel_event.is_set():
                post_ui(lambda d=dl_item: d.update_status("Cancelled"))
                log_event(f"Download cancelled: {entry.name}")
            else:
                post_ui(lambda d=dl_item, e=exc: d.update_status(f"Error: {e}"))
                log_event(f"ERROR downloading '{entry.name}': {exc} - moving on to the next download.")
            return None

    def _process_job(self, job, dest):
        """Extract a downloaded game if its mode calls for it."""
        dl_item = job.row
        entry, conv_mode, cancel_event = job.entry, job.conv_mode, job.cancel_event

        if cancel_event.is_set():       # cancelled while it waited for its turn
            post_ui(lambda d=dl_item: d.update_status("Cancelled"))
            return

        try:
            def process_cb(msg):
                post_ui_latest(("dl-status", id(dl_item)),
                               lambda m=msg: dl_item.update_status(m))

            mode = resolve_post_mode(conv_mode, entry.console)
            log_event(f"Processing {Path(dest).name} ({POST_MODE_LABELS.get(mode, mode)})...")
            success = handle_post_download(dest, entry, conversion_mode=conv_mode, progress_callback=process_cb)
            if success:
                log_event(f"Finished: {Path(dest).name}")
                done_text = "Completed - extracted" if mode == POST_MODE_EXTRACT else "Completed!"

                def _on_success(d=dl_item, t=done_text):
                    d.update_status(t)
                post_ui(_on_success)
            else:
                if cancel_event.is_set():
                    post_ui(lambda d=dl_item: d.update_status("Cancelled"))
                else:
                    post_ui(lambda d=dl_item: d.update_status("Error: Extraction failed (see Logs)."))
                    log_event(f"ERROR: extraction failed for '{entry.name}' (reason above) - "
                              f"moving on to the next game.")
        except Exception as exc:
            if cancel_event.is_set():
                post_ui(lambda d=dl_item: d.update_status("Cancelled"))
            else:
                import traceback
                post_ui(lambda d=dl_item, e=exc: d.update_status(f"Error: {e}"))
                log_event(f"ERROR processing '{entry.name}': {exc} - moving on to the next game.\n"
                          f"{traceback.format_exc().rstrip()}")

    def setup_downloads_view(self):
        self.downloads_view = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.downloads_view.grid_rowconfigure(1, weight=1)
        self.downloads_view.grid_columnconfigure(0, weight=1)

        downloads_header = ctk.CTkFrame(self.downloads_view, fg_color="transparent")
        downloads_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        downloads_header.grid_columnconfigure(0, weight=1)

        self.refresh_downloads_btn = ctk.CTkButton(
            downloads_header, text="Refresh", width=90, height=26,
            command=self.refresh_downloads_action,
        )
        self.refresh_downloads_btn.grid(row=0, column=2, sticky="e")

        # Only shown while downloads are running. Unticked the tab lists the queue,
        # ticked it lists the finished files; when the queue is empty the tab
        # lists everything and the checkbox goes away.
        self.downloads_finished_var = ctk.BooleanVar(value=False)
        self.downloads_finished_check = ctk.CTkCheckBox(
            downloads_header, text="Show finished downloads",
            variable=self.downloads_finished_var, checkbox_width=18, checkbox_height=18,
            command=self.on_downloads_finished_toggle,
        )
        self.downloads_finished_check.grid(row=0, column=1, sticky="e", padx=(0, 12))
        self.downloads_finished_check.grid_remove()

        # Only shown once there are more downloaded files than fit on one page.
        self.downloads_pager = ctk.CTkFrame(downloads_header, fg_color="transparent")
        self.downloads_pager.grid(row=0, column=0, sticky="w")
        self.downloads_prev_btn = ctk.CTkButton(self.downloads_pager, text="< Prev", width=70, height=26,
                                                command=lambda: self._downloads_page_step(-1))
        self.downloads_prev_btn.pack(side="left")
        self.downloads_next_btn = ctk.CTkButton(self.downloads_pager, text="Next >", width=70, height=26,
                                                command=lambda: self._downloads_page_step(1))
        self.downloads_next_btn.pack(side="left", padx=6)
        self.downloads_pager_lbl = ctk.CTkLabel(self.downloads_pager, text="", text_color="gray",
                                                font=ctk.CTkFont(size=11), anchor="w")
        self.downloads_pager_lbl.pack(side="left", padx=(6, 0))
        self.downloads_pager.grid_remove()

        self.downloads_scroll = ScrollFrame(self.downloads_view, label_text="Active & Downloaded Files")
        self.downloads_scroll.grid(row=1, column=0, sticky="nsew")
        self.downloads_scroll.grid_columnconfigure(0, weight=1)

    def refresh_downloads_action(self):
        for widget in self.downloads_scroll.winfo_children():
            if isinstance(widget, DownloadItemFrame):
                try:
                    widget.check_iso_and_files()
                except Exception:
                    pass
        self.refresh_downloaded_items()

    def _entry_for_filename(self, key: str):
        """The index entry for a Downloads-tab key ("<console>/<file>")."""
        index = getattr(self, "index", None)
        if index is None:
            return None
        lookup = getattr(self, "_entry_by_name", None)
        if lookup is None or getattr(self, "_entry_lookup_for", None) is not index:
            lookup = {}
            try:
                for candidate in index.entries:
                    lookup.setdefault(download_key(candidate).lower(), candidate)
            except Exception:
                lookup = {}
            self._entry_by_name = lookup
            self._entry_lookup_for = index
        return lookup.get(key.lower())

    def _downloaded_items(self) -> list:
        """Everything finished on disk as (row key, status, stem key), in the order the tab lists it."""
        items, keys, stems = [], set(), set()
        for path in downloaded_files():
            key = download_key_for_path(path)
            stem = row_stem_key(key)
            if key not in keys and stem not in stems:
                items.append((key, "Downloaded", stem))
                keys.add(key)
                stems.add(stem)

        # Games extracted and then their archive deleted still get a row.
        folders = []
        if EXTRACT_DIR.exists():
            try:
                for child in EXTRACT_DIR.iterdir():
                    if not child.is_dir():
                        continue
                    if child.name in CONSOLE_NAMES:
                        folders += [(f"{child.name}/{g.name}", g) for g in child.iterdir() if g.is_dir()]
                    else:
                        folders.append((child.name, child))
            except OSError:
                pass
        for stem, folder in folders:
            if stem in stems or not is_extracted(folder):
                continue
            key = stem + ".zip"
            if key not in keys:
                items.append((key, "Extracted", stem))
                keys.add(key)
                stems.add(stem)
        items.sort(key=lambda item: item[0].upper())
        return items

    def refresh_downloaded_items(self, _items: list | None = None):
        self._cancel_after("_dl_refresh_after")
        items = _items if _items is not None else self._downloaded_items()

        size = DOWNLOAD_PAGE_SIZE
        total = len(items)
        pages = max(1, -(-total // size))
        self._dl_page = max(0, min(self._dl_page, pages - 1))
        self._dl_paged = total > size
        wanted = items[self._dl_page * size:(self._dl_page + 1) * size] if self._dl_paged else items
        self._dl_page_names = [name for name, _status, _stem in wanted]

        if self._dl_paged:
            # Rows for files on other pages go; anything queued, downloading or busy stays.
            keep_names = set(self._dl_page_names)
            keep_stems = {stem for _name, _status, stem in wanted}
            live_rows = self._live_download_rows()
            for row in self._download_rows():
                if not self._download_row_idle(row, live_rows):
                    continue
                if row.raw_filename in keep_names or row_stem_key(row.raw_filename) in keep_stems:
                    continue
                self._destroy_download_row(row)

        rows = self._download_rows()
        existing_filenames = {w.raw_filename for w in rows}
        processed_stems = {row_stem_key(w.raw_filename) for w in rows}

        # Rows are slow to build, so a long list is built a few at a time and the window stays usable.
        started = time.perf_counter()
        built = 0
        finished = True
        for name, status, stem in wanted:
            if name in existing_filenames or stem in processed_stems:
                continue
            if built >= 3 and (time.perf_counter() - started) * 1000.0 >= 45.0:
                finished = False
                break
            item_frame = DownloadItemFrame(self.downloads_scroll, name,
                                           entry=self._entry_for_filename(name))
            item_frame.update_progress(100, status)
            item_frame.check_iso_and_files()
            try:
                if item_frame.winfo_exists():
                    item_frame.pack(fill="x", pady=5, padx=5)
            except tk.TclError:
                pass
            existing_filenames.add(name)
            processed_stems.add(stem)
            built += 1

        self._dl_total, self._dl_pages = total, pages
        self._arrange_download_rows()
        self._update_downloads_pager(total, pages)
        if not finished:
            self._dl_refresh_after = self.after(1, lambda: self.refresh_downloaded_items(items))

    def _update_downloads_pager(self, total: int, pages: int) -> None:
        # The pages are pages of downloaded files, so the queue-only view has no use for them.
        if total <= DOWNLOAD_PAGE_SIZE or self._dl_view == "active":
            self.downloads_pager.grid_remove()
            return
        first = self._dl_page * DOWNLOAD_PAGE_SIZE + 1
        last = min(total, first + DOWNLOAD_PAGE_SIZE - 1)
        self.downloads_pager_lbl.configure(
            text=f"Downloaded files {first:,}-{last:,} of {total:,}  |  page {self._dl_page + 1} of {pages}")
        self.downloads_prev_btn.configure(state="normal" if self._dl_page > 0 else "disabled")
        self.downloads_next_btn.configure(state="normal" if self._dl_page < pages - 1 else "disabled")
        self.downloads_pager.grid()

    def _downloads_page_step(self, step: int) -> None:
        self._dl_page = max(0, self._dl_page + step)
        self.refresh_downloaded_items()
        try:
            self.downloads_scroll._parent_canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _set_bulk_status(self, text: str, color: str = "gray"):
        try:
            self.bulk_status_lbl.configure(text=text, text_color=color)
        except Exception:
            pass

    def start_bulk_extract(self):
        if self._bulk_running:
            return
        self._bulk_running = True
        self._set_bulk_buttons("disabled")
        self._set_bulk_status("Scanning...", "gray")
        threading.Thread(target=self._bulk_extract_worker, daemon=True).start()

    def _set_bulk_buttons(self, state: str):
        try:
            self.bulk_extract_btn.configure(state=state)
        except Exception:
            pass

    def open_downloads_folder(self):
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        open_in_file_manager(DOWNLOAD_DIR)

    def _bulk_extract_worker(self):
        done = failed = 0
        try:
            archives = find_unextracted_archives()
            total = len(archives)
            log_event(f"=== EXTRACT ALL: {total} archive(s) not extracted yet ===")
            for i, archive in enumerate(archives, 1):
                if self._stop_for_switch:
                    break                   # switching apps: stop after the last game
                label = f"{download_key_for_path(archive)}"

                def prog(msg, idx=i, name=label):
                    post_ui_latest(_BULK_STATUS_SLOT,
                                   lambda: self._set_bulk_status(f"Extracting {idx}/{total}: {name}\n{msg}"))
                post_ui(lambda t=f"Extracting {i}/{total}: {label}": self._set_bulk_status(t))
                try:
                    ok = handle_post_download(archive, entry=None, conversion_mode=POST_MODE_EXTRACT,
                                              progress_callback=prog)
                except Exception as exc:
                    ok = False
                    log_event(f"  extraction error for {archive.name}: {exc}")
                if ok:
                    done += 1
                else:
                    failed += 1
                    log_event(f"  extraction FAILED: {archive.name} (reason above) - moving on")

            summary = (f"Extract All finished - {done} extracted" if total
                       else "Nothing to extract - every archive is already extracted.")
            if failed:
                summary += f", {failed} failed"
            log_event(f"=== {summary} ===")
            color = "#d97070" if failed else "green"
            post_ui(lambda t=summary, c=color: self._set_bulk_status(t, c))
        except Exception as exc:
            log_event(f"Extract All error: {exc}")
            post_ui(lambda e=exc: self._set_bulk_status(f"Extract All error: {e}", "#d97070"))
        finally:
            self._bulk_running = False      # here too: after an app switch the window is gone
            post_ui(self._bulk_finished)

    def _bulk_finished(self):
        self._bulk_running = False
        self._set_bulk_buttons("normal")
        try:
            self.refresh_downloaded_items()
            for widget in self.downloads_scroll.winfo_children():
                if isinstance(widget, DownloadItemFrame):
                    widget.check_iso_and_files()
        except Exception:
            pass


    def setup_collections_view(self):
        self.collections_view = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.collections_view.grid_rowconfigure(1, weight=1)
        self.collections_view.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.collections_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(3, weight=1)

        ctk.CTkButton(header, text="+ New Collection", width=150, fg_color="#2b8a3e",
                      hover_color="#236c31", command=self.new_collection_action).grid(row=0, column=0)
        ctk.CTkButton(header, text="Import Collection...", width=160, fg_color="#7a4bbd",
                      hover_color="#623a99", command=self.import_collection_action).grid(row=0, column=1, padx=8)
        ctk.CTkButton(header, text="Refresh", width=90, command=self.refresh_collections_view).grid(row=0, column=2)

        self.collections_status_lbl = ctk.CTkLabel(header, text="", text_color="gray",
                                                   font=ctk.CTkFont(size=11), anchor="w")
        self.collections_status_lbl.grid(row=0, column=3, sticky="ew", padx=(12, 0))

        self.collections_scroll = ScrollFrame(self.collections_view, label_text="Your Collections")
        self.collections_scroll.grid(row=1, column=0, sticky="nsew")
        self.collections_scroll.grid_columnconfigure(0, weight=1)

        self._collection_expanded: set = set()
        self._collection_cards: dict = {}
        self._collections_message = None
        self._collections_after = None

    def _set_collections_status(self, text: str, error: bool = False):
        try:
            self.collections_status_lbl.configure(
                text=text, text_color="#d97070" if error else "gray")
        except Exception:
            pass

    def _show_list_message(self, scroll, slot: str, text: str | None, **label_options) -> None:
        current = getattr(self, slot, None)
        if current is not None:
            shown, label = current
            alive = False
            try:
                alive = bool(label.winfo_exists())
            except Exception:
                pass
            if alive and shown == text:
                return
            if alive:
                label.destroy()
            setattr(self, slot, None)
        if text is None:
            return
        label = ctk.CTkLabel(scroll, text_color="gray", text=text, **label_options)
        label.pack(pady=30, padx=10, anchor="w")
        setattr(self, slot, (text, label))

    def _cancel_after(self, slot: str) -> None:
        pending = getattr(self, slot, None)
        setattr(self, slot, None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass

    def _card_font(self, kind: str):
        """Fonts shared by every card, instead of three new ones per card."""
        fonts = getattr(self, "_card_fonts", None)
        if fonts is None:
            fonts = self._card_fonts = {
                "title": ctk.CTkFont(size=15, weight="bold"),
                "meta": ctk.CTkFont(size=11),
                "body": ctk.CTkFont(size=12),
            }
        return fonts[kind]

    def _card_pace(self, tab: str) -> tuple:
        """(time budget, cards per pass, pause) for building cards.

        On the tab being looked at the cards go up as fast as possible. When they
        are being prepared ahead of time, one at a time with a rest in between.
        """
        if self.current_tab == tab:
            return 45.0, 4, 1
        return 10.0, 1, 60

    def _note_input(self, _event=None) -> None:
        self._last_input = time.monotonic()

    def _hold_background_cards(self, tab: str) -> bool:
        """True while cards for a tab that is not showing should wait: the app is being used."""
        return self.current_tab != tab and time.monotonic() - self._last_input < 0.8

    def refresh_collections_view(self):
        self._cancel_after("_collections_after")
        if self._hold_background_cards("collections"):
            self._collections_after = self.after(400, self.refresh_collections_view)
            return
        try:
            collections = load_collections()
            if not collections:
                sync_cards(self.collections_scroll, self._collection_cards, {}, [], None)
                self._show_list_message(
                    self.collections_scroll, "_collections_message",
                    "No collections yet.\n\n"
                    "Find a game in Library / Search, select it, and hit '+ Add to Collection'.",
                    justify="left", wraplength=640)
                return

            self._show_list_message(self.collections_scroll, "_collections_message", None)
            items, order, by_key = {}, [], {}
            for collection in collections:
                key = collection.name
                while key in items:
                    key += "\0"
                # Only what decides which widgets a card is made of. Its texts, its
                # button states and its game list are brought up to date in place
                # by _update_collection_card, so a card is hardly ever rebuilt.
                items[key] = (bool(collection.shared_id), bool(collection.description))
                order.append(key)
                by_key[key] = collection

            budget, at_least, pause = self._card_pace("collections")
            finished = sync_cards(self.collections_scroll, self._collection_cards, items, order,
                                  lambda key: self._build_collection_card(by_key[key]),
                                  budget_ms=budget, min_build=at_least)
            for key in order:
                built = self._collection_cards.get(key)
                if built is not None:
                    self._update_collection_card(built[1], by_key[key])
            if not finished:
                self._collections_after = self.after(pause, self.refresh_collections_view)
        except tk.TclError:
            return
        finally:
            settle_scrollable(self.collections_scroll)

    def _build_collection_card(self, collection: GameCollection):
        """The fixed part of a card. What it shows is filled in by _update_collection_card."""
        card = ctk.CTkFrame(self.collections_scroll)
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(head, text=collection.name, font=self._card_font("title"),
                     anchor="w").grid(row=0, column=0, sticky="w")

        meta_lbl = ctk.CTkLabel(head, text="", text_color="gray", font=self._card_font("meta"),
                                anchor="w")
        meta_lbl.grid(row=1, column=0, sticky="w")

        description_lbl = None
        if collection.description:
            description_lbl = ctk.CTkLabel(head, text="", text_color="gray",
                                           font=self._card_font("body"), anchor="w", wraplength=620,
                                           justify="left")
            description_lbl.grid(row=2, column=0, sticky="w", pady=(4, 0))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=1, column=0, sticky="w", padx=12, pady=(8, 10))

        download_btn = ctk.CTkButton(
            buttons, text="Download All", width=120, fg_color="#2b8a3e", hover_color="#236c31",
            command=lambda n=collection.name: self.download_collection_action(n),
        )
        download_btn._accent_locked = True
        download_btn.pack(side="left")

        export_btn = ctk.CTkButton(buttons, text="Export...", width=90,
                                   command=lambda n=collection.name: self.export_collection_action(n))
        export_btn.pack(side="left", padx=6)

        shared = bool(collection.shared_id)
        share_btn = ctk.CTkButton(
            buttons, text="Update Shared" if shared else "Share",
            width=120 if shared else 80, fg_color="#1f6f8b", hover_color="#175468",
            command=lambda n=collection.name: self.share_collection_action(n))
        share_btn._accent_locked = True
        share_btn.pack(side="left", padx=(0, 6))

        if shared:
            unshare_btn = ctk.CTkButton(
                buttons, text="Unshare", width=90, fg_color="transparent", border_width=1,
                text_color=("black", "white"),
                command=lambda n=collection.name: self.unshare_collection_action(n))
            unshare_btn.pack(side="left", padx=(0, 6))

        toggle_btn = ctk.CTkButton(
            buttons, text="Show Games", width=110,
            fg_color="transparent", border_width=1, text_color=("black", "white"),
            command=lambda n=collection.name: self.toggle_collection_games(n))
        toggle_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(buttons, text="Rename", width=80, fg_color="transparent", border_width=1,
                      text_color=("black", "white"),
                      command=lambda n=collection.name: self.rename_collection_action(n)).pack(side="left")

        delete_btn = ctk.CTkButton(buttons, text="Delete", width=80, fg_color="#8f2f2f",
                                   hover_color="#6f2424",
                                   command=lambda n=collection.name: self.delete_collection_action(n))
        delete_btn._accent_locked = True
        delete_btn.pack(side="left", padx=6)

        card._parts = {
            "meta": meta_lbl, "description": description_lbl, "toggle": toggle_btn,
            "need_games": (download_btn, export_btn, share_btn), "games_frame": None,
        }
        # What the card shows right now; _update_collection_card only touches what differs.
        card._shown = {"meta": "", "description": "", "has_games": True, "expanded": False,
                       "games": None}
        return card

    def _update_collection_card(self, card, collection: GameCollection) -> None:
        """Make a card show `collection` as it is now, changing only what is out of date."""
        parts, shown = card._parts, card._shown

        count = len(collection.games)
        total = collection.total_size()
        meta = f"{count} game{'s' if count != 1 else ''}"
        if total:
            meta += f"  |  {format_size(total)} total"
        if collection.updated:
            meta += f"  |  updated {collection.updated}"
        if collection.shared_id:
            meta += "  |  shared with the community"
        if meta != shown["meta"]:
            parts["meta"].configure(text=meta)
            shown["meta"] = meta

        if parts["description"] is not None and collection.description != shown["description"]:
            parts["description"].configure(text=collection.description)
            shown["description"] = collection.description

        has_games = bool(collection.games)
        if has_games != shown["has_games"]:
            for button in parts["need_games"]:
                button.configure(state="normal" if has_games else "disabled")
            shown["has_games"] = has_games

        expanded = collection.name in self._collection_expanded
        if expanded != shown["expanded"]:
            parts["toggle"].configure(text="Hide Games" if expanded else "Show Games")
            shown["expanded"] = expanded

        games = None
        if expanded:
            games = tuple((game.name, game.console, game.size) for game in collection.games)
        if games == shown["games"]:
            return
        shown["games"] = games

        if parts["games_frame"] is not None:
            parts["games_frame"].destroy()
            parts["games_frame"] = None
        if games is None:
            return

        games_frame = ctk.CTkFrame(card, fg_color=("gray92", "gray17"))
        games_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        games_frame.grid_columnconfigure(0, weight=1)
        parts["games_frame"] = games_frame

        if not games:
            ctk.CTkLabel(games_frame, text="Nothing in here yet.", text_color="gray",
                         anchor="w").grid(row=0, column=0, sticky="w", padx=10, pady=8)
            return

        labels = []
        for name, console, size in games:
            tag = f"[{console_display_name(console)}] " if console else ""
            labels.append(f"{tag}{Path(name).name}  ({format_size(size)})")
        game_names = [name for name, _console, _size in games]
        build_game_rows(
            games_frame, labels, button_text="Remove",
            on_button=lambda i, c=collection.name: self.remove_game_from_collection(c, game_names[i]),
            scroll=self.collections_scroll)

    def toggle_collection_games(self, name: str):
        if name in self._collection_expanded:
            self._collection_expanded.discard(name)
        else:
            self._collection_expanded.add(name)
        self.refresh_collections_view()


    def add_selected_to_collection(self):
        entries = [e for e in getattr(self, "selected_files", []) if e is not None]
        if not entries and self.selected_file is not None:
            entries = [self.selected_file]
        if not entries:
            return

        if len(entries) == 1:
            dialog = CollectionPickerDialog(self, entries[0].name)
        else:
            dialog = CollectionPickerDialog(
                self, f"{len(entries):,} selected items",
                game_names=[e.name for e in entries],
            )
        self.wait_window(dialog)
        chosen = dialog.result
        if not chosen:
            return

        collection = get_collection(chosen)
        if collection is None:
            log_event(f"Collection '{chosen}' vanished before the game could be added.")
            return

        added = collection.add_many(entries)
        skipped = len(entries) - added

        if added:
            upsert_collection(collection)

        if len(entries) == 1:
            if added:
                self.collection_hint_lbl.configure(text=f"Added to '{collection.name}'.")
                log_event(f"Added '{entries[0].name}' to collection '{collection.name}'.")
            else:
                self.collection_hint_lbl.configure(text=f"Already in '{collection.name}'.")
        elif added:
            note = f"Added {added:,} to '{collection.name}'."
            if skipped:
                note += f" {skipped:,} already there."
            self.collection_hint_lbl.configure(text=note)
            log_event(f"Added {added} item(s) to collection '{collection.name}' "
                      f"({skipped} already there).")
        else:
            self.collection_hint_lbl.configure(
                text=f"All {len(entries):,} were already in '{collection.name}'.")
        self.after(6000, lambda: self.collection_hint_lbl.configure(text=""))

    def new_collection_action(self):
        dialog = CollectionEditDialog(self, title="New Collection")
        self.wait_window(dialog)
        if not dialog.result:
            return
        name, description = dialog.result
        upsert_collection(GameCollection(name=name, description=description,
                                         created=time.strftime("%Y-%m-%d %H:%M:%S")))
        self._collection_expanded.add(name)
        self.refresh_collections_view()
        self._set_collections_status(f"Created '{name}'.")

    def rename_collection_action(self, name: str):
        collection = get_collection(name)
        if collection is None:
            return
        dialog = CollectionEditDialog(self, title="Rename Collection", name=collection.name,
                                      description=collection.description, ok_text="Save")
        self.wait_window(dialog)
        if not dialog.result:
            return
        new_name, description = dialog.result
        delete_collection(collection.name)
        self._collection_expanded.discard(collection.name)
        collection.name = new_name
        collection.description = description
        upsert_collection(collection)
        self._collection_expanded.add(new_name)
        self.refresh_collections_view()
        self._set_collections_status(f"Renamed to '{new_name}'.")

    def delete_collection_action(self, name: str):
        collection = get_collection(name)
        if collection is None:
            return
        count = len(collection.games)
        if not ask_confirm_popup(
            self, "Delete Collection",
            f"Delete '{collection.name}'?\n\n"
            f"The {count} game{'s' if count != 1 else ''} in it stay exactly where they are - "
            f"this only removes the list itself, and it can't be undone.",
            confirm_text="Delete", cancel_text="Keep it",
        ):
            return
        delete_collection(collection.name)
        self._collection_expanded.discard(collection.name)
        self.refresh_collections_view()
        self._set_collections_status(f"Deleted '{collection.name}'.")

    def remove_game_from_collection(self, collection_name: str, game_name: str):
        collection = get_collection(collection_name)
        if collection is None:
            return
        if collection.remove(game_name):
            upsert_collection(collection)
            self.refresh_collections_view()
            self._set_collections_status(f"Removed 1 game from '{collection.name}'.")

    def export_collection_action(self, name: str):
        collection = get_collection(name)
        if collection is None:
            return
        from tkinter import filedialog
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Export Collection",
            initialfile=safe_collection_filename(collection.name),
            defaultextension=COLLECTION_EXPORT_EXT,
            filetypes=[("Bman Collection", f"*{COLLECTION_EXPORT_EXT}"),
                       ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            written = export_collection(collection, Path(target))
        except Exception as exc:
            log_event(f"Export of '{collection.name}' failed: {exc}")
            show_info_popup(self, "Export Failed", f"Couldn't write that file:\n\n{exc}")
            return

        log_event(f"Exported collection '{collection.name}' ({len(collection.games)} games) to {written}")
        self._set_collections_status(f"Exported to {written.name}.")
        show_info_popup(
            self, "Collection Exported",
            f"'{collection.name}' was written to:\n\n{written}\n\n"
            f"Send that file to anyone running Bman Retro Downloader - they hit "
            f"'Import Collection...' on their Collections tab and the whole lot goes "
            f"straight into their download queue.",
        )

    def import_collection_action(self):
        if not self.require_api_keys():
            return
        from tkinter import filedialog
        source = filedialog.askopenfilename(
            parent=self,
            title="Import Collection",
            filetypes=[("Bman Collection", f"*{COLLECTION_EXPORT_EXT}"),
                       ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not source:
            return

        try:
            collection = read_collection_file(Path(source))
        except ValueError as exc:
            show_info_popup(self, "Import Failed", str(exc))
            return
        except Exception as exc:
            log_event(f"Import failed for {source}: {exc}")
            show_info_popup(self, "Import Failed", f"Couldn't read that collection:\n\n{exc}")
            return

        original_name = collection.name
        collection.name = unique_collection_name(collection.name)
        matched = refresh_games_against_index(collection, self.index)
        collection.created = time.strftime("%Y-%m-%d %H:%M:%S")
        upsert_collection(collection)
        self._collection_expanded.add(collection.name)
        self.refresh_collections_view()

        total = len(collection.games)
        renamed_note = (f"\n\n(You already had a collection called '{original_name}', so this one "
                        f"came in as '{collection.name}'.)") if collection.name != original_name else ""
        unknown = total - matched
        index_note = ""
        if unknown:
            index_note = (f"\n\n{unknown} of them aren't in your local index - they'll still "
                          f"download, the index is only used to fill in file sizes.")

        log_event(f"Imported collection '{collection.name}' with {total} games "
                  f"({matched} matched the local index).")
        self._set_collections_status(f"Imported '{collection.name}' ({total} games).")

        if ask_confirm_popup(
            self, "Collection Imported",
            f"'{collection.name}' is in - {total} game{'s' if total != 1 else ''}."
            f"{index_note}{renamed_note}\n\nQueue them all for download now?",
            confirm_text="Queue them up", cancel_text="Later",
        ):
            self.queue_collection(collection.name)

    def download_collection_action(self, name: str):
        self.queue_collection(name)

    def queue_collection(self, name: str):
        if not self.require_api_keys():
            return
        collection = get_collection(name)
        if collection is None or not collection.games:
            return
        self._run_collection_queue(collection, self._set_collections_status)

    def _run_collection_queue(self, collection: GameCollection, status=None):
        if not self.require_api_keys():
            return
        if collection is None or not collection.games:
            return
        if status is None:
            status = self._set_collections_status

        dialog = CollectionQueueDialog(self, collection.name, len(collection.games))
        self.wait_window(dialog)
        if not dialog.result:
            return

        mode = dialog.result.get("mode", POST_MODE_AUTO)
        queued = self._queue_entries([game.to_files() for game in collection.games], mode)
        log_event(f"Queued collection '{collection.name}': {queued} game(s) "
                  f"({POST_MODE_LABELS.get(mode, mode)}).")
        status(f"Queued {queued} game{'s' if queued != 1 else ''} from '{collection.name}'.")
        self.select_tab("downloads")


    def setup_community_view(self):
        self._community_entries: list = []
        self._community_expanded: set = set()
        self._community_details: dict = {}
        self._community_loading = False
        self._community_detail_stamps: dict = {}     # which version of each fetched game list is held
        self._community_busy: set = set()
        self._community_checked = 0.0                # when the list was last fetched from GitHub
        self._community_from_cache = False
        self._community_last_error = ""
        self._community_sharing = False
        self._community_cards: dict = {}
        self._community_message = None
        self._community_after = None

        self.community_view = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.community_view.grid_rowconfigure(1, weight=1)
        self.community_view.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self.community_view, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(2, weight=1)

        self.community_refresh_btn = ctk.CTkButton(
            header, text="Refresh", width=90,
            command=lambda: self.refresh_community_view(force=True))
        self.community_refresh_btn.grid(row=0, column=0)

        self.community_search_var = tk.StringVar()
        self.community_search_var.trace_add("write", lambda *_: self._render_community_list())
        search = ctk.CTkEntry(header, width=240, placeholder_text="Filter by name or description...",
                              textvariable=self.community_search_var)
        search.grid(row=0, column=1, padx=8)

        self.community_status_lbl = ctk.CTkLabel(header, text="", text_color="gray",
                                                 font=ctk.CTkFont(size=11), anchor="w")
        self.community_status_lbl.grid(row=0, column=2, sticky="ew", padx=(12, 0))

        self.community_scroll = ScrollFrame(
            self.community_view, label_text="Collections shared by other people")
        self.community_scroll.grid(row=1, column=0, sticky="nsew")
        self.community_scroll.grid_columnconfigure(0, weight=1)

    def _set_community_status(self, text: str, error: bool = False):
        try:
            self.community_status_lbl.configure(
                text=text, text_color="#d97070" if error else "gray")
        except Exception:
            pass

    def _load_community_from_cache(self) -> bool:
        cached, age = load_community_cache()
        if not cached:
            return False
        self._community_entries = cached
        self._community_checked = time.time() - age
        self._community_from_cache = True
        self._render_community_list()
        return True

    def open_community_tab(self):
        if not self._community_entries:
            self._load_community_from_cache()
        elif self._community_after is not None:
            self._render_community_list()            # cards were still being prepared: finish them now
        if self._community_entries:
            age = time.time() - self._community_checked
            if self._community_from_cache:
                mins = int(age // 60)
                self._set_community_status(
                    "Showing the list from last time..." if mins < 1
                    else f"Showing the list from {mins} minute{'s' if mins != 1 else ''} ago...")
            if age < COMMUNITY_CACHE_TTL_SECONDS:
                return
        self.refresh_community_view(force=not self._community_entries)

    def _prepare_lists(self, collections_started: bool = False) -> None:
        """Build the Collections and Community cards shortly after start-up.

        Building a card is the slow part of opening those tabs. Done ahead of
        time - one card at a go, and only while the mouse and keyboard are at
        rest - the tabs open with everything already in place. Nothing is
        fetched here: the community list comes from the copy saved last time.
        """
        try:
            if not collections_started:
                if self.current_tab != "collections":
                    self.refresh_collections_view()
                self.after(300, lambda: self._prepare_lists(True))
            elif self._collections_after is not None and self.current_tab != "collections":
                self.after(300, lambda: self._prepare_lists(True))      # one list at a time
            elif self.current_tab != "community" and not self._community_entries:
                self._load_community_from_cache()
        except Exception as exc:
            log_event(f"Preparing the collection lists failed: {exc}")

    def refresh_community_view(self, force: bool = False):
        if self._community_loading:
            return
        self._community_loading = True
        try:
            self.community_refresh_btn.configure(state="disabled")
        except Exception:
            pass
        self._set_community_status("Checking for shared collections...")
        if force and not self._community_entries:
            self._render_community_list()

        def worker():
            try:
                entries, _ = fetch_community_index()
                post_ui(lambda: self._community_index_loaded(entries, None))
            except Exception as exc:
                post_ui(lambda exc=exc: self._community_index_loaded(None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _community_index_loaded(self, entries, error):
        self._community_loading = False
        try:
            self.community_refresh_btn.configure(state="normal")
        except Exception:
            pass

        if error is not None:
            self._community_last_error = str(error)
            log_event(f"Community list refresh failed: {error}")
            if self._community_entries:
                self._set_community_status(
                    f"Couldn't refresh - showing the last list that loaded. ({error})", error=True)
            else:
                self._set_community_status(str(error), error=True)
                self._render_community_list()
            return

        self._community_last_error = ""
        self._community_entries = entries or []
        self._community_checked = time.time()
        self._community_from_cache = False
        # Keep a fetched game list only while it is still the shared version.
        current = {e.id: community_entry_stamp(e) for e in self._community_entries}
        self._community_details = {
            k: v for k, v in self._community_details.items()
            if k in current and self._community_detail_stamps.get(k) == current[k]}
        self._community_detail_stamps = {k: current[k] for k in self._community_details}
        self._community_expanded &= set(current)
        save_community_cache(self._community_entries)
        if current:
            prune_community_list_cache(current)
        for entry in self._community_entries:
            if entry.id in self._community_expanded:
                self._cached_community_detail(entry)
        self._render_community_list()
        for entry in self._community_entries:        # an opened list that was updated: fetch the new one
            if entry.id in self._community_expanded and entry.id not in self._community_details:
                self._fetch_community_detail(entry, lambda _c: None)

        count = len(self._community_entries)
        if count:
            self._set_community_status(
                f"{count} collection{'s' if count != 1 else ''} shared  |  "
                f"updated {time.strftime('%H:%M')}")
        else:
            self._set_community_status("Nobody has shared anything yet.")

    def _visible_community_entries(self) -> list:
        # Same matching as the game search: case, accents and punctuation don't
        # matter, so "assassin's creed" finds "Assassins Creed" and the reverse.
        tokens = normalize_search_text(self.community_search_var.get() or "").split()
        if not tokens:
            return list(self._community_entries)

        def matches(entry) -> bool:
            spaced = normalize_search_text(f"{entry.name} {entry.description or ''}")
            squashed = spaced.replace(" ", "")
            return all(t in spaced or t in squashed for t in tokens)

        return [e for e in self._community_entries if matches(e)]

    def _render_community_list(self):
        self._cancel_after("_community_after")
        if self._hold_background_cards("community"):
            self._community_after = self.after(400, self._render_community_list)
            return
        try:
            if not self._community_entries:
                if self._community_loading:
                    message = "Loading the community list..."
                elif self._community_last_error:
                    message = (f"Couldn't load the community list.\n\n{self._community_last_error}\n\n"
                               f"Hit Refresh to try again - everything else in the app works fine "
                               f"without it.")
                else:
                    message = ("Nothing here yet.\n\n"
                               "This tab shows collections other people have shared. To put one of "
                               "yours up, go to the Collections tab and hit 'Share' on it - it turns "
                               "up here for everyone running Bman Retro Downloader.")
                sync_cards(self.community_scroll, self._community_cards, {}, [], None)
                self._show_list_message(self.community_scroll, "_community_message", message,
                                        justify="left", wraplength=640)
                return

            items, by_key = {}, {}
            for entry in self._community_entries:
                if entry.id in items:
                    continue
                # Only what decides which widgets a card is made of; the rest is
                # brought up to date in place by _update_community_card.
                items[entry.id] = (bool(entry.description),)
                by_key[entry.id] = entry

            visible, seen = [], set()
            for entry in self._visible_community_entries():
                if entry.id not in seen:
                    seen.add(entry.id)
                    visible.append(entry.id)

            self._show_list_message(
                self.community_scroll, "_community_message",
                None if visible else "Nothing matches that filter.")
            budget, at_least, pause = self._card_pace("community")
            finished = sync_cards(self.community_scroll, self._community_cards, items, visible,
                                  lambda key: self._build_community_card(by_key[key]),
                                  budget_ms=budget, min_build=at_least)
            for key in visible:
                built = self._community_cards.get(key)
                if built is not None:
                    self._update_community_card(built[1], by_key[key])
            if not finished:
                self._community_after = self.after(pause, self._render_community_list)
        except tk.TclError:
            return
        finally:
            settle_scrollable(self.community_scroll)

    def _build_community_card(self, entry: CommunityEntry):
        """The fixed part of a card. What it shows is filled in by _update_community_card."""
        card = ctk.CTkFrame(self.community_scroll)
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        head.grid_columnconfigure(0, weight=1)

        name_lbl = ctk.CTkLabel(head, text="", font=self._card_font("title"), anchor="w")
        name_lbl.grid(row=0, column=0, sticky="w")

        meta_lbl = ctk.CTkLabel(head, text="", text_color="gray", font=self._card_font("meta"),
                                anchor="w")
        meta_lbl.grid(row=1, column=0, sticky="w")

        description_lbl = None
        if entry.description:
            description_lbl = ctk.CTkLabel(head, text="", text_color="gray",
                                           font=self._card_font("body"), anchor="w", wraplength=620,
                                           justify="left")
            description_lbl.grid(row=2, column=0, sticky="w", pady=(4, 0))

        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.grid(row=1, column=0, sticky="w", padx=12, pady=(8, 10))

        download_btn = ctk.CTkButton(
            buttons, text="Download All", width=120, fg_color="#2b8a3e", hover_color="#236c31",
            command=lambda i=entry.id: self.community_download_action(i))
        download_btn._accent_locked = True
        download_btn.pack(side="left")

        save_btn = ctk.CTkButton(
            buttons, text="Save to My Collections", width=180, fg_color="#7a4bbd",
            hover_color="#623a99", command=lambda i=entry.id: self.community_save_action(i))
        save_btn._accent_locked = True
        save_btn.pack(side="left", padx=6)

        show_btn = ctk.CTkButton(
            buttons, text="Show Games", width=110,
            fg_color="transparent", border_width=1, text_color=("black", "white"),
            command=lambda i=entry.id: self.community_toggle_games(i))
        show_btn.pack(side="left")

        card._parts = {
            "name": name_lbl, "meta": meta_lbl, "description": description_lbl,
            "buttons": buttons, "actions": (download_btn, save_btn, show_btn), "toggle": show_btn,
            "busy_lbl": None, "games_frame": None,
        }
        card._shown = {"name": "", "meta": "", "description": "", "busy": False,
                       "expanded": False, "games": None}
        return card

    def _update_community_card(self, card, entry: CommunityEntry) -> None:
        """Make a card show `entry` as it is now, changing only what is out of date."""
        parts, shown = card._parts, card._shown

        if entry.name != shown["name"]:
            parts["name"].configure(text=entry.name)
            shown["name"] = entry.name

        meta = f"{entry.games} game{'s' if entry.games != 1 else ''}"
        if entry.size:
            meta += f"  |  {format_size(entry.size)} total"
        if entry.updated:
            meta += f"  |  shared {entry.updated}"
        if meta != shown["meta"]:
            parts["meta"].configure(text=meta)
            shown["meta"] = meta

        if parts["description"] is not None and entry.description != shown["description"]:
            parts["description"].configure(text=entry.description)
            shown["description"] = entry.description

        busy = entry.id in self._community_busy
        if busy != shown["busy"]:
            for button in parts["actions"]:
                button.configure(state="disabled" if busy else "normal")
            if busy:
                parts["busy_lbl"] = ctk.CTkLabel(parts["buttons"], text="Fetching...",
                                                 text_color="gray", font=self._card_font("meta"))
                parts["busy_lbl"].pack(side="left", padx=(10, 0))
            elif parts["busy_lbl"] is not None:
                parts["busy_lbl"].destroy()
                parts["busy_lbl"] = None
            shown["busy"] = busy

        expanded = entry.id in self._community_expanded
        if expanded != shown["expanded"]:
            parts["toggle"].configure(text="Hide Games" if expanded else "Show Games")
            shown["expanded"] = expanded

        detail = self._community_details.get(entry.id) if expanded else None
        if not expanded:
            games = None
        elif detail is None:
            games = "loading"
        else:
            games = (id(detail), len(detail.games))
        if games == shown["games"]:
            return
        shown["games"] = games

        if parts["games_frame"] is not None:
            parts["games_frame"].destroy()
            parts["games_frame"] = None
        if games is None:
            return

        games_frame = ctk.CTkFrame(card, fg_color=("gray92", "gray17"))
        games_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        games_frame.grid_columnconfigure(0, weight=1)
        parts["games_frame"] = games_frame

        if detail is None:
            ctk.CTkLabel(games_frame, text="Loading the game list...", text_color="gray",
                         anchor="w").grid(row=0, column=0, sticky="w", padx=10, pady=8)
            return

        labels = []
        for game in detail.games:
            tag = f"[{console_display_name(game.console)}] " if game.console else ""
            labels.append(f"{tag}{Path(game.name).name}  ({format_size(game.size)})")
        build_game_rows(games_frame, labels, scroll=self.community_scroll)

    def _community_entry(self, entry_id: str) -> CommunityEntry | None:
        for entry in self._community_entries:
            if entry.id == entry_id:
                return entry
        return None

    def _cached_community_detail(self, entry: CommunityEntry):
        """The game list of `entry` if it is already here: fetched this session, or saved on disk."""
        stamp = community_entry_stamp(entry)
        detail = self._community_details.get(entry.id)
        if detail is not None and self._community_detail_stamps.get(entry.id) == stamp:
            return detail
        detail = load_cached_community_collection(entry)
        if detail is None:
            self._community_details.pop(entry.id, None)
            self._community_detail_stamps.pop(entry.id, None)
            return None
        self._community_details[entry.id] = detail
        self._community_detail_stamps[entry.id] = stamp
        return detail

    def _fetch_community_detail(self, entry: CommunityEntry, on_ready):
        cached = self._cached_community_detail(entry)
        if cached is not None:
            on_ready(cached)
            return
        if entry.id in self._community_busy:
            return

        self._community_busy.add(entry.id)
        self._render_community_list()
        self._set_community_status(f"Fetching '{entry.name}'...")

        def worker():
            try:
                collection = fetch_community_collection(entry)
                post_ui(lambda: self._community_detail_loaded(entry, collection, None, on_ready))
            except Exception as exc:
                post_ui(lambda exc=exc: self._community_detail_loaded(entry, None, exc, on_ready))

        threading.Thread(target=worker, daemon=True).start()

    def _community_detail_loaded(self, entry, collection, error, on_ready):
        self._community_busy.discard(entry.id)
        if error is not None:
            log_event(f"Community: couldn't fetch '{entry.name}': {error}")
            self._community_expanded.discard(entry.id)
            self._set_community_status(str(error), error=True)
            self._render_community_list()
            return
        self._community_details[entry.id] = collection
        self._community_detail_stamps[entry.id] = community_entry_stamp(entry)
        self._set_community_status(f"'{entry.name}' loaded - {len(collection.games)} games.")
        self._render_community_list()
        try:
            on_ready(collection)
        except Exception as exc:
            log_event(f"Community: action after fetching '{entry.name}' failed: {exc}")

    def community_toggle_games(self, entry_id: str):
        entry = self._community_entry(entry_id)
        if entry is None:
            return
        if entry_id in self._community_expanded:
            self._community_expanded.discard(entry_id)
            self._render_community_list()
            return
        self._community_expanded.add(entry_id)
        self._cached_community_detail(entry)         # if this PC has the list already, it shows at once
        self._render_community_list()
        self._fetch_community_detail(entry, lambda _c: None)

    def community_save_action(self, entry_id: str):
        entry = self._community_entry(entry_id)
        if entry is None:
            return
        self._fetch_community_detail(entry, self._save_community_collection)

    def _save_community_collection(self, collection: GameCollection):
        saved = GameCollection.from_dict(collection.to_dict())
        if saved is None:
            return
        original = saved.name
        saved.name = unique_collection_name(saved.name)
        saved.shared_id = ""
        matched = refresh_games_against_index(saved, self.index)
        saved.created = time.strftime("%Y-%m-%d %H:%M:%S")
        upsert_collection(saved)

        total = len(saved.games)
        renamed = (f"\n\n(You already had one called '{original}', so it came in as "
                   f"'{saved.name}'.)") if saved.name != original else ""
        unknown = total - matched
        index_note = (f"\n\n{unknown} of them aren't in your local index - they'll still "
                      f"download, the index is only used to fill in file sizes."
                      ) if unknown else ""

        log_event(f"Saved community collection '{saved.name}' ({total} games, {matched} matched the index).")
        self._set_community_status(f"Saved '{saved.name}' to your collections.")

        if ask_confirm_popup(
            self, "Saved to Your Collections",
            f"'{saved.name}' is in your Collections tab - "
            f"{total} game{'s' if total != 1 else ''}.{index_note}{renamed}\n\n"
            f"Queue them all for download now?",
            confirm_text="Queue them up", cancel_text="Later",
        ):
            self._run_collection_queue(saved, self._set_community_status)

    def community_download_action(self, entry_id: str):
        entry = self._community_entry(entry_id)
        if entry is None:
            return
        if not self.require_api_keys():
            return
        self._fetch_community_detail(
            entry, lambda c: self._run_collection_queue(c, self._set_community_status))


    def share_collection_action(self, name: str):
        collection = get_collection(name)
        if collection is None:
            return
        if not collection.games:
            show_info_popup(self, "Nothing to Share",
                            "That collection is empty - put some games in it first.")
            return
        if not community_can_share():
            show_info_popup(
                self, "Sharing Unavailable",
                "This build can't upload to the Community tab - it has no sharing token "
                "configured.\n\nYou can still browse and download everything other people "
                "have shared, and 'Export...' writes a .bmc file you can send to anyone "
                "directly.")
            return
        if getattr(self, "_community_sharing", False):
            return

        already = bool(collection.shared_id)
        count = len(collection.games)
        if not ask_confirm_popup(
            self, "Update Shared Collection" if already else "Share Collection",
            (f"Update the shared copy of '{collection.name}' so everyone sees the "
             f"current {count} game{'s' if count != 1 else ''}?"
             if already else
             f"Put '{collection.name}' ({count} game{'s' if count != 1 else ''}) on the "
             f"Community Collections tab?\n\n"
             f"Everyone running Bman Retro Downloader will be able to see and download it. "
             f"Shares are anonymous - the collection's name, description and game list go "
             f"up, and nothing about you does.\n\n"
             f"You can take it down again from this same button."),
            confirm_text="Update it" if already else "Share it", cancel_text="Cancel",
        ):
            return

        self._community_sharing = True
        self._set_collections_status(
            f"{'Updating' if already else 'Sharing'} '{collection.name}'...")

        def worker():
            try:
                entry = publish_community_collection(collection)
                post_ui(lambda: self._share_finished(collection.name, entry, None, already))
            except Exception as exc:
                post_ui(lambda exc=exc: self._share_finished(collection.name, None, exc, already))

        threading.Thread(target=worker, daemon=True).start()

    def _share_finished(self, name, entry, error, was_update):
        self._community_sharing = False
        if error is not None:
            log_event(f"Sharing '{name}' failed: {error}")
            self._set_collections_status(f"Couldn't share '{name}'.", error=True)
            show_info_popup(self, "Share Failed",
                            f"'{name}' wasn't shared.\n\n{error}\n\nYour own copy is untouched.")
            return

        collection = get_collection(name)
        if collection is not None:
            collection.shared_id = entry.id
            upsert_collection(collection)

        self._community_details.pop(entry.id, None)
        self._community_detail_stamps.pop(entry.id, None)
        self._community_checked = 0.0                # the Community tab re-reads the list next time
        log_event(f"{'Updated' if was_update else 'Shared'} collection '{name}' "
                  f"as community entry {entry.id}.")
        self._set_collections_status(
            f"{'Updated' if was_update else 'Shared'} '{name}' - it's on the Community tab.")
        self.refresh_collections_view()

        show_info_popup(
            self, "Collection Updated" if was_update else "Collection Shared",
            f"'{name}' is live on the Community Collections tab"
            f"{' with your latest changes' if was_update else ' for everyone'}.\n\n"
            f"It can take a minute or two to show up for other people. Hit 'Unshare' on "
            f"this collection any time to take it back down.")

    def unshare_collection_action(self, name: str):
        collection = get_collection(name)
        if collection is None or not collection.shared_id:
            return
        if getattr(self, "_community_sharing", False):
            return
        if not ask_confirm_popup(
            self, "Unshare Collection",
            f"Take '{collection.name}' off the Community Collections tab?\n\n"
            f"Your own copy stays exactly where it is - this only removes the shared one. "
            f"Anyone who already saved it keeps their copy.",
            confirm_text="Take it down", cancel_text="Leave it up",
        ):
            return

        shared_id = collection.shared_id
        self._community_sharing = True
        self._set_collections_status(f"Removing '{collection.name}' from the community list...")

        def worker():
            try:
                unshare_community_collection(shared_id)
                post_ui(lambda: self._unshare_finished(name, shared_id, None))
            except Exception as exc:
                post_ui(lambda exc=exc: self._unshare_finished(name, shared_id, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _unshare_finished(self, name, shared_id, error):
        self._community_sharing = False
        if error is not None:
            log_event(f"Unsharing '{name}' failed: {error}")
            self._set_collections_status(f"Couldn't unshare '{name}'.", error=True)
            show_info_popup(
                self, "Unshare Failed",
                f"'{name}' is still on the community list.\n\n{error}\n\n"
                f"Try again in a moment.")
            return

        current = get_collection(name)
        if current is not None:
            current.shared_id = ""
            upsert_collection(current)
        self._community_details.pop(shared_id, None)
        self._community_detail_stamps.pop(shared_id, None)
        self._community_entries = [e for e in self._community_entries if e.id != shared_id]
        save_community_cache(self._community_entries)
        log_event(f"Unshared collection '{name}' (community entry {shared_id}).")
        self._set_collections_status(f"'{name}' is off the community list.")
        self.refresh_collections_view()

    def setup_logs_view(self):
        self.logs_view = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self.logs_view.grid_rowconfigure(0, weight=1)
        self.logs_view.grid_columnconfigure(0, weight=1)

        self.log_textbox = ctk.CTkTextbox(self.logs_view, wrap="word", state="disabled")
        self.log_textbox.grid(row=0, column=0, sticky="nsew")

    def setup_settings_view(self):
        self.settings_view = ScrollFrame(self.content_frame, fg_color="transparent")
        self.settings_view.grid_columnconfigure(0, weight=1)

        self.setup_appearance_section()

        self.setup_sources_section()

        self.setup_archive_auth_section()

        ctk.CTkLabel(self.settings_view, text="Download Settings", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=("Changes save automatically and apply to the next file that starts downloading - "
                  "no restart needed. If nothing feels better after a change, put it back on the "
                  "defaults; these are already tuned for archive.org."),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        self.download_ahead_var = ctk.BooleanVar(value=self.download_ahead)
        self.download_ahead_check = ctk.CTkCheckBox(
            self.settings_view,
            text="Download the next game while the current one extracts",
            variable=self.download_ahead_var, checkbox_width=18, checkbox_height=18,
            command=self.on_download_ahead_toggle,
        )
        self.download_ahead_check.pack(anchor="w")

        self.delete_after_extract_var = ctk.BooleanVar(value=delete_after_extract_enabled())
        self.delete_after_extract_check = ctk.CTkCheckBox(
            self.settings_view,
            text="Delete the downloaded archive once it has been extracted (saves space on disc games)",
            variable=self.delete_after_extract_var, checkbox_width=18, checkbox_height=18,
            command=self.on_delete_after_extract_toggle,
        )
        self.delete_after_extract_check.pack(anchor="w", pady=(6, 0))
        
        knobs = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        knobs.pack(anchor="w", fill="x")

        self.setting_sliders = {}
        self.setting_value_lbls = {}
        values = get_download_settings()

        for i, spec in enumerate(DOWNLOAD_SETTINGS):
            ctk.CTkLabel(knobs, text=spec.label, anchor="w", width=175).grid(
                row=i * 2, column=0, sticky="w", pady=(10, 0))

            slider = ctk.CTkSlider(
                knobs,
                from_=spec.minimum,
                to=spec.maximum,
                number_of_steps=max(1, int(round((spec.maximum - spec.minimum) / spec.step))),
                width=260,
                command=lambda raw, s=spec: self._on_setting_slider(s, raw),
            )
            slider.grid(row=i * 2, column=1, sticky="w", padx=(10, 12), pady=(10, 0))
            slider.set(values[spec.key])

            value_lbl = ctk.CTkLabel(knobs, text="", width=115, anchor="w")
            value_lbl.grid(row=i * 2, column=2, sticky="w", pady=(10, 0))

            ctk.CTkLabel(knobs, text=spec.help_text, text_color="gray", font=ctk.CTkFont(size=11),
                         wraplength=575, justify="left", anchor="w").grid(
                row=i * 2 + 1, column=0, columnspan=3, sticky="w", pady=(1, 0))

            self.setting_sliders[spec.key] = slider
            self.setting_value_lbls[spec.key] = value_lbl
            self._update_setting_label(spec, values[spec.key])

        self.reset_settings_btn = ctk.CTkButton(
            self.settings_view, text="Reset All to Defaults", width=160,
            fg_color="transparent", border_width=1, text_color=("black", "white"),
            command=self.reset_download_settings,
        )
        self.reset_settings_btn.pack(anchor="w", pady=(15, 0))

        self.settings_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                                wraplength=600, justify="left")
        self.settings_status_lbl.pack(anchor="w", pady=(6, 10))
        
        ctk.CTkLabel(self.settings_view, text="Library Index", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=("The game list is cached on disk for a week so the app starts instantly. "
                  "Refresh if archive.org has new files you don't see yet."),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        self.refresh_index_btn = ctk.CTkButton(self.settings_view, text="Refresh Library Index", command=self.refresh_index_action)
        self.refresh_index_btn.pack(anchor="w")

        self.refresh_status_lbl = ctk.CTkLabel(self.settings_view, text=f"{len(self.index.entries):,} games in library.", text_color="gray")
        self.refresh_status_lbl.pack(anchor="w", pady=(5, 0))
        
        self.update_section_lbl = ctk.CTkLabel(self.settings_view, text="Updates",
                                               font=ctk.CTkFont(size=18, weight="bold"))
        self.update_section_lbl.pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=(f"Retro Downloader {APP_VERSION}. It is part of Bman Downloader and updates "
                  "with it - check for updates in Bman Downloader's Settings."
                  if EMBEDDED_IN_MAIN_APP else f"You are running version {APP_VERSION}."),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        update_row = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        if not EMBEDDED_IN_MAIN_APP:
            update_row.pack(anchor="w", fill="x")

        self.check_update_btn = ctk.CTkButton(update_row, text="Check for Updates", command=self.check_update_action)
        self.check_update_btn.pack(side="left")

        self.install_update_btn = ctk.CTkButton(update_row, text="Download & Install", fg_color="#2b8a3e",
                                                hover_color="#236c31", command=self.install_update_action)
        self.install_update_btn.pack(side="left", padx=(10, 0))
        self.install_update_btn.pack_forget()

        self.update_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                              wraplength=600, justify="left")
        self.update_status_lbl.pack(anchor="w", pady=(6, 15))

        self.setup_archives_section()

    def setup_archives_section(self):
        ctk.CTkLabel(self.settings_view, text="Archives",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=".zip, .7z and .rar downloads are unpacked by the app itself - 7-Zip is not needed.",
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 5))
        self.archive_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                               wraplength=600, justify="left")
        self.archive_status_lbl.pack(anchor="w", pady=(0, 15))
        try:
            problems = [f".{kind}: {problem}" for kind, problem in archive_support().items() if problem]
        except Exception as exc:
            problems = [str(exc)]
        if problems:
            self.archive_status_lbl.configure(text="\n".join(problems), text_color="orange")
        else:
            self.archive_status_lbl.configure(text="zip, 7z and rar support: ready", text_color="green")

    def setup_archive_auth_section(self):
        creds = grab_archive_credentials()
        s3 = creds.get("s3") or {}

        self.api_keys_header_lbl = ctk.CTkLabel(self.settings_view, text="Archive.org API Keys",
                                                font=ctk.CTkFont(size=18, weight="bold"))
        self.api_keys_header_lbl.pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=("Click Grab API Keys, sign in to archive.org if you are not already, "
                  "then copy the two keys it shows you into the boxes below"),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(self.settings_view, text="Your S3 access key:").pack(anchor="w")
        self.s3_access_entry = ctk.CTkEntry(self.settings_view, width=400)
        self.s3_access_entry.pack(anchor="w", pady=(0, 10))
        self.s3_access_entry.insert(0, s3.get("access", ""))

        ctk.CTkLabel(self.settings_view, text="Your S3 secret key:").pack(anchor="w")
        self.s3_secret_entry = ctk.CTkEntry(self.settings_view, width=400, show="*")
        self.s3_secret_entry.pack(anchor="w", pady=(0, 15))
        self.s3_secret_entry.insert(0, s3.get("secret", ""))

        for widget in (self.s3_access_entry, self.s3_secret_entry):
            widget.bind("<Return>", lambda _event: self.save_s3_keys_action())

        button_row = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        button_row.pack(anchor="w")
        ctk.CTkButton(button_row, text="Save", command=self.save_s3_keys_action).pack(side="left")
        ctk.CTkButton(button_row, text="Grab API Keys", fg_color="transparent", border_width=1,
                      command=self.open_s3_keys_page).pack(side="left", padx=(10, 0))

        self.archive_status_lbl = ctk.CTkLabel(self.settings_view, text="", justify="left", wraplength=600)
        self.archive_status_lbl.pack(anchor="w", pady=(8, 0))
        self._set_archive_status(
            "API keys saved." if s3.get("access") and s3.get("secret")
            else "No API keys saved. Downloads still work, but keys avoid rate limits.",
            ok=bool(s3.get("access") and s3.get("secret")),
        )

    def _set_archive_status(self, text: str, ok: bool = True) -> None:
        try:
            self.archive_status_lbl.configure(text=text, text_color="green" if ok else "#d97070")
        except Exception:
            pass

    def _refresh_archive_credentials(self) -> None:
        creds = grab_archive_credentials()
        if self.index:
            self.index.cookies = creds.get("cookies", [])
            self.index.s3 = creds.get("s3", {})
            if not self.is_downloading:
                try:
                    self.downloader = Downloader(self.index)
                except Exception:
                    pass

    def open_s3_keys_page(self):
        try:
            webbrowser.open(ARCHIVE_S3_KEYS_URL)
        except Exception as exc:
            self._set_archive_status(f"Could not open a browser ({exc}). Go to {ARCHIVE_S3_KEYS_URL}", ok=False)

    def save_s3_keys_action(self):
        access = self.s3_access_entry.get().strip()
        secret = self.s3_secret_entry.get().strip()
        if not access or not secret:
            self._set_archive_status("Enter both an access key and a secret key.", ok=False)
            return
        save_archive_s3_keys(access, secret)
        self._refresh_archive_credentials()
        self._set_archive_status("API keys saved. The app is unlocked.", ok=True)
        log_event("Archive.org API keys saved.")
        self._enforce_api_keys(show_popup=False)

    def clear_s3_keys_action(self):
        _write_archive_credentials({"cookies": [], "s3": {}, "screenname": ""})
        self._refresh_archive_credentials()
        for entry in (getattr(self, "s3_access_entry", None), getattr(self, "s3_secret_entry", None)):
            try:
                entry.delete(0, "end")
            except Exception:
                pass
        self._set_archive_status("Stored API keys removed.", ok=False)

    def setup_sources_section(self):
        ctk.CTkLabel(self.settings_view, text="Sources",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(25, 5))
        editable = sources_editable()
        ctk.CTkLabel(
            self.settings_view,
            text=(f"Running from source: games come from {SOURCES_FILE} next to the .py files. "
                  f"Edit it, press Reload sources to try it, and it's built into the exe when you build."
                  if editable else
                  "The consoles and games this version can download are built in. New ones arrive "
                  "with app updates."),
            wraplength=600, justify="left",
        ).pack(anchor="w", pady=(0, 10))

        row = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        row.pack(anchor="w", fill="x")
        if editable:
            ctk.CTkButton(row, text=f"Open {SOURCES_FILE}", command=self.open_sources_file).pack(
                side="left", padx=(0, 10))
        self.reload_sources_btn = ctk.CTkButton(row, text="Reload sources", fg_color="#2b8a3e",
                                                hover_color="#236c31", command=self.refresh_index_action)
        self.reload_sources_btn._accent_locked = True
        self.reload_sources_btn.pack(side="left")

        self.sources_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                               wraplength=600, justify="left", anchor="w")
        self.sources_status_lbl.pack(anchor="w", fill="x", pady=(8, 0))
        self._refresh_sources_status()

    def _refresh_sources_status(self):
        index = self.index
        if index is not None and index.load_error:
            text, color = index.load_error, "#d97070"
        elif index is None or not index.sources:
            text, color = ((f"No sources yet - {SOURCES_FILE} is empty. Add archive.org items to it."
                            if sources_editable() else "This version has no game sources built in."),
                           "orange")
        else:
            per_console = {}
            for source in index.sources:
                per_console.setdefault(source.console, 0)
            for entry in index.entries:
                per_console[entry.console] = per_console.get(entry.console, 0) + 1
            parts = [f"{console_display_name(k)}: {per_console[k]:,}"
                     for k in CONSOLE_KEYS if k in per_console]
            text = (f"{len(index.sources)} source(s) across {len(per_console)} console(s) - "
                    + ", ".join(parts))
            color = "green"
            if index.failed:
                text += f"\nCould not load: {', '.join(index.failed)} (see Logs)"
                color = "orange"
        try:
            self.sources_status_lbl.configure(text=text, text_color=color)
        except Exception:
            pass

    def open_sources_file(self):
        path = sources_path()
        if not path.exists():
            try:
                write_sources_template(path)
            except OSError as exc:
                log_event(f"Could not create {path}: {exc}")
                return
        try:
            if os.name == "nt":
                os.startfile(str(path))       # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-t", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            log_event(f"Could not open {path}: {exc}")
            open_in_file_manager(path, select=True)

    def setup_appearance_section(self):
        ctk.CTkLabel(self.settings_view, text="Appearance",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(0, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=(""),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        swatch_grid = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        swatch_grid.pack(anchor="w", fill="x")

        self.accent_swatches = {}
        per_row = 8
        for i, (name, value) in enumerate(ACCENT_PRESETS):
            swatch = ctk.CTkButton(
                swatch_grid, text="", width=38, height=30, corner_radius=6,
                fg_color=value, hover_color=_shade(value, _HOVER_FACTOR, 1.0),
                border_width=0, border_color="#FFFFFF",
                command=lambda c=value, n=name: self.apply_accent_choice(c, n),
            )
            swatch._accent_locked = True
            swatch.grid(row=i // per_row, column=i % per_row, padx=(0, 6), pady=(0, 6))
            self.accent_swatches[normalize_hex(value)] = swatch

        accent_row = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        accent_row.pack(anchor="w", fill="x", pady=(4, 0))

        self.accent_pick_btn = ctk.CTkButton(accent_row, text="Custom...", width=100,
                                             command=self.pick_custom_accent)
        self.accent_pick_btn.pack(side="left")

        self.accent_hex_entry = ctk.CTkEntry(accent_row, width=110, placeholder_text="#1F6AA5")
        self.accent_hex_entry.pack(side="left", padx=(10, 0))
        self.accent_hex_entry.bind("<Return>", lambda _event: self.apply_accent_from_entry())

        self.accent_apply_btn = ctk.CTkButton(accent_row, text="Apply hex", width=90,
                                              command=self.apply_accent_from_entry)
        self.accent_apply_btn.pack(side="left", padx=(8, 0))

        self.accent_reset_btn = ctk.CTkButton(
            accent_row, text="Reset to blue", width=110, fg_color="transparent",
            border_width=1, text_color=("black", "white"),
            command=lambda: self.apply_accent_choice(DEFAULT_ACCENT, "Default blue"),
        )
        self.accent_reset_btn.pack(side="left", padx=(8, 0))

        self.accent_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                              wraplength=600, justify="left")
        self.accent_status_lbl.pack(anchor="w", pady=(6, 0))

        self._refresh_accent_ui()

    def _refresh_accent_ui(self, note: str | None = None, error: bool = False):
        active = normalize_hex(current_accent()) or DEFAULT_ACCENT

        try:
            self.accent_hex_entry.delete(0, "end")
            self.accent_hex_entry.insert(0, active)
        except Exception:
            pass

        for value, swatch in self.accent_swatches.items():
            try:
                swatch.configure(border_width=3 if value == active else 0)
            except Exception:
                pass

        if note is None:
            preset = next((n for n, v in ACCENT_PRESETS if normalize_hex(v) == active), None)
            note = f"Accent colour: {active}" + (f" ({preset})" if preset else " (custom)")
        self.accent_status_lbl.configure(text=note, text_color="#d97070" if error else "gray")

    def apply_accent_choice(self, value, label: str | None = None):
        accent = normalize_hex(value)
        if accent is None:
            self._refresh_accent_ui(note=f"'{value}' isn't a colour - use a hex code like #7C3AED.",
                                    error=True)
            return

        previous = current_accent()
        if accent == normalize_hex(previous):
            self._refresh_accent_ui()
            return

        set_accent(accent)
        try:
            retheme_widgets(self, previous, accent)
        except Exception as exc:
            print(f"Accent repaint hit a snag: {exc}")

        try:
            self.results_list.refresh()
        except Exception:
            pass

        save_accent(accent)
        self._refresh_accent_ui(note=f"Accent colour set to {label or accent} ({accent}).")

    def apply_accent_from_entry(self):
        self.apply_accent_choice(self.accent_hex_entry.get())

    def pick_custom_accent(self):
        try:
            from tkinter import colorchooser
            chosen = colorchooser.askcolor(color=current_accent(), parent=self,
                                           title="Pick an accent colour")[1]
        except Exception as exc:
            self._refresh_accent_ui(note=f"Could not open the colour picker: {exc}", error=True)
            return

        if chosen:
            self.apply_accent_choice(chosen)

    def _format_setting(self, spec: DownloadSetting, value) -> str:
        text = f"{value:.{spec.decimals}f}{spec.unit}" if spec.decimals else f"{int(value)}{spec.unit}"
        if abs(float(value) - float(spec.default)) < 1e-9:
            text += " (default)"
        return text

    def _update_setting_label(self, spec: DownloadSetting, value):
        self.setting_value_lbls[spec.key].configure(text=self._format_setting(spec, value))

    def _set_download_ahead(self, on: bool) -> None:
        with self._post_cond:
            self.download_ahead = on
            self._post_cond.notify_all()      # a download held back by the old value can go now

    def on_delete_after_extract_toggle(self):
        on = bool(self.delete_after_extract_var.get())
        data = load_settings()
        data[DELETE_AFTER_EXTRACT_KEY] = on
        save_settings(data)
        log_event("Delete archive after extracting: " + ("ON." if on else "OFF."))

    def on_download_ahead_toggle(self):
        on = bool(self.download_ahead_var.get())
        data = load_settings()
        data[DOWNLOAD_AHEAD_KEY] = on
        save_settings(data)
        self._set_download_ahead(on)

        if on:
            note = "Saved - the next queued game now downloads while the current one is extracting."
        else:
            note = "Saved - each queued game now finishes completely before the next one downloads."
            if self.is_downloading:
                note += " Anything already downloading or downloaded carries on."
        self.settings_status_lbl.configure(text=note, text_color="green")

    def _on_setting_slider(self, spec: DownloadSetting, raw_value):
        value = normalize_setting(spec, raw_value)
        self._update_setting_label(spec, value)
        job = self._setting_save_jobs.get(spec.key)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._setting_save_jobs[spec.key] = self.after(400, lambda s=spec, v=value: self._commit_setting(s, v))

    def _commit_setting(self, spec: DownloadSetting, value):
        self._setting_save_jobs[spec.key] = None
        value = normalize_setting(spec, value)

        data = load_settings()
        data[spec.key] = value
        save_settings(data)

        if self.downloader:
            apply_download_settings(self.downloader)

        note = f"Saved - {spec.label}: {self._format_setting(spec, value).replace(' (default)', '')}."
        if spec.key in ("max_segments", "min_segment_mb"):
            current = get_download_settings()
            full_mb = current["max_segments"] * current["min_segment_mb"]
            note += f" A file needs {full_mb} MB to use all {current['max_segments']} connections."
        if self.is_downloading:
            note += " The file downloading right now keeps its current split; this takes effect from the next file on."
            self.settings_status_lbl.configure(text=note, text_color="gray")
        else:
            self.settings_status_lbl.configure(text=note, text_color="green")

    def reset_download_settings(self):
        data = load_settings()
        for spec in DOWNLOAD_SETTINGS:
            job = self._setting_save_jobs.get(spec.key)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                self._setting_save_jobs[spec.key] = None
            data[spec.key] = spec.default
            self.setting_sliders[spec.key].set(spec.default)
            self._update_setting_label(spec, spec.default)

        data[DOWNLOAD_AHEAD_KEY] = False
        self.download_ahead_var.set(False)
        self._set_download_ahead(False)

        data[DELETE_AFTER_EXTRACT_KEY] = False
        self.delete_after_extract_var.set(False)

        save_settings(data)
        if self.downloader:
            apply_download_settings(self.downloader)
        self.settings_status_lbl.configure(text="All download settings restored to their defaults.", text_color="green")

    def open_update_settings(self):
        self.select_tab("settings")
        self.after(60, self._scroll_settings_to_updates)

    def _scroll_settings_to_updates(self):
        try:
            canvas = self.settings_view._parent_canvas
            self.settings_view.update_idletasks()
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)
            content_height = (bbox[3] - bbox[1]) if bbox else 0
            if content_height <= 0:
                return
            target_y = self.update_section_lbl.winfo_y() - 10
            canvas.yview_moveto(max(0.0, min(1.0, target_y / content_height)))
        except Exception:
            pass

    def _start_update_check(self, manual: bool = False):
        if EMBEDDED_IN_MAIN_APP:
            return                  # Bman Downloader checks for both
        if self._update_checking or self._update_installing:
            return
        self._update_checking = True
        try:
            self.check_update_btn.configure(state="disabled")
            self.update_status_lbl.configure(text="Checking for updates...", text_color="gray")
        except Exception:
            pass

        def work():
            manifest = check_for_update()
            post_ui(lambda m=manifest: self._on_update_checked(m, manual))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_checked(self, manifest: dict | None, manual: bool):
        self._update_checking = False
        self._pending_update = manifest
        try:
            self.check_update_btn.configure(state="normal")
        except Exception:
            pass

        if not manifest:
            self.update_banner_btn.grid_remove()
            self.install_update_btn.pack_forget()
            if manual:
                self.update_status_lbl.configure(
                    text=f"You are on the latest version ({APP_VERSION}).", text_color="green")
            else:
                self.update_status_lbl.configure(text="", text_color="gray")
            return

        notes = str(manifest.get("notes", "")).strip()
        text = f"Version {manifest.get('version')} is available (you have {APP_VERSION})."
        if notes:
            text += f"\n{notes}"
        if not IS_FROZEN:
            text += "\nRunning from source - updates only install into the packaged .exe."
        self.update_status_lbl.configure(text=text, text_color="green")
        self.update_banner_btn.grid()
        if IS_FROZEN:
            self.install_update_btn.pack(side="left", padx=(10, 0))
            if not manual:
                # Found at startup: install it straight away, no click needed.
                self._install_update(auto=True)

    def check_update_action(self):
        self._start_update_check(manual=True)

    def install_update_action(self):
        self._install_update(auto=False)

    def _app_busy(self) -> bool:
        return bool(self.is_downloading or self._bulk_running)

    def _set_update_banner(self, text: str, command=None):
        try:
            self.update_banner_btn.configure(text=text, command=command or self.open_update_settings)
            self.update_banner_btn.grid()
        except Exception:
            pass

    def _install_update(self, auto: bool):
        """Downloads and installs the pending update. auto=True is the startup path:
        downloading and swapping the exe is safe while the app is busy, only the
        restart waits until downloads and bulk jobs are done."""
        if not self._pending_update or self._update_installing:
            return
        if not auto and self._app_busy():
            self.update_status_lbl.configure(
                text="A download or bulk job is running - let it finish (or cancel it) before updating.",
                text_color="#d97070")
            return

        self._update_installing = True
        manifest = self._pending_update
        version = manifest.get("version", "")
        self.install_update_btn.configure(state="disabled")
        self.check_update_btn.configure(state="disabled")
        self.update_status_lbl.configure(text="Downloading update...", text_color="gray")
        self._set_update_banner(f"Updating to {version}...")
        log_event(f"Installing update {version} ({'automatic' if auto else 'requested'}).")

        slot = ("update-download",)
        banner_slot = ("update-banner",)

        def show(text, banner=None):
            post_ui_latest(slot, lambda t=text: self.update_status_lbl.configure(
                text=t, text_color="gray"))
            if banner:
                post_ui_latest(banner_slot, lambda b=banner: self._set_update_banner(b))

        def work():
            def prog(pct, received, total):
                if total:
                    show(f"Downloading update... {pct}% "
                         f"({format_size(received)} of {format_size(total)})",
                         f"Updating... {pct}%")
                else:
                    show(f"Downloading update... {format_size(received)}", "Updating...")

            ok = False
            try:
                new_exe, error = download_update(manifest, progress_callback=prog)
                if new_exe is not None:
                    show("Installing update...", "Installing update...")
                    ok, error = apply_update(new_exe)
            except Exception as exc:
                log_event(f"Update failed unexpectedly: {exc}")
                error = f"The update failed unexpectedly ({exc})."
            post_ui(lambda o=ok, e=error: self._on_update_installed(o, e))

        threading.Thread(target=work, daemon=True, name="app-update").start()

    def _on_update_installed(self, ok: bool, error: str):
        if not ok:
            self._update_installing = False
            self.install_update_btn.configure(state="normal")
            self.check_update_btn.configure(state="normal")
            self.update_status_lbl.configure(text=error, text_color="#d97070")
            self._set_update_banner("Update Available!")
            return

        self.install_update_btn.pack_forget()
        if self._app_busy():
            # The new version is already in place; restart once the work is done
            # so nothing in progress is cut off.
            self.update_status_lbl.configure(
                text="Update installed. The app will restart into the new version when "
                     "your downloads finish.", text_color="green")
            self._set_update_banner("Update ready - restart now", command=self._restart_into_update)
            self.after(3000, self._restart_when_idle)
            return
        self._restart_into_update()

    def _restart_when_idle(self):
        if self._app_busy():
            self.after(3000, self._restart_when_idle)
            return
        self._restart_into_update()

    def _restart_into_update(self):
        if self._app_busy():
            self.update_status_lbl.configure(
                text="Update installed. A download or bulk job is still running - the app will "
                     "restart when it finishes, or cancel it to restart now.", text_color="#d97070")
            self.open_update_settings()
            return
        self.update_banner_btn.grid_remove()
        if relaunch_app():
            self.update_status_lbl.configure(text="Update installed - restarting...",
                                             text_color="green")
            self.after(400, self.destroy)
        else:
            self.update_status_lbl.configure(
                text="Update installed. Close the app and open it again to run the new version.",
                text_color="green")

    def refresh_index_action(self):
        for button in (self.refresh_index_btn, getattr(self, "reload_sources_btn", None)):
            try:
                button.configure(state="disabled")
            except Exception:
                pass
        self.refresh_status_lbl.configure(text="Refreshing from archive.org...", text_color="gray")
        try:
            self.sources_status_lbl.configure(text="Reading the game list from archive.org...", text_color="gray")
        except Exception:
            pass

        def work():
            try:
                new_index = Index.load_combined(use_cache=False)
            except Exception as exc:
                post_ui(lambda e=exc: self._on_index_refreshed(None, str(e)))
                return
            post_ui(lambda i=new_index: self._on_index_refreshed(i, None))

        threading.Thread(target=work, daemon=True).start()

    def _on_index_refreshed(self, new_index: Index | None, error: str | None):
        for button in (self.refresh_index_btn, getattr(self, "reload_sources_btn", None)):
            try:
                button.configure(state="normal")
            except Exception:
                pass
        if new_index is None:
            self.refresh_status_lbl.configure(text=f"Refresh failed: {error}", text_color="#d97070")
            try:
                self.sources_status_lbl.configure(text=f"Reload failed: {error}", text_color="#d97070")
            except Exception:
                pass
            return

        self.index = new_index
        if not self.is_downloading:
            try:
                self.downloader = Downloader(self.index)
            except Exception:
                pass
        self.refresh_status_lbl.configure(text=f"Library refreshed - {len(new_index.entries):,} games.", text_color="green")
        self._refresh_sources_status()
        self._refresh_console_menu()
        self.current_results = []
        self._show_library_intro()

    def save_creds_action(self):
        try:
            save_archive_credentials(self.user_entry.get().strip(), self.sig_entry.get().strip())
        except AttributeError:
            return
        self._refresh_archive_credentials()


def row_stem_key(key: str) -> str:
    """A Downloads-tab key without its file extension: "GB/Tetris (World).zip" -> "GB/Tetris (World)"."""
    key = key or ""
    head, _, tail = key.rpartition("/")
    stem = Path(tail).stem
    return f"{head}/{stem}" if head else stem


def open_in_file_manager(path: Path, select: bool = False) -> None:
    try:
        if os.name == "nt":
            if select and path.exists():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(path if path.is_dir() else path.parent))     # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)] if select else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except Exception as exc:
        log_event(f"Could not open {path}: {exc}")


class DownloadItemFrame(ctk.CTkFrame):
    """One row on the Downloads tab. raw_filename is its key: "<console>/<file name>"."""

    def __init__(self, master, filename: str, entry: Files | None = None, cancel_callback: threading.Event | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.filename = filename
        self.raw_filename = filename
        self.entry = entry
        self.cancel_event = cancel_callback
        self.is_active = cancel_callback is not None

        self.name_lbl = ctk.CTkLabel(self, text=self._display_name(), font=ctk.CTkFont(weight="bold"))
        self.name_lbl.grid(row=0, column=0, sticky="w", padx=10, pady=(5, 0))

        self.status_lbl = ctk.CTkLabel(self, text="In Queue...")
        self.status_lbl.grid(row=0, column=1, sticky="e", padx=10, pady=(5, 0))

        self.progress_bar = ctk.CTkProgressBar(self)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=5)
        self.progress_bar.set(0.0)

        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 5))
        self.info_frame.grid_columnconfigure(0, weight=1)

        self.iso_status_lbl = ctk.CTkLabel(self.info_frame, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.iso_status_lbl.grid(row=0, column=0, sticky="w")

        self.btn_frame = ctk.CTkFrame(self.info_frame, fg_color="transparent")
        self.btn_frame.grid(row=0, column=1, sticky="e")

        self.cancel_btn = ctk.CTkButton(self.btn_frame, text="Cancel", width=70, height=24, fg_color="#b53737", hover_color="#8c2929", command=self.cancel_download)
        self.del_archive_btn = ctk.CTkButton(self.btn_frame, text="Delete Download", width=110, height=24, fg_color="#b53737", hover_color="#8c2929", command=self.delete_archive_file)
        self.del_extracted_btn = ctk.CTkButton(self.btn_frame, text="Delete Extracted", width=110, height=24, fg_color="#b53737", hover_color="#8c2929", command=self.delete_extracted_files)
        self.extract_zip_btn = ctk.CTkButton(self.btn_frame, text="Extract", width=80, height=24, fg_color=accent_fg(), hover_color=accent_hover(), command=self.action_extract_zip)
        self.show_btn = ctk.CTkButton(self.btn_frame, text="Show in Folder", width=100, height=24,
                                      fg_color="transparent", border_width=1, text_color=("black", "white"),
                                      command=self.show_in_folder)
        self.is_working = False

        if self.cancel_event:
            self.cancel_btn.pack(side="right", padx=2)

    def _display_name(self) -> str:
        head, _, tail = self.raw_filename.rpartition("/")
        return f"[{console_display_name(head)}] {tail}" if head else tail

    def _archive_path(self) -> Path:
        return DOWNLOAD_DIR / self.raw_filename

    def _extract_dir(self) -> Path:
        return extract_dir_for(self._archive_path())

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def destroy(self):
        settle = None
        try:
            settle = getattr(self.winfo_toplevel(), "_settle_downloads_soon", None)
        except Exception:
            pass
        super().destroy()
        if settle is not None:
            try:
                settle()        # the list just got shorter: keep the view on what is left
            except Exception:
                pass

    def update_progress(self, percent: int, text: str):
        if not self._alive():
            return
        self.progress_bar.set(percent / 100.0)
        self.status_lbl.configure(text=text)

    def update_status(self, text: str):
        if not self._alive():
            return
        self.status_lbl.configure(text=text)
        if "Completed" in text or "Error" in text or "Cancelled" in text or "Complete!" in text:
            self.is_active = False
            if self.cancel_event and self.cancel_btn.winfo_exists():
                self.cancel_btn.pack_forget()
            self.check_iso_and_files()

    def cancel_download(self):
        app = None
        try:
            app = self.winfo_toplevel()
        except Exception:
            pass
        if self.cancel_event:
            self.cancel_event.set()
        self.update_status("Cancelled")
        refill = getattr(app, "_fill_queue_rows", None)      # let the next queued item take this row's place
        if refill is not None:
            try:
                app.after_idle(refill)
            except Exception:
                pass

    def check_iso_and_files(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        archive_path = self._archive_path()
        if not archive_path.exists():
            # Same game, different extension (e.g. the .zip row for a .7z download).
            stem = archive_path.stem
            try:
                for candidate in archive_path.parent.iterdir():
                    if candidate.is_file() and candidate.stem == stem and candidate.suffix.lower() != ".part":
                        archive_path = candidate
                        self.raw_filename = download_key_for_path(candidate)
                        self.name_lbl.configure(text=self._display_name())
                        break
            except OSError:
                pass
        game_extract_dir = extract_dir_for(archive_path)

        has_archive = archive_path.exists()
        has_extracted = is_extracted(game_extract_dir)
        is_archive = has_archive and is_archive_file(archive_path)

        if has_extracted:
            self.iso_status_lbl.configure(
                text=f"Extracted to extracted/{row_stem_key(self.raw_filename)}", text_color="green")
        elif has_archive and is_archive:
            self.iso_status_lbl.configure(text="Downloaded (zipped) - most emulators open .zip ROMs directly",
                                          text_color="gray")
        elif has_archive:
            self.iso_status_lbl.configure(text="Downloaded - ready to load in an emulator", text_color="green")
        else:
            self.iso_status_lbl.configure(text="", text_color="gray")

        for btn in (self.cancel_btn, self.del_archive_btn, self.del_extracted_btn,
                    self.extract_zip_btn, self.show_btn):
            btn.pack_forget()

        if has_extracted:
            self.del_extracted_btn.pack(side="right", padx=2)
        if has_archive:
            self.del_archive_btn.pack(side="right", padx=2)

        if self.is_active and self.cancel_event:
            self.cancel_btn.pack(side="right", padx=2)
        else:
            if is_archive and not has_extracted and not self.is_working:
                self.extract_zip_btn.pack(side="right", padx=2)
            if has_archive or has_extracted:
                self.show_btn.pack(side="right", padx=2)

        if not has_archive and not has_extracted and not (self.is_active and self.cancel_event):
            for slot in ("dl-progress", "dl-status", "row-status"):
                drop_ui_slot((slot, id(self)))
            self.destroy()

    def show_in_folder(self):
        extracted = self._extract_dir()
        if is_extracted(extracted):
            open_in_file_manager(extracted)
        else:
            open_in_file_manager(self._archive_path(), select=True)

    def action_extract_zip(self):
        archive_path = self._archive_path()
        if not archive_path.exists():
            return

        def run_extraction():
            self.is_working = True
            post_ui(lambda: self.update_status("Extracting..."))
            try:
                def prog_cb(msg):
                    post_ui_latest(("row-status", id(self)),
                                   lambda m=msg: self.update_status(m))
                success = handle_post_download(archive_path, entry=self.entry,
                                               conversion_mode=POST_MODE_EXTRACT, progress_callback=prog_cb)
                if success:
                    post_ui(lambda: self.update_status("Extracted Successfully!"))
                else:
                    post_ui(lambda: self.update_status("Extraction Failed."))
            except Exception as e:
                post_ui(lambda e=e: self.update_status(f"Error: {e}"))
            self.is_working = False
            post_ui(self.check_iso_and_files)

        threading.Thread(target=run_extraction, daemon=True).start()

    def delete_archive_file(self):
        archive_path = self._archive_path()
        try:
            if archive_path.exists():
                archive_path.unlink()
            self.check_iso_and_files()
        except Exception as e:
            print(f"Error deleting download: {e}")

    def delete_extracted_files(self):
        game_extract_dir = self._extract_dir()
        try:
            root = EXTRACT_DIR.resolve()
            if game_extract_dir.exists() and root in game_extract_dir.resolve().parents:
                shutil.rmtree(game_extract_dir)
            self.check_iso_and_files()
        except Exception as e:
            print(f"Error deleting extracted files: {e}")
