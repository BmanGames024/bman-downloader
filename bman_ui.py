"""Bman Downloader - the interface: dialogs, the list widgets and the main window.

All the logic it calls lives in bman_core.py."""
from __future__ import annotations

import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bman_core import (
    ACCENT_PRESETS, APP_VERSION, APP_WINDOW, ARCHIVE_S3_KEYS_URL, ArchiveError,
    COLLECTION_EXPORT_EXT, COMMUNITY_CACHE_TTL_SECONDS, CommunityEntry, DEFAULT_ACCENT,
    DEFAULT_FTP_PARALLEL, DEFAULT_FTP_PORT, DEFAULT_FTP_TIMEOUT, DEFAULT_HDD_PARALLEL,
    DOWNLOAD_DIR, clean_incomplete_downloads,
    DELETE_AFTER_UPLOAD_KEY, delete_after_upload_enabled, delete_local_copies,
    DOWNLOAD_SETTINGS, DownloadSetting, Downloader, EXTRACT_DIR, FATX_IMPORT_ERROR,
    FTP_STATE, Files, FtpConfig, GameCollection, HDD_COMMON_PATHS, HDD_COMMON_PATHS_NORMAL,
    HDD_LOCK,
    HDD_MAX_NAME, HDD_MODE_FATX, HDD_MODE_NORMAL, HDD_POLL_MS, HDD_RENAME_AT, HDD_STATE,
    HddConfig,
    IS_FROZEN, Index, LOG_QUEUE, MAX_FTP_PARALLEL, MAX_HDD_PARALLEL, NORMAL_MAX_NAME,
    NormalPartition,
    REGION_FILTER_ALL, REGION_FILTER_DEFAULT, REGION_FILTER_LABELS, REGION_SETTING_KEY,
    RAR_7Z_SUFFIXES, RUN_DIR, archive_support,
    TextRedirector, UI_PUMP_MS,
    UPLOAD_KIND_LABELS, _BULK_STATUS_SLOT, _HOVER_FACTOR, _drain_ui_pending,
    _has_iso, _shade, _write_archive_credentials, accent_fg, accent_hover,
    analyze_upload_target, apply_download_settings, apply_update, bulk_convert_iso,
    check_for_update, cleanup_update_leftovers, community_can_share, community_entry_stamp,
    content_scan_depth,
    contrast_text_for, current_accent, delete_collection,
    download_update, drop_ui_slot, entry_is_content, entry_matches_region,
    export_collection, fatx360, fetch_community_collection, fetch_cover, covers_offline, cover_warm_up, is_og_xbox_entry,
    load_saved_image, save_image, prefetch_covers, COVER_PREFETCH_COUNT,
    ITEM_KINDS, ITEM_KIND_LABELS, item_kind, count_item_kinds, describe_item_counts, item_label,
    fetch_community_index,
    find_content_upload_roots, find_extracted_isos,
    find_unextracted_archives, format_size, ftp_connect, ftp_join, ftp_mode_name,
    ftp_list_dir, ftp_normalize_path, ftp_parent, ftp_test_connection, ftp_upload_tree,
    get_collection, get_download_settings, grab_archive_credentials,
    handle_post_download, has_archive_keys, hdd_join, hdd_normalize_path, hdd_open_volume,
    hdd_name_problem, hdd_parent, hdd_suggest_name, hdd_test_connection,
    hdd_title_needs_rename, hdd_upload_folder, is_dlc_entry, list_normal_partitions, load_collections,
    load_cached_community_collection, load_community_cache, load_ftp_config, load_hdd_config,
    load_settings, log_event,
    normalize_hex, normalize_setting, post_ui, post_ui_latest, predict_upload_folders,
    prune_community_list_cache,
    publish_community_collection,
    read_collection_file, refresh_games_against_index, region_filter_for_label,
    relaunch_app, reset_scrollable_frame, retheme_widgets,
    safe_collection_filename, save_accent, save_archive_credentials,
    save_archive_s3_keys, save_community_cache, save_ftp_config, save_hdd_config,
    save_settings, set_accent, unique_collection_name, unshare_community_collection,
    CATALOGS, Catalog, GAMERPIC_CATALOG, catalog_display_name, catalog_entry_for_filename,
    catalog_folder_kind, catalog_for_entry, catalog_name_hash, catalog_title_id,
    catalog_upload_destination, fetch_gamerpic_ids, fetch_gamerpic_image, gamerpic_image_url,
    gamerpic_site_url, is_catalog_entry, load_catalog_index, load_gamerpic_site_titles,
    search_catalog, THEME_CATALOG, fetch_theme_wallpaper, load_theme_wallpapers,
    theme_wallpaper_page_url, theme_wallpaper_urls,
    upload_game_to_hdd, upload_game_via_ftp, upsert_collection,
    FORCE_TITLE_ID_TO_CONTENT, FORCED_CONTENT_KINDS, forced_content_destination,
    xefu_lookup, xefu_versions_text,
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


class FtpBrowserDialog(ctk.CTkToplevel):

    def __init__(self, master, cfg: FtpConfig, start_path: str = "/",
                 title: str = "Choose FTP upload destination",
                 subtitle: str | None = None):
        super().__init__(master)
        self.cfg = cfg
        self.result: str | None = None
        self._ftp = None
        self._busy = False
        self._current = ftp_normalize_path(start_path or "/")

        self.title(title)
        self.geometry("620x520")
        self.minsize(480, 400)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkLabel(
            self, anchor="w", justify="left", wraplength=560,
            text=subtitle or f"Pick the folder on {cfg.host} to upload into. "
                             "Click a folder to open it; anything missing gets created on upload.",
        )
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 8))

        path_row = ctk.CTkFrame(self, fg_color="transparent")
        path_row.grid(row=1, column=0, sticky="ew", padx=15)
        path_row.grid_columnconfigure(0, weight=1)

        self.path_entry = ctk.CTkEntry(path_row, placeholder_text="/Hdd1/Games")
        self.path_entry.grid(row=0, column=0, sticky="ew")
        self.path_entry.insert(0, self._current)
        self.path_entry.bind("<Return>", lambda _e: self._go_typed())

        self.go_btn = ctk.CTkButton(path_row, text="Go", width=60, command=self._go_typed)
        self.go_btn.grid(row=0, column=1, padx=(8, 0))

        nav_row = ctk.CTkFrame(self, fg_color="transparent")
        nav_row.grid(row=2, column=0, sticky="ew", padx=15, pady=(8, 6))

        self.up_btn = ctk.CTkButton(nav_row, text="Up one level", width=110,
                                    command=lambda: self._navigate(ftp_parent(self._current)))
        self.up_btn.pack(side="left")

        self.refresh_btn = ctk.CTkButton(nav_row, text="Refresh", width=80,
                                         command=lambda: self._navigate(self._current))
        self.refresh_btn.pack(side="left", padx=(8, 0))

        self.newdir_btn = ctk.CTkButton(nav_row, text="New Folder", width=95,
                                        command=self._new_folder)
        self.newdir_btn.pack(side="left", padx=(8, 0))

        self.listing = ScrollFrame(self, label_text="Folders")
        self.listing.grid(row=3, column=0, sticky="nsew", padx=15)
        self.listing.grid_columnconfigure(0, weight=1)

        self.status_lbl = ctk.CTkLabel(self, text="Connecting...", text_color="gray",
                                       anchor="w", justify="left", wraplength=560)
        self.status_lbl.grid(row=4, column=0, sticky="ew", padx=15, pady=(6, 0))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=5, column=0, sticky="ew", padx=15, pady=(8, 15))

        self.use_btn = ctk.CTkButton(button_row, text="Upload Here", fg_color="#2b8a3e",
                                     hover_color="#236c31", command=self._on_use)
        self.use_btn.pack(side="right")

        ctk.CTkButton(button_row, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("black", "white"),
                      command=self._on_cancel).pack(side="right", padx=(0, 8))

        self.after(120, self._grab)
        self._navigate(self._current)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.go_btn, self.up_btn, self.refresh_btn, self.newdir_btn, self.use_btn):
            try:
                widget.configure(state=state)
            except Exception:
                pass

    def _go_typed(self):
        self._navigate(self.path_entry.get())

    def _navigate(self, path: str):
        if self._busy:
            return
        self._set_busy(True)
        target = ftp_normalize_path(path)
        try:
            self.status_lbl.configure(text=f"Listing {target} ...", text_color="gray")
        except Exception:
            pass
        threading.Thread(target=self._navigate_worker, args=(target,), daemon=True).start()

    def _navigate_worker(self, target: str):
        for attempt in range(2):
            try:
                if self._ftp is None:
                    self._ftp = ftp_connect(self.cfg)
                actual, entries = ftp_list_dir(self._ftp, target)
                post_ui(lambda a=actual, e=entries: self._render(a, e))
                return
            except Exception as exc:
                try:
                    if self._ftp is not None:
                        self._ftp.close()
                except Exception:
                    pass
                self._ftp = None
                if attempt == 1:
                    post_ui(lambda e=exc: self._render_error(e))

    def _render_error(self, exc):
        try:
            self.status_lbl.configure(text=f"Could not open that folder: {exc}", text_color="#d97070")
        except Exception:
            pass
        self._set_busy(False)

    def _render(self, actual: str, entries: list[tuple[str, bool]]):
        self._current = actual
        try:
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, actual)
            self.listing.configure(label_text=f"Folders in {actual}")
            for widget in self.listing.winfo_children():
                widget.destroy()

            folders = [name for name, is_dir in entries if is_dir]
            files = [name for name, is_dir in entries if not is_dir]

            if not folders:
                ctk.CTkLabel(self.listing, text="(no sub-folders here)", text_color="gray",
                             anchor="w").pack(fill="x", pady=6, padx=4)
            for name in folders:
                ctk.CTkButton(
                    self.listing, text=f"{name}/", anchor="w", height=28,
                    fg_color="transparent", text_color=("black", "white"),
                    hover_color=("gray70", "gray30"),
                    command=lambda n=name: self._navigate(ftp_join(self._current, n)),
                ).pack(fill="x", pady=1)

            note = f"{len(folders)} folder(s)"
            if files:
                note += f", {len(files)} file(s) (hidden)"
            self.status_lbl.configure(text=f"{note}. 'Upload Here' uses {actual}.", text_color="gray")
        except Exception:
            pass
        reset_scrollable_frame(self.listing)
        self._set_busy(False)

    def _new_folder(self):
        if self._busy:
            return
        prompt = ctk.CTkInputDialog(text=f"New folder inside {self._current}:", title="New Folder")
        name = (prompt.get_input() or "").strip()
        self.after(120, self._grab)
        if not name:
            return
        self._set_busy(True)
        self.status_lbl.configure(text=f"Creating {name} ...", text_color="gray")

        def worker():
            try:
                if self._ftp is None:
                    self._ftp = ftp_connect(self.cfg)
                self._ftp.cwd(self._current)
                self._ftp.mkd(name)
                target = ftp_join(self._current, name)
                post_ui(lambda t=target: self._after_mkdir(t))
            except Exception as exc:
                post_ui(lambda e=exc: self._render_error(e))

        threading.Thread(target=worker, daemon=True).start()

    def _after_mkdir(self, target: str):
        self._set_busy(False)
        self._navigate(target)

    def _close_session(self):
        try:
            if self._ftp is not None:
                self._ftp.quit()
        except Exception:
            try:
                if self._ftp is not None:
                    self._ftp.close()
            except Exception:
                pass
        self._ftp = None

    def _on_use(self):
        self.result = ftp_normalize_path(self.path_entry.get() or self._current)
        self._close_session()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self._close_session()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class FtpFormatDialog(ctk.CTkToplevel):

    def __init__(self, master, title: str = "Upload All via FTP"):
        super().__init__(master)
        self.result: str | None = None

        self.title(title)
        self.geometry("520x330")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        ctk.CTkLabel(self, text="What format should everything be in?",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(
            self, wraplength=470, justify="left", text_color="gray",
            text=("Any archive that has not been extracted yet gets extracted first, then every ISO "
                  "found is converted to the format you pick, then the whole lot is uploaded. "
                  "Anything already in that format is skipped rather than redone."),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        self.mode_var = ctk.StringVar(value="2")
        for value, label in (
            ("2", "Convert to GOD (games on demand), then upload"),
            ("1", "Extract XISO (loose files), then upload"),
            ("3", "Do not convert anything - upload what is already converted/extracted"),
        ):
            ctk.CTkRadioButton(self, text=label, variable=self.mode_var, value=value).pack(
                anchor="w", padx=25, pady=5)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(18, 20), side="bottom")

        ctk.CTkButton(row, text="Choose Destination...", fg_color="#2b8a3e",
                      hover_color="#236c31", command=self._on_ok).pack(side="right")
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

    def _finish(self, value):
        self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_ok(self):
        self._finish(self.mode_var.get())

    def _on_cancel(self):
        self._finish(None)


def prompt_ftp_destination(master, cfg: FtpConfig, subtitle: str | None = None) -> str | None:
    dialog = FtpBrowserDialog(master, cfg, start_path=cfg.default_path or "/", subtitle=subtitle)
    master.wait_window(dialog)
    chosen = dialog.result
    if chosen:
        cfg.default_path = chosen
        try:
            save_ftp_config(cfg)
        except Exception:
            pass
    return chosen


class HddBrowserDialog(ctk.CTkToplevel):

    def __init__(self, master, cfg: HddConfig, start_path: str = "/",
                 title: str = "Choose hard drive destination",
                 subtitle: str | None = None):
        super().__init__(master)
        self.cfg = cfg
        self.result: str | None = None
        self._busy = False
        self._current = hdd_normalize_path(start_path or "/")

        self.title(title)
        self.geometry("640x560")
        self.minsize(480, 400)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            self, anchor="w", justify="left", wraplength=580,
            text=subtitle or (f"Pick the folder on {cfg.label} to copy into. "
                              "Click a folder to open it; anything missing gets created."),
        ).grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 8))

        path_row = ctk.CTkFrame(self, fg_color="transparent")
        path_row.grid(row=1, column=0, sticky="ew", padx=15)
        path_row.grid_columnconfigure(0, weight=1)

        self.path_entry = ctk.CTkEntry(path_row, placeholder_text="/Content/0000000000000000")
        self.path_entry.grid(row=0, column=0, sticky="ew")
        self.path_entry.insert(0, self._current)
        self.path_entry.bind("<Return>", lambda _e: self._go_typed())

        self.go_btn = ctk.CTkButton(path_row, text="Go", width=60, command=self._go_typed)
        self.go_btn.grid(row=0, column=1, padx=(8, 0))

        nav_row = ctk.CTkFrame(self, fg_color="transparent")
        nav_row.grid(row=2, column=0, sticky="ew", padx=15, pady=(8, 4))

        self.up_btn = ctk.CTkButton(nav_row, text="Up one level", width=110,
                                    command=lambda: self._navigate(hdd_parent(self._current)))
        self.up_btn.pack(side="left")

        self.refresh_btn = ctk.CTkButton(nav_row, text="Refresh", width=80,
                                         command=lambda: self._navigate(self._current))
        self.refresh_btn.pack(side="left", padx=(8, 0))

        self.newdir_btn = ctk.CTkButton(nav_row, text="New Folder", width=95,
                                        command=self._new_folder)
        self.newdir_btn.pack(side="left", padx=(8, 0))

        shortcut_row = ctk.CTkFrame(self, fg_color="transparent")
        shortcut_row.grid(row=3, column=0, sticky="ew", padx=15, pady=(0, 6))
        ctk.CTkLabel(shortcut_row, text="Jump to:", text_color="gray",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 6))
        shortcut_paths = HDD_COMMON_PATHS_NORMAL if cfg.mode == HDD_MODE_NORMAL else HDD_COMMON_PATHS
        for label, target in shortcut_paths:
            ctk.CTkButton(shortcut_row, text=label, height=22,
                          width=max(90, 8 * len(label)), font=ctk.CTkFont(size=11),
                          fg_color="transparent", border_width=1,
                          text_color=("black", "white"),
                          command=lambda t=target: self._navigate(t)).pack(side="left", padx=(0, 6))

        self.listing = ScrollFrame(self, label_text="Folders")
        self.listing.grid(row=4, column=0, sticky="nsew", padx=15)
        self.listing.grid_columnconfigure(0, weight=1)

        self.status_lbl = ctk.CTkLabel(self, text="Reading the drive...", text_color="gray",
                                       anchor="w", justify="left", wraplength=580)
        self.status_lbl.grid(row=5, column=0, sticky="ew", padx=15, pady=(6, 0))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.grid(row=6, column=0, sticky="ew", padx=15, pady=(8, 15))

        self.use_btn = ctk.CTkButton(button_row, text="Upload Here", fg_color="#2b8a3e",
                                     hover_color="#236c31", command=self._on_use)
        self.use_btn.pack(side="right")

        ctk.CTkButton(button_row, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("black", "white"),
                      command=self._on_cancel).pack(side="right", padx=(0, 8))

        self.after(120, self._grab)
        self._navigate(self._current)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in (self.go_btn, self.up_btn, self.refresh_btn, self.newdir_btn, self.use_btn):
            try:
                btn.configure(state=state)
            except Exception:
                pass

    def _go_typed(self):
        self._navigate(self.path_entry.get() or "/")

    def _navigate(self, path: str):
        if self._busy:
            return
        target = hdd_normalize_path(path)
        self._set_busy(True)
        self.status_lbl.configure(text=f"Reading {target} ...", text_color="gray")
        threading.Thread(target=self._navigate_worker, args=(target,), daemon=True).start()

    def _navigate_worker(self, target: str):
        dev = None
        try:
            with HDD_LOCK:
                dev, vol = hdd_open_volume(self.cfg, writable=False)
                entries = vol.listdir(target)
                names = [e.name for e in entries if e.is_dir]
                files = sum(1 for e in entries if not e.is_dir)
        except Exception as exc:
            post_ui(lambda e=exc: self._render_error(e))
            return
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
        post_ui(lambda: self._render(target, names, files))

    def _render_error(self, exc):
        self._set_busy(False)
        message = str(exc) or exc.__class__.__name__
        self.status_lbl.configure(text=f"Could not read that folder: {message}",
                                  text_color="#d97070")

    def _render(self, actual: str, folders: list[str], file_count: int):
        self._current = actual
        self.path_entry.delete(0, "end")
        self.path_entry.insert(0, actual)

        for child in self.listing.winfo_children():
            child.destroy()

        if not folders:
            ctk.CTkLabel(self.listing, text="(no sub-folders here)", text_color="gray",
                         anchor="w").grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        for row, name in enumerate(sorted(folders, key=str.lower)):
            ctk.CTkButton(self.listing, text=f"[DIR]  {name}", anchor="w", height=26,
                          fg_color="transparent", hover_color=("gray80", "gray28"),
                          text_color=("gray10", "gray90"),
                          command=lambda n=name: self._navigate(hdd_join(self._current, n)),
                          ).grid(row=row, column=0, sticky="ew", padx=2, pady=1)

        reset_scrollable_frame(self.listing)
        self._set_busy(False)
        detail = f"{len(folders)} folder(s)"
        if file_count:
            detail += f", {file_count} file(s)"
        self.status_lbl.configure(text=f"{actual} - {detail}", text_color="gray")

    def _new_folder(self):
        if self._busy:
            return
        dialog = ctk.CTkInputDialog(text=f"Folder name to create in {self._current}:",
                                    title="New folder")
        name = (dialog.get_input() or "").strip()
        self.after(120, self._grab)
        if not name:
            return
        max_name = NORMAL_MAX_NAME if self.cfg.mode == HDD_MODE_NORMAL else HDD_MAX_NAME
        if len(name) > max_name:
            message = (f"Names cap at {max_name} characters on this drive."
                       if self.cfg.mode == HDD_MODE_NORMAL
                       else f"FATX names cap at {max_name} characters.")
            self.status_lbl.configure(text=message, text_color="#d97070")
            return

        target = self._current
        self._set_busy(True)
        self.status_lbl.configure(text=f"Creating {name} ...", text_color="gray")

        def worker():
            dev = None
            try:
                with HDD_LOCK:
                    dev, vol = hdd_open_volume(self.cfg, writable=True)
                    vol.ensure_dir(hdd_join(target, name))
            except Exception as exc:
                post_ui(lambda e=exc: self._render_error(e))
                return
            finally:
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
            post_ui(lambda t=hdd_join(target, name): self._after_mkdir(t))

        threading.Thread(target=worker, daemon=True).start()

    def _after_mkdir(self, target: str):
        self._set_busy(False)
        self._navigate(target)

    def _on_use(self):
        self.result = hdd_normalize_path(self.path_entry.get() or self._current)
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


def prompt_hdd_destination(master, cfg: HddConfig, subtitle: str | None = None) -> str | None:
    dialog = HddBrowserDialog(master, cfg, start_path=cfg.default_path or "/", subtitle=subtitle)
    master.wait_window(dialog)
    chosen = dialog.result
    if chosen:
        cfg.default_path = chosen
        try:
            save_hdd_config(cfg)
        except Exception:
            pass
    return chosen


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




class HddRenameDialog(ctk.CTkToplevel):
    """Asks for a shorter name for each game whose folder name is too long to upload.

    Used for the Xbox drive, an ordinary drive and FTP alike ('where' says which).

    One window walks through all of them. Every answer is handed to on_pick(index, name)
    straight away; name is None for a title that was left as it is (window closed).
    """

    def __init__(self, master, titles: list, on_pick, taken=None, still_wanted=None,
                 on_finished=None, limit: int = HDD_RENAME_AT, where: str = "the HDD"):
        super().__init__(master)
        self._where = where
        self._titles = list(titles)
        self._on_pick = on_pick
        self._taken = {str(name).lower() for name in (taken or ())}
        self._still_wanted = still_wanted
        self._on_finished = on_finished
        self._limit = limit
        self._index = -1
        self._finished = False
        self.results: dict = {}

        self.title("Name Too Long")
        self.geometry("540x330")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.heading_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=16, weight="bold"),
                                        anchor="w", justify="left")
        self.heading_lbl.pack(anchor="w", padx=20, pady=(20, 6))
        self.message_lbl = ctk.CTkLabel(self, text="", wraplength=490, justify="left", anchor="w")
        self.message_lbl.pack(anchor="w", fill="x", padx=20)
        self.title_lbl = ctk.CTkLabel(self, text="", wraplength=490, justify="left", anchor="w",
                                      text_color="gray")
        self.title_lbl.pack(anchor="w", fill="x", padx=20, pady=(6, 10))

        ctk.CTkLabel(self, text="What do you want it renamed to?", anchor="w").pack(fill="x", padx=20)
        self.name_var = tk.StringVar()
        self.name_entry = ctk.CTkEntry(self, textvariable=self.name_var)
        self.name_entry.pack(fill="x", padx=20, pady=(2, 2))
        self.name_entry.bind("<Return>", lambda e: self._on_rename())
        self.name_var.trace_add("write", lambda *_: self._on_typed())

        self.count_lbl = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11), anchor="w",
                                      text_color="gray")
        self.count_lbl.pack(fill="x", padx=20)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(10, 20), side="bottom")
        ctk.CTkButton(row, text="Rename", width=110, fg_color="#2b8a3e", hover_color="#236c31",
                      command=self._on_rename).pack(side="right")
        self.all_btn = ctk.CTkButton(row, text="", fg_color="transparent", border_width=1,
                                     text_color=("black", "white"), command=self._on_use_all)

        self._show_next()
        if not self._finished:
            self.after(120, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
            self._focus_entry()
        except Exception:
            pass

    def _focus_entry(self):
        try:
            self.name_entry.focus_set()
            self.name_entry.select_range(0, "end")
            self.name_entry.icursor("end")
        except Exception:
            pass

    def _wanted(self, index: int) -> bool:
        if self._still_wanted is None:
            return True
        try:
            return bool(self._still_wanted(index))
        except Exception:
            return True

    def _deliver(self, index: int, name) -> None:
        if name:
            self.results[index] = name
            self._taken.add(name.lower())
        try:
            self._on_pick(index, name)
        except Exception as exc:
            log_event(f"Rename prompt: could not apply a name ({exc})")

    def _left(self) -> int:
        return sum(1 for i in range(self._index, len(self._titles)) if self._wanted(i))

    def _show_next(self) -> None:
        while True:
            self._index += 1
            if self._index >= len(self._titles):
                self._finish()
                return
            if self._wanted(self._index):
                break
            self._deliver(self._index, None)      # cancelled in the meantime: nothing to ask

        title = self._titles[self._index]
        total = len(self._titles)
        heading = f"This name is too long for {self._where}"
        if total > 1:
            heading += f"  ({self._index + 1} of {total})"
        self.heading_lbl.configure(text=heading)
        self.message_lbl.configure(
            text=(f"It is {len(title)} characters, and it has to be {self._limit} or fewer. "
                  f"Only the uploaded folder gets the new name - nothing on this computer "
                  f"is renamed."))
        self.title_lbl.configure(text=title)
        self.name_var.set(hdd_suggest_name(title, self._limit, self._taken))

        left = self._left()
        if left > 1:
            self.all_btn.configure(text=f"Use suggested names for all {left:,} left")
            if not self.all_btn.winfo_ismapped():
                self.all_btn.pack(side="right", padx=(0, 8))
        else:
            self.all_btn.pack_forget()

        fit_dialog_to_content(self)
        self._focus_entry()

    def _problem(self, name: str):
        problem = hdd_name_problem(name, self._limit)
        if problem:
            return problem
        if name.strip().lower() in self._taken:
            return "Another game is already using that name."
        return None

    def _on_typed(self) -> None:
        name = self.name_var.get()
        problem = self._problem(name) if name.strip() else None
        text = f"{len(name.strip())} / {self._limit} characters"
        if problem:
            text += f"  -  {problem}"
        try:
            self.count_lbl.configure(text=text, text_color="#d97070" if problem else "gray")
        except Exception:
            pass

    def _on_rename(self) -> None:
        if self._finished:
            return
        name = self.name_var.get().strip()
        problem = self._problem(name)
        if problem:
            self.count_lbl.configure(
                text=f"{len(name)} / {self._limit} characters  -  {problem}", text_color="#d97070")
            return
        self._deliver(self._index, name)
        self._show_next()

    def _on_use_all(self) -> None:
        if self._finished:
            return
        for index in range(self._index, len(self._titles)):
            name = None
            if self._wanted(index):
                name = hdd_suggest_name(self._titles[index], self._limit, self._taken)
            self._deliver(index, name)
        self._index = len(self._titles)
        self._finish()

    def _on_close(self) -> None:
        if self._finished:
            return
        for index in range(max(0, self._index), len(self._titles)):
            self._deliver(index, None)
        self._index = len(self._titles)
        self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        if self._on_finished is not None:
            try:
                self._on_finished()
            except Exception as exc:
                log_event(f"Rename prompt: {exc}")


def ask_hdd_rename(master, title: str, taken=None, where: str = "the HDD") -> str | None:
    """Ask for a shorter upload name for one game. None when the window was closed."""
    try:
        dialog = HddRenameDialog(master, [title], on_pick=lambda _index, _name: None, taken=taken,
                                 where=where)
        master.wait_window(dialog)
        return dialog.results.get(0)
    except Exception as exc:
        log_event(f"Rename prompt failed ({title}): {exc}")
        return None


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
            label = f"{collection.name}   ({describe_item_counts(collection.kind_counts())})"
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
                 ftp_online: bool, hdd_online: bool,
                 ftp_label: str = "", hdd_label: str = "",
                 intro: str | None = None, window_title: str = "Download Collection",
                 show_convert: bool = True):
        super().__init__(master)
        self.result: dict | None = None

        rows = 3 + int(bool(ftp_online)) + int(bool(hdd_online))
        self.title(window_title)
        self.geometry(f"560x{280 + rows * 30}")
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

        # Themes and profile pic packs are never converted, so without any games
        # in the batch there is nothing to pick here.
        self.mode_var = ctk.StringVar(value="2" if show_convert else "3")
        if show_convert:
            ctk.CTkLabel(self, text="Convert each game to:",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=20)
            for value, label in (
                ("2", "GOD (games on demand)"),
                ("1", "Extracted XISO (loose files)"),
                ("3", "Just download - no conversion"),
            ):
                ctk.CTkRadioButton(self, text=label, variable=self.mode_var, value=value).pack(
                    anchor="w", padx=30, pady=3)
            ctk.CTkLabel(self, text="Sources that can't be converted (title ID content, themes, "
                                    "profile pics) are always downloaded as-is, whatever is picked here.",
                         text_color="gray", font=ctk.CTkFont(size=11), wraplength=510,
                         justify="left", anchor="w").pack(fill="x", padx=30, pady=(2, 12))

        ctk.CTkLabel(self, text="When each one is finished:",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=20)
        self.dest_var = ctk.StringVar(value="local")
        ctk.CTkRadioButton(self, text="Keep the files on this PC", variable=self.dest_var,
                           value="local").pack(anchor="w", padx=30, pady=3)
        if ftp_online:
            ctk.CTkRadioButton(self, text=f"Upload via FTP{f' to {ftp_label}' if ftp_label else ''}"
                                          f" - you'll pick the folder next",
                               variable=self.dest_var, value="ftp").pack(anchor="w", padx=30, pady=3)
        if hdd_online:
            ctk.CTkRadioButton(self, text=f"Write to the HDD{f' ({hdd_label})' if hdd_label else ''}"
                                          f" - you'll pick the folder next",
                               variable=self.dest_var, value="hdd").pack(anchor="w", padx=30, pady=3)
        if not ftp_online and not hdd_online:
            ctk.CTkLabel(self, text="No console or drive has answered yet, so there's nowhere to "
                                    "send these. Plug one in and the option appears here.",
                         text_color="gray", font=ctk.CTkFont(size=11), wraplength=510,
                         justify="left", anchor="w").pack(fill="x", padx=30, pady=(2, 0))

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
        self.result = {"mode": self.mode_var.get(), "destination": self.dest_var.get()}
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

DOWNLOAD_AHEAD_KEY = "download_ahead"    # settings.json: download the next game while this one converts/uploads
CATALOG_PAGE_SIZE = 1000       # themes / gamer picture packs listed per page on their tabs
DOWNLOAD_AHEAD_LIMIT = 5    # finished downloads allowed to wait for their turn before downloading pauses


class QueuedDownload:
    """One queued download.

    It only gets a row on the Downloads tab when it is near the front of the
    queue. A row is about 25 native windows and Windows allows a program 10,000,
    so a row per queued item made a very large queue freeze and then crash.
    """

    __slots__ = ("entry", "conv_mode", "upload_job", "cancel_event", "row", "ready",
                 "started", "done", "rename_to", "rename_ready", "rename_declined")

    def __init__(self, entry, conv_mode, upload_job):
        self.entry = entry
        self.conv_mode = conv_mode
        self.upload_job = upload_job
        self.cancel_event = threading.Event()
        self.row = None
        self.ready = threading.Event()
        self.started = False
        self.done = False
        self.rename_to = None                   # name for its folder on the HDD, when its own is too long
        self.rename_ready = threading.Event()   # cleared while the rename prompt is still unanswered
        self.rename_ready.set()
        self.rename_declined = False            # prompt closed without a name: download it, do not upload it


class CatalogTab:
    """One Themes / Gamer Pictures tab.

    Search, pages of results, Download (through the normal queue) and the
    Upload via FTP / Upload to HDD boxes. Uploads always go under
    Content/0000000000000000/FFFE07D1/ - the catalog decides the folder in it.
    The Gamer Pictures tab also shows the actual pictures, matched to
    xboxgamer.pics by the title ID in each pack's file name. The Themes tab
    shows each theme's wallpapers, matched by pack name to the folders in the
    xbox360-theme-wallpapers-zip archive item.
    """

    # preview kind: (pictures shown at once, thumbnail width, thumbnail height)
    PIC_LAYOUTS = {"gamerpics": (12, 64, 64), "wallpapers": (4, 160, 90)}

    def __init__(self, app, parent, catalog: Catalog, show_pictures: bool = False,
                 preview: str | None = None):
        self.app = app
        self.catalog = catalog
        if preview is None and show_pictures:
            preview = "gamerpics"
        self.preview = preview if preview in self.PIC_LAYOUTS else None
        self.show_pictures = self.preview is not None
        self.gamerpics = self.preview == "gamerpics"
        self.wallpapers = self.preview == "wallpapers"
        self.PIC_SLOTS, self.PIC_W, self.PIC_H = self.PIC_LAYOUTS.get(self.preview, (0, 0, 0))
        self.wallpaper_index: dict = {}  # pack key -> wallpaper URLs (Themes tab)
        self._walls_pending = False      # the wallpaper list is still loading
        self._setting_prefix = {"themes": "theme", "gamerpics": "gamerpic"}.get(catalog.key, catalog.key)

        self.entries: list = []         # every pack in the archive item
        self.matches: list = []         # the ones the current search matches
        self.page = 0
        self.query = ""
        self.selected: list = []
        self.loading = False
        self.loaded = False
        self.site_titles: dict = {}     # xboxgamer.pics titles by lower-case title ID
        self._pic_token = 0
        self._pic_ids: list = []
        self._pic_title = ""
        self._pic_page = 0

        noun, plural = catalog.noun, catalog.plural
        self.view = ctk.CTkFrame(parent, fg_color="transparent")
        self.view.grid_rowconfigure(2, weight=1)
        self.view.grid_columnconfigure(0, weight=1)

        search_row = ctk.CTkFrame(self.view, fg_color="transparent")
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        search_row.grid_columnconfigure(0, weight=1)

        hint = f"Search Xbox 360 {plural}" + (" (pack name or game)" if self.gamerpics else "")
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text=hint)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.search_entry.bind("<Return>", lambda e: self.perform_search())

        ctk.CTkButton(search_row, text="Search", width=100,
                      command=self.perform_search).grid(row=0, column=1)

        self.ftp_var = ctk.BooleanVar(value=False)
        self.ftp_check = ctk.CTkCheckBox(
            search_row, text="Upload via FTP", variable=self.ftp_var,
            checkbox_width=18, checkbox_height=18, command=self.on_ftp_toggle,
        )
        self.ftp_check.grid(row=0, column=2, padx=(12, 0))
        self.ftp_check.grid_remove()

        self.hdd_var = ctk.BooleanVar(value=False)
        self.hdd_check = ctk.CTkCheckBox(
            search_row, text="Upload to HDD", variable=self.hdd_var,
            checkbox_width=18, checkbox_height=18, command=self.on_hdd_toggle,
        )
        self.hdd_check.grid(row=0, column=3, padx=(12, 0))
        self.hdd_check.grid_remove()

        self.reload_btn = ctk.CTkButton(search_row, text="Reload List", width=100,
                                        command=lambda: self.load(force=True))
        self.reload_btn.grid(row=0, column=4, padx=(12, 0))

        details = ctk.CTkFrame(self.view)
        details.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        details.grid_columnconfigure(0, weight=1)

        self.title_lbl = ctk.CTkLabel(details, text=f"Select a {noun} to view details",
                                      font=ctk.CTkFont(size=16, weight="bold"), anchor="w")
        self.title_lbl.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))

        self.meta_lbl = ctk.CTkLabel(details, text="", font=ctk.CTkFont(size=12), anchor="w",
                                     justify="left")
        self.meta_lbl.grid(row=1, column=0, sticky="w", padx=12)

        self.dest_lbl = ctk.CTkLabel(details, text="", text_color="#00FF40", font=ctk.CTkFont(size=11),
                                     anchor="w", justify="left", wraplength=700)
        self.dest_lbl.grid(row=2, column=0, sticky="w", padx=12, pady=(2, 0))

        if self.show_pictures:
            self._build_picture_strip(details)

        actions = ctk.CTkFrame(details, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="w", padx=12, pady=(6, 10))
        self._download_text = f"Download {noun.title()}"
        self.download_btn = ctk.CTkButton(actions, text=self._download_text, state="disabled",
                                          command=self.start_download)
        self.download_btn.pack(side="left")
        self.add_collection_btn = ctk.CTkButton(
            actions, text="+ Add to Collection", state="disabled", width=160,
            fg_color="#7a4bbd", hover_color="#623a99", command=self.add_to_collection,
        )
        self.add_collection_btn._accent_locked = True
        self.add_collection_btn.pack(side="left", padx=(8, 0))
        self.status_lbl = ctk.CTkLabel(actions, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.status_lbl.pack(side="left", padx=(10, 0))

        self.list = VirtualResultsList(
            self.view, label_text=catalog.tab_label,
            on_selection_change=self._on_selection,
            on_activate=lambda entry: self.start_download(),
        )
        self.list.grid(row=2, column=0, sticky="nsew")

        pager = ctk.CTkFrame(self.view, fg_color="transparent")
        pager.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.first_btn = ctk.CTkButton(pager, text="<< First", width=70, height=26,
                                       command=lambda: self.goto_page(0))
        self.first_btn.pack(side="left")
        self.prev_btn = ctk.CTkButton(pager, text="< Prev", width=70, height=26,
                                      command=lambda: self.goto_page(self.page - 1))
        self.prev_btn.pack(side="left", padx=(6, 0))
        self.next_btn = ctk.CTkButton(pager, text="Next >", width=70, height=26,
                                      command=lambda: self.goto_page(self.page + 1))
        self.next_btn.pack(side="left", padx=(6, 0))
        self.last_btn = ctk.CTkButton(pager, text="Last >>", width=70, height=26,
                                      command=lambda: self.goto_page(10 ** 9))
        self.last_btn.pack(side="left", padx=(6, 0))
        self.page_lbl = ctk.CTkLabel(pager, text="", text_color="gray",
                                     font=ctk.CTkFont(size=11), anchor="w")
        self.page_lbl.pack(side="left", padx=(12, 0))

        self.update_dest_label()
        self._show_page()

    # -- loading -----------------------------------------------------------
    def open(self):
        if not self.loaded and not self.loading:
            self.load(force=False)
        try:
            self.search_entry.focus_set()
        except Exception:
            pass

    def load(self, force: bool = False):
        if self.loading:
            return
        self.loading = True
        try:
            self.reload_btn.configure(state="disabled")
        except Exception:
            pass
        self._set_status(f"Loading the {self.catalog.noun} list from archive.org...")
        if not self.entries:
            self.list.set_items([], empty_text=f"Loading {self.catalog.plural}...")
        cat, gamerpics, wallpapers = self.catalog, self.gamerpics, self.wallpapers

        def worker():
            entries, error, titles, walls = None, None, {}, {}
            try:
                entries = load_catalog_index(cat, force=force)
            except Exception as exc:
                error = exc
            if gamerpics:
                try:
                    titles = load_gamerpic_site_titles(force=force)
                except Exception as exc:
                    log_event(f"Gamer Pictures: xboxgamer.pics lookup failed - {exc}")
            if not wallpapers:
                post_ui(lambda: self._loaded(entries, error, titles))
                return
            # The theme list goes up straight away; the wallpaper list (four big
            # archive.org listings the first time) fills in the previews after.
            self._walls_pending = True
            post_ui(lambda: self._loaded(entries, error, titles))
            try:
                walls = load_theme_wallpapers(force=force)
            except Exception as exc:
                log_event(f"Themes: wallpaper lookup failed - {exc}")
            post_ui(lambda: self._walls_loaded(walls))

        threading.Thread(target=worker, daemon=True, name=f"{cat.key}-index").start()

    def _loaded(self, entries, error, titles):
        self.loading = self._walls_pending     # Reload List stays off until the wallpapers are in too
        try:
            self.reload_btn.configure(state="disabled" if self.loading else "normal")
        except Exception:
            pass
        if titles:
            self.site_titles = titles
        if error is not None:
            log_event(f"{self.catalog.tab_label}: {error}")
            self._set_status("Could not load the list - press Reload List to try again.", error=True)
            if not self.entries:
                self.list.set_items([], empty_text=f"{error}\n\nPress Reload List to try again.")
            return
        self.loaded = True
        self.entries = list(entries or [])
        status = f"{len(self.entries):,} {self.catalog.plural} available."
        if self.gamerpics:
            matched = sum(1 for e in self.entries if self._site_title(e))
            status += (f" {matched:,} matched to xboxgamer.pics." if self.site_titles
                       else " xboxgamer.pics could not be reached - no previews.")
        elif self.wallpapers:
            status += self._wallpaper_status()
        self._set_status(status)
        self._apply_search(keep_page=True)

    def _wallpaper_status(self) -> str:
        if self._walls_pending:
            return " Loading wallpaper previews..."
        if not self.wallpaper_index:
            return " The wallpaper list could not be loaded - no previews."
        matched = sum(1 for e in self.entries if self._wallpapers_for(e))
        return f" {matched:,} have wallpaper previews."

    def _walls_loaded(self, walls):
        """The wallpaper list is in: relabel the rows and fill the preview, keeping the selection."""
        self._walls_pending = False
        self.loading = False
        try:
            self.reload_btn.configure(state="normal")
        except Exception:
            pass
        if walls:
            self.wallpaper_index = walls
        if not self.loaded:
            return                                  # the theme list itself failed: its error stays up
        self._set_status(f"{len(self.entries):,} {self.catalog.plural} available." + self._wallpaper_status())
        try:
            self.list._labels = [self._label_for(entry) for entry in self.list.items]
            self.list.refresh()
        except Exception:
            self._show_page()
        if len(self.selected) == 1:
            self._show_pictures_for(self.selected[0])

    def _set_status(self, text: str, error: bool = False):
        try:
            self.status_lbl.configure(text=text, text_color="#d97070" if error else "gray")
        except Exception:
            pass

    # -- search and pages --------------------------------------------------
    def _site_title(self, entry):
        if not self.site_titles:
            return None
        return self.site_titles.get(catalog_title_id(entry.name).lower())

    def _wallpapers_for(self, entry) -> list:
        if not self.wallpaper_index or entry is None:
            return []
        return theme_wallpaper_urls(entry.name)

    def perform_search(self):
        self.query = self.search_entry.get().strip()
        self._apply_search(keep_page=False)

    def _apply_search(self, keep_page: bool = False):
        extra = None
        if self.gamerpics:
            def extra(entry):
                title = self._site_title(entry)
                return title.get("name", "") if title else ""
        self.matches = search_catalog(self.entries, self.query, extra_text=extra)
        if not keep_page:
            self.page = 0
        self._show_page()

    def _pages(self) -> int:
        return max(1, -(-len(self.matches) // CATALOG_PAGE_SIZE))

    def goto_page(self, page: int):
        page = max(0, min(int(page), self._pages() - 1))
        if page == self.page:
            return
        self.page = page
        self._show_page()

    def _label_for(self, entry) -> str:
        title_id = catalog_title_id(entry.name)
        parts = [format_size(entry.size)]
        if title_id:
            parts.append(title_id)
        if self.gamerpics and self.site_titles:
            title = self._site_title(entry)
            if title:
                count = title.get("count") or 0
                parts.append(f"{title.get('name') or '?'} - {count} pic{'s' if count != 1 else ''}")
            else:
                parts.append("no preview")
        elif self.wallpapers and self.wallpaper_index:
            count = len(self._wallpapers_for(entry))
            parts.append(f"{count} wallpaper{'s' if count != 1 else ''}" if count else "no preview")
        return f"{catalog_display_name(entry.name)}  ({', '.join(parts)})"

    def _show_page(self):
        total = len(self.matches)
        pages = self._pages()
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * CATALOG_PAGE_SIZE
        page_items = self.matches[start:start + CATALOG_PAGE_SIZE]
        labels = [self._label_for(entry) for entry in page_items]

        self.list._label_text = (f"{self.catalog.tab_label} matching '{self.query}'" if self.query
                                 else f"All {self.catalog.tab_label}")
        empty_text = ""
        if not page_items:
            if self.loading or not self.loaded:
                empty_text = f"Loading {self.catalog.plural}..." if self.loading else "Nothing loaded yet."
            elif self.query:
                empty_text = f"No {self.catalog.plural} match '{self.query}'."
            else:
                empty_text = f"No {self.catalog.plural} found."
        self.list.set_items(page_items, labels, empty_text=empty_text)
        self.list.scroll_to_top()

        if total:
            self.page_lbl.configure(
                text=f"{self.catalog.tab_label} {start + 1:,}-{start + len(page_items):,} of {total:,}"
                     f"  |  page {self.page + 1} of {pages}")
        else:
            self.page_lbl.configure(text="")
        back, forward = self.page > 0, self.page < pages - 1
        for button, enabled in ((self.first_btn, back), (self.prev_btn, back),
                                (self.next_btn, forward), (self.last_btn, forward)):
            button.configure(state="normal" if enabled else "disabled")

    def select_all(self):
        self.list.select_all()

    # -- selection ---------------------------------------------------------
    def _on_selection(self, entries: list, primary):
        self.selected = [e for e in entries if e is not None]
        count = len(self.selected)
        if not count:
            self.title_lbl.configure(text=f"Select a {self.catalog.noun} to view details")
            self.meta_lbl.configure(text="")
            self.download_btn.configure(text=self._download_text, state="disabled")
            self.add_collection_btn.configure(text="+ Add to Collection", state="disabled")
            self._show_pictures_for(None)
            return
        if count == 1:
            entry = self.selected[0]
            title_id = catalog_title_id(entry.name) or "unknown"
            meta = f"Game title ID: {title_id} | Size: {format_size(entry.size)} | File: {entry.name}"
            if catalog_name_hash(entry.name):
                meta = f"Game title ID: {title_id} | Size: {format_size(entry.size)} | " \
                       f"Pack ID: {catalog_name_hash(entry.name)[:12]}..."
            self.title_lbl.configure(text=catalog_display_name(entry.name))
            self.meta_lbl.configure(text=meta)
            self.download_btn.configure(text=self._download_text, state="normal")
            self.add_collection_btn.configure(text="+ Add to Collection", state="normal")
            self._show_pictures_for(entry)
            return
        known = [e.size for e in self.selected if e.size]
        size_text = format_size(sum(known)) if known else "unknown size"
        self.title_lbl.configure(text=f"{count:,} {self.catalog.plural} selected")
        self.meta_lbl.configure(text=f"Total size: {size_text}")
        self.download_btn.configure(text=f"Download {count:,} {self.catalog.tab_label}", state="normal")
        self.add_collection_btn.configure(text=f"+ Add {count:,} to Collection", state="normal")
        self._show_pictures_for(None)

    # -- picture preview (Gamer Pictures and Themes tabs) ------------------
    def _build_picture_strip(self, details):
        box = ctk.CTkFrame(details, fg_color="transparent")
        box.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 0))
        box.grid_columnconfigure(0, weight=1)

        # One row: the text on the left, then  <  >  and the site button on the right.
        # It is a grid, not pack: the buttons keep their size and only the text
        # column shrinks (and wraps) when the window is narrow or the text is long.
        nav = ctk.CTkFrame(box, fg_color="transparent")
        nav.grid(row=0, column=0, sticky="ew")
        nav.grid_columnconfigure(0, weight=1)
        self.pic_info_lbl = ctk.CTkLabel(nav, text="", text_color="gray", font=ctk.CTkFont(size=11),
                                         anchor="w", justify="left", wraplength=420)
        self.pic_info_lbl.grid(row=0, column=0, sticky="ew")
        self.pic_prev_btn = ctk.CTkButton(nav, text="<", width=30, height=24,
                                          command=lambda: self._pic_step(-1))
        self.pic_next_btn = ctk.CTkButton(nav, text=">", width=30, height=24,
                                          command=lambda: self._pic_step(1))
        site_text = "Open on archive.org" if self.wallpapers else "Open on xboxgamer.pics"
        self.pic_site_btn = ctk.CTkButton(nav, text=site_text, width=150, height=24,
                                          command=self._open_site_page)
        self._pic_nav = nav
        nav.bind("<Configure>", self._fit_pic_info, add="+")

        self.pic_strip = ctk.CTkFrame(box, fg_color="transparent", height=self.PIC_H + 4)
        self.pic_strip.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.pic_labels = []
        for _ in range(self.PIC_SLOTS):
            label = ctk.CTkLabel(self.pic_strip, text="", width=self.PIC_W, height=self.PIC_H,
                                 fg_color=("gray85", "gray25"), corner_radius=6)
            label._pic_url = None
            label.bind("<Button-1>", lambda e, l=label: self._open_picture(l))
            self.pic_labels.append(label)
        self._show_pictures_for(None)

    def _set_pic_nav(self, paged: bool, site: bool):
        for button in (self.pic_prev_btn, self.pic_next_btn, self.pic_site_btn):
            button.grid_forget()
        if paged:
            self.pic_prev_btn.grid(row=0, column=1, padx=(8, 0))
            self.pic_next_btn.grid(row=0, column=2, padx=(4, 0))
        if site:
            self.pic_site_btn.grid(row=0, column=3, padx=(8, 0))
        self._fit_pic_info()

    def _fit_pic_info(self, _event=None):
        """Wrap the text to whatever room the buttons leave, so it never pushes them."""
        try:
            nav = self._pic_nav
            if _event is None:
                nav.update_idletasks()      # buttons were just shown/hidden: measure them first
            width = nav.winfo_width()
            if width <= 1:
                return                      # tab not on screen yet
            buttons = sum(b.winfo_width() + 12 for b in (self.pic_prev_btn, self.pic_next_btn,
                                                          self.pic_site_btn) if b.winfo_ismapped())
            scale = self.pic_info_lbl._get_widget_scaling() or 1.0
            wrap = int(max(160, width - buttons - 8) / scale)
            if wrap != getattr(self, "_pic_wrap", None):
                self._pic_wrap = wrap
                self.pic_info_lbl.configure(wraplength=wrap)
        except Exception:
            pass

    def _clear_pic_slots(self):
        for label in self.pic_labels:
            label.configure(image="", text="")
            label.image = None
            label._pic_url = None
            label.pack_forget()

    def _show_pictures_for(self, entry):
        if not self.show_pictures:
            return
        self._pic_token += 1
        self._pic_ids, self._pic_page = [], 0
        self._clear_pic_slots()
        self._pic_title = catalog_title_id(entry.name).lower() if entry is not None else ""
        if self.wallpapers:
            self._show_wallpapers_for(entry)
            return
        if entry is None:
            self.pic_info_lbl.configure(text="Select one pack to see its pictures from xboxgamer.pics.")
            self._set_pic_nav(False, False)
            return
        title = self._site_title(entry)
        if not title:
            why = ("xboxgamer.pics could not be reached." if not self.site_titles
                   else f"xboxgamer.pics has no pictures for title ID {self._pic_title.upper() or '?'}.")
            self.pic_info_lbl.configure(text=f"No preview - {why}")
            self._set_pic_nav(False, False)
            return

        self.pic_info_lbl.configure(text=f"Loading pictures from xboxgamer.pics ({title.get('name')})...")
        self._set_pic_nav(False, True)
        token, tid = self._pic_token, self._pic_title

        def worker():
            ids = fetch_gamerpic_ids(tid)
            post_ui(lambda: self._pics_listed(token, ids, title))

        threading.Thread(target=worker, daemon=True, name="gamerpic-ids").start()

    def _show_wallpapers_for(self, entry):
        """Themes tab: the theme's wallpapers, straight out of the wallpaper zips on archive.org."""
        if entry is None:
            self.pic_info_lbl.configure(text="Select one theme to see its wallpapers.")
            self._set_pic_nav(False, False)
            return
        self._pic_site_name = catalog_display_name(entry.name)
        if self._walls_pending and not self.wallpaper_index:
            self.pic_info_lbl.configure(text="Loading the wallpaper list from archive.org...")
            self._set_pic_nav(False, False)
            return
        if not self.wallpaper_index:
            self.pic_info_lbl.configure(text="No preview - the wallpaper list could not be loaded "
                                             "(press Reload List to try again).")
            self._set_pic_nav(False, False)
            return
        urls = self._wallpapers_for(entry)
        if not urls:
            self.pic_info_lbl.configure(text="No preview - the wallpaper archive has no pictures for this theme.")
            self._set_pic_nav(False, True)
            return
        self._pic_ids = urls
        self._show_pic_page()

    def _pics_listed(self, token: int, ids: list, title: dict):
        if token != self._pic_token:
            return
        self._pic_ids = list(ids)
        self._pic_site_name = title.get("name") or ""
        if not ids:
            self.pic_info_lbl.configure(
                text=f"No preview - could not read the pictures for {self._pic_site_name} on xboxgamer.pics.")
            self._set_pic_nav(False, True)
            return
        self._show_pic_page()

    def _pic_step(self, step: int):
        pages = max(1, -(-len(self._pic_ids) // self.PIC_SLOTS))
        page = max(0, min(self._pic_page + step, pages - 1))
        if page != self._pic_page:
            self._pic_page = page
            self._show_pic_page()

    def _show_pic_page(self):
        self._pic_token += 1
        token, tid = self._pic_token, self._pic_title
        total = len(self._pic_ids)
        pages = max(1, -(-total // self.PIC_SLOTS))
        start = self._pic_page * self.PIC_SLOTS
        shown = self._pic_ids[start:start + self.PIC_SLOTS]

        self._clear_pic_slots()
        urls = []
        for label, pid in zip(self.pic_labels, shown):
            url = pid if self.wallpapers else gamerpic_image_url(tid, pid)
            label._pic_url = url
            label.configure(text="...")
            label.pack(side="left", padx=(0, 6))
            urls.append((label, url))

        span = f"{start + 1}-{start + len(shown)} of {total}" if pages > 1 else f"{total}"
        if self.wallpapers:
            self.pic_info_lbl.configure(
                text=f"{span} wallpaper{'s' if total != 1 else ''} from archive.org "
                     f"(click one to open it full size)")
        else:
            self.pic_info_lbl.configure(
                text=f"{self._pic_site_name} - pictures {span} from xboxgamer.pics (click one to open it)")
        self._set_pic_nav(pages > 1, True)
        self.pic_prev_btn.configure(state="normal" if self._pic_page > 0 else "disabled")
        self.pic_next_btn.configure(state="normal" if self._pic_page < pages - 1 else "disabled")

        fetch = fetch_theme_wallpaper if self.wallpapers else fetch_gamerpic_image

        def fetch_one(item):
            label, url = item
            if token != self._pic_token:
                return
            data = fetch(url)
            image = None
            if data:
                try:
                    image = Image.open(io.BytesIO(data)).convert("RGBA")
                    image.load()
                except Exception:
                    image = None
            post_ui(lambda: self._set_pic(token, label, url, image))

        def worker():
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(fetch_one, urls))

        threading.Thread(target=worker, daemon=True, name=f"{self.preview}-images").start()

    def _set_pic(self, token: int, label, url: str, image):
        if token != self._pic_token or label._pic_url != url:
            return
        try:
            if image is None:
                label.configure(image="", text="x")
                label.image = None
                return
            picture = ctk.CTkImage(light_image=image, dark_image=image, size=(self.PIC_W, self.PIC_H))
            label.configure(image=picture, text="")
            label.image = picture
        except Exception:
            pass

    def _open_picture(self, label):
        url = getattr(label, "_pic_url", None)
        if url:
            webbrowser.open(url)

    def _open_site_page(self):
        if self.wallpapers:
            webbrowser.open(theme_wallpaper_page_url())
            return
        webbrowser.open(gamerpic_site_url(self._pic_title))

    # -- uploads -----------------------------------------------------------
    def upload_kind(self) -> str | None:
        if self.app.ftp_online and self.ftp_var.get():
            return "ftp"
        if self.app.hdd_online and self.hdd_var.get():
            return "hdd"
        return None

    def destination(self, kind: str) -> tuple[str, str]:
        """(folder handed to the upload, where the packs really land) for FTP or HDD."""
        if kind == "ftp":
            base = self.app.ftp_config.default_path or "/"
            return base, catalog_upload_destination("ftp", base, self.app.ftp_config.default_path,
                                                    self.catalog.content_dir)
        base = self.app.hdd_config.default_path or "/"
        return base, catalog_upload_destination("hdd", base, "", self.catalog.content_dir)

    def update_dest_label(self):
        try:
            kind = self.upload_kind()
            if kind == "ftp":
                _base, dest = self.destination("ftp")
                text = f"Downloads, then uploads via FTP to {self.app.ftp_config.host}: {dest}"
            elif kind == "hdd":
                _base, dest = self.destination("hdd")
                text = f"Downloads, then writes to {self.app.hdd_config.label}: {dest}"
            else:
                text = (f"Auto uploads to "
                        f"{self.catalog.plural} straight to {self.catalog.console_path}")
            self.dest_lbl.configure(text=text)
        except Exception:
            pass

    def _save_toggle(self, which: str, on: bool):
        data = load_settings()
        other = "hdd" if which == "ftp" else "ftp"
        data[f"{self._setting_prefix}_{which}_upload_default"] = on
        if on:
            (self.hdd_var if which == "ftp" else self.ftp_var).set(False)
            data[f"{self._setting_prefix}_{other}_upload_default"] = False
        save_settings(data)
        self.update_dest_label()

    def on_ftp_toggle(self):
        self._save_toggle("ftp", bool(self.ftp_var.get()))

    def on_hdd_toggle(self):
        self._save_toggle("hdd", bool(self.hdd_var.get()))

    def sync_upload_checks(self):
        """Show the upload boxes only while FTP / the HDD is there, like the Library's."""
        try:
            saved = load_settings()
            prefix = self._setting_prefix
            if self.app.ftp_online:
                self.ftp_check.grid()
                if (not self.ftp_var.get() and not self.hdd_var.get()
                        and saved.get(f"{prefix}_ftp_upload_default")):
                    self.ftp_var.set(True)
            else:
                self.ftp_var.set(False)
                self.ftp_check.grid_remove()
            if self.app.hdd_online:
                self.hdd_check.grid()
                if (not self.hdd_var.get() and not self.ftp_var.get()
                        and saved.get(f"{prefix}_hdd_upload_default")):
                    self.hdd_var.set(True)
            else:
                self.hdd_var.set(False)
                self.hdd_check.grid_remove()
        except Exception:
            pass
        self.update_dest_label()

    def add_to_collection(self):
        entries = [e for e in self.selected if e is not None]
        if not entries:
            primary = self.list.primary_item()
            entries = [primary] if primary is not None else []

        def hint(text: str) -> None:
            self._set_status(text)

            def clear():
                try:
                    if self.status_lbl.cget("text") == text:     # not replaced by something newer
                        self._set_status("")
                except Exception:
                    pass
            self.app.after(6000, clear)

        self.app.add_entries_to_collection(entries, hint)

    def start_download(self):
        app = self.app
        if not app.require_api_keys():
            return
        entries = [e for e in self.selected if e is not None]
        if not entries:
            primary = self.list.primary_item()
            if primary is None:
                return
            entries = [primary]

        upload_job = None
        kind = self.upload_kind()
        if kind:
            base, _dest = self.destination(kind)
            upload_job = (kind, base)

        queued = app._queue_entries(entries, "3", upload_job)
        if not queued:
            return
        if upload_job:
            _base, dest = self.destination(upload_job[0])
            how = "uploaded via FTP" if upload_job[0] == "ftp" else "written to the HDD"
            log_event(f"Queued {queued} {self.catalog.noun}(s), then {how} into {dest}.")
        else:
            log_event(f"Queued {queued} {self.catalog.noun}(s) for download.")
        app.select_tab("downloads")


class ArchiveGUI(ctk.CTk):
    def report_callback_exception(self, exc_type, exc, tb):
        # Errors in button clicks / timers: log them (Logs tab + log file) instead of losing them.
        import traceback
        log_event("UNEXPECTED ERROR in the window:\n"
                  + "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())

    def __init__(self, index: Index):
        super().__init__()
        self.title("Bman Downloader")
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
        self._hdd_rename_backlog: list = []     # queued jobs still to be asked for a shorter HDD name
        self._hdd_rename_dialog = None
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
        # Downloading and the work after it (extract / convert / upload) are two
        # stages. Each handles one game at a time; the "download ahead" setting
        # decides whether the next download may start while the stage after it
        # is still busy with an earlier game.
        self.download_ahead = bool(load_settings().get(DOWNLOAD_AHEAD_KEY, False))
        self._dl_thread_running = False
        self._post_queue = queue.Queue()
        self._post_cond = threading.Condition()
        self._post_pending = 0          # downloaded, extract/convert/upload not finished yet
        self._post_thread = None
        self._api_locked = False
        self._api_dialog_open = False
        self._setting_save_jobs = {}

        self.ftp_config = load_ftp_config()
        self.ftp_online = False
        self._ftp_checking = False

        self.hdd_config = load_hdd_config()
        self.hdd_online = False
        self._hdd_checking = False

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(11, weight=1)

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="Bman Downloader", font=ctk.CTkFont(size=18, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))

        self.sidebar_btn_library = ctk.CTkButton(self.sidebar_frame, text="Library / Search", command=lambda: self.select_tab("library"))
        self.sidebar_btn_library.grid(row=1, column=0, padx=20, pady=10)

        self.sidebar_btn_themes = ctk.CTkButton(self.sidebar_frame, text="Themes", command=lambda: self.select_tab("themes"))
        self.sidebar_btn_themes.grid(row=2, column=0, padx=20, pady=10)

        self.sidebar_btn_gamerpics = ctk.CTkButton(self.sidebar_frame, text="Gamer Pictures", command=lambda: self.select_tab("gamerpics"))
        self.sidebar_btn_gamerpics.grid(row=3, column=0, padx=20, pady=10)

        self.sidebar_btn_downloads = ctk.CTkButton(self.sidebar_frame, text="Downloads", command=lambda: self.select_tab("downloads"))
        self.sidebar_btn_downloads.grid(row=4, column=0, padx=20, pady=10)

        self.sidebar_btn_collections = ctk.CTkButton(self.sidebar_frame, text="Collections", command=lambda: self.select_tab("collections"))
        self.sidebar_btn_collections.grid(row=5, column=0, padx=20, pady=10)

        self.sidebar_btn_community = ctk.CTkButton(self.sidebar_frame, text="Community Collections",
                                                   command=lambda: self.select_tab("community"))
        self.sidebar_btn_community.grid(row=6, column=0, padx=20, pady=10)

        self.sidebar_btn_logs = ctk.CTkButton(self.sidebar_frame, text="Logs", command=lambda: self.select_tab("logs"))
        self.sidebar_btn_logs.grid(row=7, column=0, padx=20, pady=10)

        self.sidebar_btn_settings = ctk.CTkButton(self.sidebar_frame, text="Settings", command=lambda: self.select_tab("settings"))
        self.sidebar_btn_settings.grid(row=8, column=0, padx=20, pady=10)

        self._setup_app_switch_button(row=9, target="retro", text="Retro Downloader")

        self._bulk_running = False
        self._stop_for_switch = False     # set when the user switches apps mid-work
        self.bulk_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.bulk_frame.grid(row=10, column=0, padx=20, pady=(10, 0), sticky="ew")

        ctk.CTkLabel(self.bulk_frame, text="Bulk Actions", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="gray").pack(anchor="w", pady=(0, 4))

        self.bulk_god_btn = ctk.CTkButton(self.bulk_frame, text="GOD All", fg_color="#2b8a3e",
                                          hover_color="#236c31", command=lambda: self.start_bulk_process("2"))
        self.bulk_god_btn.pack(fill="x", pady=(0, 6))

        self.bulk_xex_btn = ctk.CTkButton(self.bulk_frame, text="XEX All", fg_color=accent_fg(),
                                          hover_color=accent_hover(), command=lambda: self.start_bulk_process("1"))
        self.bulk_xex_btn.pack(fill="x")

        self.bulk_ftp_btn = ctk.CTkButton(self.bulk_frame, text="Upload All via FTP",
                                          fg_color="#7a4bbd", hover_color="#623a99",
                                          command=self.start_bulk_ftp_upload)
        self.bulk_ftp_btn._accent_locked = True

        self.bulk_hdd_btn = ctk.CTkButton(self.bulk_frame, text="Upload All to HDD",
                                          fg_color="#0f766e", hover_color="#0c5d56",
                                          command=self.start_bulk_hdd_upload)
        self.bulk_hdd_btn._accent_locked = True

        self.bulk_status_lbl = ctk.CTkLabel(self.bulk_frame, text="", text_color="gray",
                                            font=ctk.CTkFont(size=11), wraplength=160,
                                            justify="left", anchor="w")
        self.bulk_status_lbl.pack(fill="x", pady=(6, 0))

        self.bulk_frame.grid_remove()

        self.update_banner_btn = ctk.CTkButton(
            self.sidebar_frame, text="Update Available!", fg_color="#2b8a3e",
            hover_color="#236c31", command=self.open_update_settings,
        )
        self.update_banner_btn.grid(row=12, column=0, padx=20, pady=(10, 20))
        self.update_banner_btn.grid_remove()

        self.content_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.content_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.content_frame.grid_rowconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(0, weight=1)

        self.current_tab = "library"

        self.setup_library_view()
        self.catalog_tabs = {
            cat.key: CatalogTab(self, self.content_frame, cat,
                                preview=("gamerpics" if cat is GAMERPIC_CATALOG
                                         else "wallpapers" if cat is THEME_CATALOG else None))
            for cat in CATALOGS
        }
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
        self.after(900, lambda: self._start_ftp_check(manual=False))
        self.after(1200, lambda: self._start_hdd_check(manual=False))
        self.after(HDD_POLL_MS, self._poll_hdd)

        APP_WINDOW["ref"] = self
        self.after(2200, self._check_archive_support_at_startup)

        self.after(250, self._enforce_api_keys)
        self.after(3000, self._prepare_lists)
        self.after(800, cover_warm_up)       # first cover shows sooner
        self._index_refreshing = False
        self._index_retry_after = None
        self._show_index_status()
        self.after(700, self._refresh_index_if_needed)
        threading.Thread(target=cleanup_update_leftovers, daemon=True,
                         name="update-cleanup").start()


    # ----- switching between Bman Downloader and the Retro Downloader -----------
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

    # ----- game list (archive.org) health -------------------------------------
    INDEX_RETRY_OFFLINE_MS = 2 * 60 * 1000      # nothing loaded: try again every 2 minutes
    INDEX_RETRY_PARTIAL_MS = 10 * 60 * 1000     # some sources failed: try again every 10 minutes

    def _show_index_status(self) -> None:
        index = self.index
        label = getattr(self, "index_status_lbl", None)
        if label is None:
            return
        text = ""
        if index.status == "offline" and self._index_refreshing:
            text = "Loading the game list from archive.org..."
        elif index.status == "stale" and not index.failed_sources:
            # The saved list is just older than a day and is being refreshed quietly
            # in the background. Nothing has gone wrong, so say nothing; a warning
            # appears only if that refresh fails (failed_sources is filled in then).
            text = ""
        elif index.status == "offline":
            text = ("archive.org isn't responding, so the game list couldn't be loaded. "
                    "Retrying automatically every 2 minutes.")
        elif index.status == "stale":
            when = ""
            if index.fetched_at:
                when = time.strftime(" from %b %d, %Y", time.localtime(index.fetched_at))
            text = (f"archive.org isn't responding - showing the saved game list{when}. "
                    "Downloads may fail until it's back. Retrying automatically.")
        elif index.status == "partial":
            count = len(index.failed_sources)
            text = (f"{count} archive.org source{'s' if count != 1 else ''} didn't load - "
                    "their games come from the saved list. Retrying automatically.")
        try:
            if text:
                label.configure(text=text)
                label.grid()
            else:
                label.configure(text="")
                label.grid_remove()
        except Exception:
            pass

    def _refresh_index_if_needed(self) -> None:
        self._index_retry_after = None
        if self._index_refreshing or not self.index.needs_refresh:
            self._show_index_status()
            return
        self._index_refreshing = True
        self._show_index_status()

        def work():
            try:
                changed = self.index.refresh()
            except Exception as exc:
                log_event(f"Game list refresh failed: {exc}")
                changed = False
            post_ui(lambda: self._on_index_refreshed(changed))

        threading.Thread(target=work, daemon=True, name="index-refresh").start()

    def _on_index_refreshed(self, changed: bool) -> None:
        self._index_refreshing = False
        self._show_index_status()
        status = self.index.status
        if changed:
            if status == "fresh":
                log_event(f"Game list updated from archive.org ({len(self.index.entries):,} items).")
            # Re-run a search the user made while the list was missing or out of date.
            try:
                if self.search_entry.get().strip() and self.current_tab == "library":
                    self.perform_search()
            except Exception:
                pass
        if status in ("offline", "stale"):
            delay = self.INDEX_RETRY_OFFLINE_MS
        elif status == "partial":
            delay = self.INDEX_RETRY_PARTIAL_MS
        else:
            return
        if self._index_retry_after is None:
            self._index_retry_after = self.after(delay, self._refresh_index_if_needed)

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
                "library", "themes", "gamerpics", "downloads", "collections", "community"):
            self.show_api_keys_dialog()
            return

        self.current_tab = name

        catalog_views = [tab.view for tab in self.catalog_tabs.values()]
        for frame in (self.library_view, *catalog_views, self.downloads_view, self.collections_view,
                      self.community_view, self.logs_view, self.settings_view):
            frame.grid_forget()

        if name == "downloads":
            self.bulk_frame.grid()
        else:
            self.bulk_frame.grid_remove()

        if name == "library":
            self.library_view.grid(row=0, column=0, sticky="nsew")
        elif name in self.catalog_tabs:
            self.catalog_tabs[name].view.grid(row=0, column=0, sticky="nsew")
            self.catalog_tabs[name].open()
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
        tab = getattr(self, "current_tab", "")
        if tab != "library" and tab not in self.catalog_tabs:
            return None
        widget = getattr(event, "widget", None)
        if isinstance(widget, (tk.Entry, tk.Text)):
            return None
        try:
            (self.catalog_tabs[tab].list if tab in self.catalog_tabs else self.results_list).select_all()
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

        self.search_entry = ctk.CTkEntry(search_row, placeholder_text="Search for any Xbox 360/OG Xbox game or dlc")
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.search_entry.bind("<Return>", lambda e: self.perform_search())

        search_btn = ctk.CTkButton(search_row, text="Search", width=100, command=self.perform_search)
        search_btn.grid(row=0, column=1)

        saved = load_settings()

        saved_region = saved.get(REGION_SETTING_KEY, REGION_FILTER_DEFAULT)
        if saved_region not in REGION_FILTER_LABELS:
            saved_region = REGION_FILTER_DEFAULT
        self.region_var = ctk.StringVar(value=saved_region)

        region_lbl = ctk.CTkLabel(search_row, text="Region:", font=ctk.CTkFont(size=12))
        region_lbl.grid(row=0, column=2, padx=(12, 6))

        self.region_menu = ctk.CTkOptionMenu(
            search_row, values=REGION_FILTER_LABELS, variable=self.region_var,
            width=175, command=self.on_region_change,
        )
        self.region_menu.grid(row=0, column=3)

        self.show_dlc_var = ctk.BooleanVar(value=bool(saved.get("show_dlc", True)))
        self.show_dlc_check = ctk.CTkCheckBox(
            search_row, text="Show DLC", variable=self.show_dlc_var,
            checkbox_width=18, checkbox_height=18, command=self.on_show_dlc_toggle,
        )
        self.show_dlc_check.grid(row=0, column=4, padx=(12, 0))

        # Shown only when archive.org is having trouble (see _show_index_status).
        self.index_status_lbl = ctk.CTkLabel(
            search_row, text="", anchor="w", justify="left",
            text_color=("#B35900", "#FFB347"), font=ctk.CTkFont(size=12),
        )
        self.index_status_lbl.grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))
        self.index_status_lbl.grid_remove()

        self.ftp_upload_var = ctk.BooleanVar(value=False)
        self.ftp_upload_check = ctk.CTkCheckBox(
            search_row, text="Upload via FTP", variable=self.ftp_upload_var,
            checkbox_width=18, checkbox_height=18, command=self.on_ftp_upload_toggle,
        )
        self.ftp_upload_check.grid(row=0, column=5, padx=(12, 0))
        self.ftp_upload_check.grid_remove()

        self.hdd_upload_var = ctk.BooleanVar(value=False)
        self.hdd_upload_check = ctk.CTkCheckBox(
            search_row, text="Upload to HDD", variable=self.hdd_upload_var,
            checkbox_width=18, checkbox_height=18, command=self.on_hdd_upload_toggle,
        )
        self.hdd_upload_check.grid(row=0, column=6, padx=(12, 0))
        self.hdd_upload_check.grid_remove()

        self.details_frame = ctk.CTkFrame(self.library_view, height=140)
        self.details_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.details_frame.grid_columnconfigure(1, weight=1)

        self.cover_label = ctk.CTkLabel(self.details_frame, text="No Cover",
                                        width=self.COVER_MAX_W, height=self.COVER_MAX_H,
                                        fg_color=self.COVER_EMPTY_COLOR)
        self.cover_label.grid(row=0, column=0, rowspan=4, padx=10, pady=10)

        self.game_title_lbl = ctk.CTkLabel(self.details_frame, text="Select an item to view details", font=ctk.CTkFont(size=16, weight="bold"))
        self.game_title_lbl.grid(row=0, column=1, sticky="w", padx=10, pady=(10, 0))

        # Source / size line, then (for Original Xbox games) the xefu versions that work.
        meta_box = ctk.CTkFrame(self.details_frame, fg_color="transparent")
        meta_box.grid(row=1, column=1, sticky="w", padx=10, pady=(0, 10))
        self.game_meta_lbl = ctk.CTkLabel(meta_box, text="", font=ctk.CTkFont(size=12),
                                          anchor="w", justify="left")
        self.game_meta_lbl.pack(anchor="w")
        # Same look as the "ISO conversion disabled..." note: plain text, bright colour.
        self.xefu_lbl = ctk.CTkLabel(meta_box, text="", anchor="w", justify="left", wraplength=640)
        self._xefu_lbl_shown = False

        # height=1: an empty CTkFrame is 200px tall by default, which stretched this
        # panel when nothing was selected. The row keeps one line of space either way.
        self.action_sub = ctk.CTkFrame(self.details_frame, fg_color="transparent", width=1, height=1)
        self.action_sub.grid(row=2, column=1, sticky="w", padx=10)
        try:
            row_scale = ctk.ScalingTracker.get_widget_scaling(self.details_frame)
        except Exception:
            row_scale = 1.0
        self.details_frame.grid_rowconfigure(2, minsize=round(28 * row_scale))   # grid minsize is raw pixels

        self.conversion_var = ctk.StringVar(value="2")
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

    def _reset_results_scroll(self):
        try:
            self.results_list.scroll_to_top()
        except Exception:
            pass

    def perform_search(self):
        query = self.search_entry.get().strip()
        if not query:
            return

        matches = self.index.search(query)

        region_label = self.region_var.get()
        wanted_regions = region_filter_for_label(region_label)
        hidden_region = 0
        if wanted_regions:
            filtered = [e for e in matches if entry_matches_region(e, wanted_regions)]
            hidden_region = len(matches) - len(filtered)
            matches = filtered

        hidden_dlc = 0
        if not self.show_dlc_var.get():
            filtered = [e for e in matches if not is_dlc_entry(e)]
            hidden_dlc = len(matches) - len(filtered)
            matches = filtered

        notes = []
        if hidden_region:
            notes.append(f"{hidden_region} result{'s' if hidden_region != 1 else ''} "
                         f"hidden by the '{region_label}' filter")
        if hidden_dlc:
            notes.append(f"{hidden_dlc} DLC result{'s' if hidden_dlc != 1 else ''} hidden")

        self.current_results = matches

        empty_text = ""
        if not self.current_results and not self.index.entries:
            empty_text = ("The game list couldn't be loaded because archive.org isn't responding. "
                          "It will retry automatically - your search will run again once it loads.")
            notes = []
        elif not self.current_results:
            empty_text = "No matching items found."
            if notes:
                empty_text += "  (" + ", ".join(notes) + ".)"
                if hidden_region:
                    empty_text += f"  Set Region to '{REGION_FILTER_ALL}' to see everything."
            notes = []

        labels = [
            f"[{entry.archive_identifier}] {entry.name}  "
            f"({format_size(entry.size)}, {entry.date or 'unknown date'})"
            for entry in self.current_results
        ]

        self.results_list.set_items(
            self.current_results, labels,
            note=(", ".join(notes) + "." if notes else ""),
            empty_text=empty_text,
        )
        self._prefetch_covers(self.current_results[:COVER_PREFETCH_COUNT])

    def _prefetch_covers(self, entries, replace: bool = True) -> None:
        """Fetch covers in the background so clicking shows them at once."""
        try:
            prefetch_covers(
                [(e.name, is_og_xbox_entry(e), is_dlc_entry(e)) for e in entries],
                replace=replace,
            )
        except Exception as exc:
            log_event(f"Cover prefetch failed: {exc}")

    def on_show_dlc_toggle(self):
        data = load_settings()
        data["show_dlc"] = bool(self.show_dlc_var.get())
        save_settings(data)
        if self.search_entry.get().strip():
            self.perform_search()

    def on_region_change(self, _choice=None):
        data = load_settings()
        data[REGION_SETTING_KEY] = self.region_var.get()
        save_settings(data)
        if self.search_entry.get().strip():
            self.perform_search()

    def on_ftp_upload_toggle(self):
        data = load_settings()
        on = bool(self.ftp_upload_var.get())
        data["ftp_upload_default"] = on
        if on:
            try:
                self.hdd_upload_var.set(False)
            except Exception:
                pass
            data["hdd_upload_default"] = False
        save_settings(data)

    def on_hdd_upload_toggle(self):
        data = load_settings()
        on = bool(self.hdd_upload_var.get())
        data["hdd_upload_default"] = on
        if on:
            try:
                self.ftp_upload_var.set(False)
            except Exception:
                pass
            data["ftp_upload_default"] = False
        save_settings(data)

    def _start_ftp_check(self, manual: bool = False):
        if self._ftp_checking:
            return
        self.ftp_config = load_ftp_config()

        if not self.ftp_config.is_configured():
            self._set_ftp_online(False)
            if manual:
                self._set_ftp_status("Enter the console's IP address first, then Save.", error=True)
            return

        self._ftp_checking = True
        if manual:
            self._set_ftp_status(f"Connecting to {self.ftp_config.host}:{self.ftp_config.port} in {ftp_mode_name(self.ftp_config)}...")
            try:
                self.ftp_test_btn.configure(state="disabled")
            except Exception:
                pass

        cfg = self.ftp_config

        def worker():
            ok, message = ftp_test_connection(cfg)
            post_ui(lambda: self._on_ftp_checked(ok, message, manual))

        threading.Thread(target=worker, daemon=True).start()

    def _on_ftp_checked(self, ok: bool, message: str, manual: bool):
        self._ftp_checking = False
        try:
            self.ftp_test_btn.configure(state="normal")
        except Exception:
            pass

        was_online = self.ftp_online
        self._set_ftp_online(ok)

        if manual or ok != was_online:
            self._set_ftp_status(message, error=not ok)
        if ok and not was_online:
            log_event(f"FTP: {self.ftp_config.host} reachable - upload controls enabled.")

    def _set_ftp_online(self, online: bool):
        self.ftp_online = bool(online)
        FTP_STATE["online"] = bool(online)

        try:
            if online:
                self.ftp_upload_check.grid()
                hdd_armed = bool(getattr(self, "hdd_upload_var", None)
                                 and self.hdd_upload_var.get())
                if (not self.ftp_upload_var.get()
                        and load_settings().get("ftp_upload_default")
                        and not hdd_armed):
                    self.ftp_upload_var.set(True)
            else:
                self.ftp_upload_var.set(False)
                self.ftp_upload_check.grid_remove()
        except Exception:
            pass

        for catalog_tab in getattr(self, "catalog_tabs", {}).values():
            catalog_tab.sync_upload_checks()
        self._refresh_bulk_buttons()

        try:
            for widget in self.downloads_scroll.winfo_children():
                if isinstance(widget, DownloadItemFrame):
                    widget.check_iso_and_files()
        except Exception:
            pass

    def _refresh_bulk_buttons(self):
        state = (bool(self.ftp_online), bool(self.hdd_online))
        if getattr(self, "_bulk_button_state", None) == state:
            return
        self._bulk_button_state = state

        for button in (self.bulk_ftp_btn, self.bulk_hdd_btn):
            try:
                button.pack_forget()
            except Exception:
                pass

        for button, wanted in ((self.bulk_ftp_btn, self.ftp_online),
                               (self.bulk_hdd_btn, self.hdd_online)):
            if not wanted:
                continue
            try:
                button.pack(fill="x", pady=(6, 0), before=self.bulk_status_lbl)
            except Exception:
                pass

    def _set_ftp_status(self, text: str, error: bool = False):
        try:
            self.ftp_status_lbl.configure(text=text, text_color="#d97070" if error else "green")
        except Exception:
            pass

    def require_ftp_destination(self, subtitle: str | None = None) -> str | None:
        if not self.ftp_online:
            return None
        return prompt_ftp_destination(self, self.ftp_config, subtitle=subtitle)


    def _poll_hdd(self):
        try:
            if self.hdd_config.is_configured() and not self._bulk_running and not self.is_downloading:
                self._start_hdd_check(manual=False)
        except Exception:
            pass
        finally:
            self.after(HDD_POLL_MS, self._poll_hdd)

    def _start_hdd_check(self, manual: bool = False):
        if self._hdd_checking:
            return
        self.hdd_config = load_hdd_config()

        if not self.hdd_config.is_configured():
            self._set_hdd_online(False)
            if manual:
                self._set_hdd_status("Pick a drive and a partition above, then Save.", error=True)
            return

        self._hdd_checking = True
        if manual:
            self._set_hdd_status(f"Reading {self.hdd_config.label}...")
            try:
                self.hdd_test_btn.configure(state="disabled")
            except Exception:
                pass

        cfg = self.hdd_config

        def worker():
            ok, message = hdd_test_connection(cfg)
            post_ui(lambda: self._on_hdd_checked(ok, message, manual))

        threading.Thread(target=worker, daemon=True).start()

    def _on_hdd_checked(self, ok: bool, message: str, manual: bool):
        self._hdd_checking = False
        try:
            self.hdd_test_btn.configure(state="normal")
        except Exception:
            pass

        was_online = self.hdd_online
        self._set_hdd_online(ok)

        if manual or ok != was_online:
            self._set_hdd_status(message, error=not ok)
        if ok and not was_online:
            log_event(f"HDD: {self.hdd_config.label} mounted - upload controls enabled.")

    def _set_hdd_online(self, online: bool):
        online = bool(online)
        changed = (self.hdd_online != online)
        self.hdd_online = online
        HDD_STATE["online"] = online

        if not changed:
            return

        try:
            if online:
                self.hdd_upload_check.grid()
                ftp_armed = bool(getattr(self, "ftp_upload_var", None)
                                 and self.ftp_upload_var.get())
                if (not self.hdd_upload_var.get()
                        and load_settings().get("hdd_upload_default")
                        and not ftp_armed):
                    self.hdd_upload_var.set(True)
            else:
                self.hdd_upload_var.set(False)
                self.hdd_upload_check.grid_remove()
        except Exception:
            pass

        for catalog_tab in getattr(self, "catalog_tabs", {}).values():
            catalog_tab.sync_upload_checks()
        self._refresh_bulk_buttons()

        try:
            for widget in self.downloads_scroll.winfo_children():
                if isinstance(widget, DownloadItemFrame):
                    widget.check_iso_and_files()
        except Exception:
            pass

    def _set_hdd_status(self, text: str, error: bool = False):
        try:
            self.hdd_status_lbl.configure(text=text, text_color="#d97070" if error else "green")
        except Exception:
            pass

    def require_hdd_destination(self, subtitle: str | None = None) -> str | None:
        if not self.hdd_online:
            return None
        return prompt_hdd_destination(self, self.hdd_config, subtitle=subtitle)

    def _on_results_selection(self, entries: list, primary):
        self.selected_files = list(entries)
        if not entries:
            self.selected_file = None
            self._show_no_selection()
        elif len(entries) == 1:
            self.select_game_item(entries[0])
            # Moving down the list with the keyboard: have the next few ready too.
            try:
                at = self.current_results.index(entries[0])
                self._prefetch_covers(self.current_results[at + 1:at + 6], replace=False)
            except ValueError:
                pass
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

    # Cover art area. Xbox 360 box art is 219x300, so 120x165 shows it with no bars.
    COVER_MAX_W = 120
    COVER_MAX_H = 165
    COVER_EMPTY_COLOR = ("gray85", "gray25")

    def _clear_cover(self, text: str = "No Cover"):
        if getattr(self.cover_label, "image", None) is None and self.cover_label.cget("text") == text:
            return
        self.cover_label.configure(image="", text=text, width=self.COVER_MAX_W,
                                   height=self.COVER_MAX_H, fg_color=self.COVER_EMPTY_COLOR)
        self.cover_label.image = None

    def _set_cover_image(self, data: bytes) -> None:
        """Show cover art at its own shape, as large as fits the cover area.
        The label shrinks to the picture, so there are no gray bars around it."""
        try:
            picture = Image.open(io.BytesIO(data))
            picture.load()
            picture = picture.convert("RGB")
            w, h = picture.size
            scale = min(self.COVER_MAX_W / w, self.COVER_MAX_H / h)
            size = (max(1, round(w * scale)), max(1, round(h * scale)))
            # CTkImage follows Windows display scaling, same as the label size.
            ctk_img = ctk.CTkImage(light_image=picture, dark_image=picture, size=size)
        except Exception:
            self._clear_cover()
            return
        self.cover_label.configure(image=ctk_img, text="", width=size[0], height=size[1],
                                   fg_color="transparent")
        self.cover_label.image = ctk_img

    # Text colour of the XEFU line (green like the "ISO conversion disabled..." note).
    _XEFU_LINE_STYLES = {
        "works": "#00FF40",
        "broken": "#FFA500",
        "unknown": "gray",
    }

    def _set_xefu_line(self, text, kind: str = "unknown"):
        """Shows the xefu line under the source/size line, or hides it (text=None)."""
        if not text:
            if self._xefu_lbl_shown:
                self.xefu_lbl.pack_forget()
                self._xefu_lbl_shown = False
            return
        text_color = self._XEFU_LINE_STYLES.get(kind, self._XEFU_LINE_STYLES["unknown"])
        self.xefu_lbl.configure(text=text, text_color=text_color)
        if not self._xefu_lbl_shown:
            self.xefu_lbl.pack(anchor="w", pady=(2, 0))
            self._xefu_lbl_shown = True

    def _show_no_selection(self):
        self._cancel_pending_cover()
        self._clear_conversion_widgets()
        self.game_title_lbl.configure(text="Select an item to view details")
        self.game_meta_lbl.configure(text="")
        self._set_xefu_line(None)
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

        sources = sorted({e.archive_identifier for e in entries if e.archive_identifier})
        source_text = sources[0] if len(sources) == 1 else f"{len(sources)} sources"
        dlc_count = sum(1 for e in entries if is_dlc_entry(e))

        self.game_title_lbl.configure(text=f"{count:,} items selected")
        meta = f"Source: {source_text} | Total size: {size_text}"
        if dlc_count:
            meta += f" | {dlc_count} DLC"
        self.game_meta_lbl.configure(text=meta)
        self._set_xefu_line(None)

        note = ctk.CTkLabel(
            self.action_sub,
            text="Format and destination are asked once, for the whole selection.",
            text_color="#00FF40",
        )
        note.pack(side="left", padx=5)
        self.conversion_widgets.append(note)

        self.download_btn.configure(text=f"Download {count:,} Items", state="normal")
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
        self.game_title_lbl.configure(text=entry.name)
        self.game_meta_lbl.configure(
            text=f"Source: {entry.archive_identifier} | Size: {format_size(entry.size)} | Date: {entry.date or 'N/A'}")
        if is_og_xbox_entry(entry) and not is_dlc_entry(entry):
            try:
                xefu, listed = xefu_lookup(entry.name)
            except Exception:
                xefu, listed = None, False
            if xefu is not None:
                self._set_xefu_line(f"XEFU: works on {xefu_versions_text(xefu)}", "works")
            elif listed:
                self._set_xefu_line("XEFU: no fully working version (In-game or worse on every xefu)", "broken")
            else:
                self._set_xefu_line("XEFU: not on the ConsoleMods compatibility list", "unknown")
        else:
            self._set_xefu_line(None)
        self.download_btn.configure(state="normal")
        try:
            self.add_collection_btn.configure(state="normal")
            self.collection_hint_lbl.configure(text="")
        except Exception:
            pass

        for widget in self.conversion_widgets:
            widget.destroy()
        self.conversion_widgets.clear()

        if entry.allow_iso_conversion:
            self.conversion_var.set("2") 
            lbl = ctk.CTkLabel(self.action_sub, text="Post-Process:")
            lbl.pack(side="left", padx=(0, 5))
            self.conversion_widgets.append(lbl)

            rb1 = ctk.CTkRadioButton(self.action_sub, text="GOD", variable=self.conversion_var, value="2", width=0)
            rb1.pack(side="left", padx=(5, 12))
            self.conversion_widgets.append(rb1)

            rb2 = ctk.CTkRadioButton(self.action_sub, text="Extract ISO", variable=self.conversion_var, value="1", width=0)
            rb2.pack(side="left", padx=(0, 12))
            self.conversion_widgets.append(rb2)

            rb3 = ctk.CTkRadioButton(self.action_sub, text="Just Download", variable=self.conversion_var, value="3", width=0)
            rb3.pack(side="left", padx=(0, 5))
            self.conversion_widgets.append(rb3)
        else:
            self.conversion_var.set("3")
            if entry_is_content(entry):
                info_text = "Title ID content - only the title ID folder (e.g. 584E07D2) is uploaded."
                info_color = "#00FF40"
            elif is_og_xbox_entry(entry):
                info_text = ""            # OG Xbox games show their XEFU line instead
                info_color = "#00FF40"
            else:
                info_text = "ISO conversion disabled for this source (Just Download)."
                info_color = "#00FF40"
            if info_text:
                info_lbl = ctk.CTkLabel(self.action_sub, text=info_text, text_color=info_color)
                info_lbl.pack(side="left", padx=5)
                self.conversion_widgets.append(info_lbl)

        token = self._cancel_pending_cover()
        threading.Thread(target=self.fetch_cover_art, daemon=True,
                         args=(entry.name, is_og_xbox_entry(entry), is_dlc_entry(entry), token)).start()

    def fetch_cover_art(self, game_name: str, og: bool, dlc: bool = False, token: int | None = None):
        """Covers come from the cover repo on GitHub (see bman_core.cover_repo_path)."""
        def stale() -> bool:
            return token is not None and token != getattr(self, "_cover_token", token)

        data = None
        try:
            data = fetch_cover(game_name, og=og, dlc=dlc)
        except Exception as exc:
            log_event(f"Cover art lookup failed for {game_name}: {exc}")

        if data:
            def _set_cover(data=data):
                if stale():
                    return
                self._set_cover_image(data)
            post_ui(_set_cover)
            return

        # Couldn't reach GitHub: say so, instead of a cover box that looks stuck.
        text = "Covers offline" if covers_offline() else "No Cover"

        def _no_cover():
            if stale():
                return
            self._clear_cover(text)
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
        return not (row in live_rows or row.is_active or row.is_uploading
                    or getattr(row, "is_working", False))

    def _destroy_download_row(self, row) -> None:
        for slot in ("dl-progress", "dl-status", "row-status", "upload-progress"):
            drop_ui_slot((slot, id(row)))
        try:
            row.destroy()
        except Exception:
            pass

    def _downloads_running(self) -> bool:
        """True while the queue still has something to download, extract, convert or upload."""
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
        for widget in self._download_rows():
            if widget.raw_filename == entry.name:
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
            row = DownloadItemFrame(self.downloads_scroll, entry.name, entry=entry,
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

    def _queue_entries(self, entries: list, mode: str, upload_job) -> int:
        queued = 0
        to_rename = []
        for entry in entries:
            if entry is None:
                continue
            conv_mode = mode if entry.allow_iso_conversion else "3"
            job = QueuedDownload(entry, conv_mode, upload_job)
            if self._job_needs_hdd_rename(job):
                job.rename_ready.clear()        # its upload waits for the answer; the download does not
                to_rename.append(job)
            self._dl_waiting.append(job)
            self.download_queue.put(job)
            queued += 1

        if to_rename:
            # Asked once the Downloads tab is showing, all of them up front, so the
            # queue can then be left to download and upload on its own.
            self._hdd_rename_backlog.extend(to_rename)
            self.after(250, self._ask_hdd_renames)

        if queued:
            self._fill_queue_rows()
            with self._queue_start_lock:
                self.is_downloading = True
                if not self._dl_thread_running:
                    self._dl_thread_running = True
                    threading.Thread(target=self._process_download_queue, daemon=True).start()
        return queued

    def _job_needs_hdd_rename(self, job) -> bool:
        """True when this queued game will be uploaded (HDD or FTP) as a folder with too long a name."""
        try:
            upload_job, entry = job.upload_job, job.entry
            if not upload_job or upload_job[0] not in ("hdd", "ftp"):
                return False
            if is_catalog_entry(entry):
                return False                    # files go loose under FFFE07D1, no folder of its own
            if entry_is_content(entry):
                return False                    # goes in as title ID folders, the title is never written
            if entry.allow_iso_conversion and job.conv_mode != "1":
                return False                    # GOD (or left as an ISO): no folder named after the game
            return hdd_title_needs_rename(Path(entry.name).stem)
        except Exception:
            return False

    def _ask_hdd_renames(self) -> None:
        """Open the rename prompt for every queued game still waiting to be asked."""
        if self._hdd_rename_dialog is not None or not self._hdd_rename_backlog:
            return
        batch = [job for job in self._hdd_rename_backlog]
        self._hdd_rename_backlog = []
        asking = {id(job) for job in batch}

        def active(job) -> bool:
            return not job.done and not job.cancel_event.is_set()

        # Names other games in the queue will already have where they are uploaded.
        taken = set()
        for job in list(self._dl_waiting) + list(self._dl_live):
            if not active(job) or not job.upload_job:
                continue
            if job.rename_to:
                taken.add(job.rename_to.lower())
            elif id(job) not in asking:
                taken.add(Path(job.entry.name).stem.lower())

        kept_long = []

        def on_pick(index, name):
            job = batch[index]
            title = Path(job.entry.name).stem
            job.rename_to = name or None
            job.rename_declined = not name
            job.rename_ready.set()
            if name:
                log_event(f"Rename: '{title}' will be uploaded as '{name}'.")
            elif active(job):
                kept_long.append(title)

        def on_finished():
            self._hdd_rename_dialog = None
            if kept_long:
                log_event(f"{len(kept_long)} long name(s) were not renamed - those games will be "
                          f"downloaded but not uploaded. Use the row's Upload button afterwards "
                          f"to rename and upload them.")
            if self._hdd_rename_backlog:
                self.after(50, self._ask_hdd_renames)

        kinds = {job.upload_job[0] for job in batch if job.upload_job}
        where = {("hdd",): "the HDD", ("ftp",): "the FTP upload"}.get(tuple(sorted(kinds)), "the upload")
        try:
            dialog = HddRenameDialog(
                self, [Path(job.entry.name).stem for job in batch], on_pick, taken=taken,
                still_wanted=lambda index: active(batch[index]), on_finished=on_finished,
                where=where)
            # Every game in it may have been cancelled already, in which case it closed itself.
            self._hdd_rename_dialog = None if dialog._finished else dialog
        except Exception as exc:
            self._hdd_rename_dialog = None
            log_event(f"Rename prompt failed: {exc}")
            for job in batch:
                job.rename_declined = True
                job.rename_ready.set()          # never leave an upload waiting on a prompt that is not there

    @staticmethod
    def _upload_is_forced(entry, conv_mode: str) -> bool:
        """True when this game's upload ignores the picked folder anyway.

        GOD games, title ID content and themes / gamer pictures always land under
        Content/0000000000000000, so asking for a folder for them is pointless.
        """
        if entry is None:
            return False
        if is_catalog_entry(entry):
            return True
        if not FORCE_TITLE_ID_TO_CONTENT:
            return False
        if entry_is_content(entry):
            return True
        return bool(entry.allow_iso_conversion) and conv_mode == "2"

    def _forced_upload_dir(self, kind: str) -> str:
        """The folder handed to a forced upload: the default from Settings (used only to
        work out the drive, e.g. /Hdd1, and as the fallback if GOD has to fall back to XISO)."""
        cfg = self.ftp_config if kind == "ftp" else self.hdd_config
        return cfg.default_path or "/"

    def _forced_upload_note(self, kind: str) -> str:
        base = self._forced_upload_dir(kind)
        if kind == "ftp":
            return forced_content_destination(base, drive_prefixed=True,
                                              fallback_path=self.ftp_config.default_path)
        return forced_content_destination(base, drive_prefixed=False)

    def start_bulk_download(self, entries: list):
        if not self.require_api_keys():
            return
        entries = [e for e in entries if e is not None]
        if not entries:
            return

        count = len(entries)
        dialog = CollectionQueueDialog(
            self, f"{count:,} selected items", count,
            ftp_online=self.ftp_online, hdd_online=self.hdd_online,
            ftp_label=self.ftp_config.host if self.ftp_online else "",
            hdd_label=self.hdd_config.label if self.hdd_online else "",
            intro=f"{count:,} selected item{'s' if count != 1 else ''} will be added to the "
                  f"download queue.",
            window_title="Download Selected",
        )
        self.wait_window(dialog)
        if not dialog.result:
            return

        mode = dialog.result.get("mode", "2")
        destination = dialog.result.get("destination", "local")

        upload_job = None
        if destination in ("ftp", "hdd") and all(
                self._upload_is_forced(e, mode if e.allow_iso_conversion else "3") for e in entries):
            upload_job = (destination, self._forced_upload_dir(destination))
            log_event(f"Every selected item goes to {self._forced_upload_note(destination)} - "
                      f"no folder to pick.")
        elif destination in ("ftp", "hdd"):
            what = {"2": "the GOD conversions", "1": "the extracted XISOs"}.get(
                mode, "the downloaded files")
            if destination == "ftp":
                where = self.require_ftp_destination(
                    subtitle=(f"{count:,} selected item{'s' if count != 1 else ''}\n\n"
                              f"Each one will be downloaded, then {what} will be uploaded into "
                              f"the folder you pick on {self.ftp_config.host}.")
                )
            else:
                where = self.require_hdd_destination(
                    subtitle=(f"{count:,} selected item{'s' if count != 1 else ''}\n\n"
                              f"Each one will be downloaded, then {what} will be written into "
                              f"the folder you pick on {self.hdd_config.label}.")
                )
            if not where:
                return
            upload_job = (destination, where)

        queued = self._queue_entries(entries, mode, upload_job)
        where_note = ""
        if upload_job:
            where_note = (f", then {'uploaded to ' if upload_job[0] == 'ftp' else 'written to '}"
                          f"{upload_job[1]}")
        log_event(f"Queued {queued} selected item(s), mode {mode}{where_note}.")
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
        entry = self.selected_file
        conv_mode = self.conversion_var.get() if entry.allow_iso_conversion else "3"

        upload_job = None
        want_ftp = self.ftp_online and self.ftp_upload_var.get()
        want_hdd = self.hdd_online and self.hdd_upload_var.get()

        if (want_ftp or want_hdd) and self._upload_is_forced(entry, conv_mode):
            kind = "ftp" if want_ftp else "hdd"
            upload_job = (kind, self._forced_upload_dir(kind))
            log_event(f"{entry.name} will be uploaded to {self._forced_upload_note(kind)} - "
                      f"no folder to pick.")
        elif want_ftp or want_hdd:
            if entry_is_content(entry):
                what = "the title ID content folder (e.g. 584E07D2) on its own"
            elif entry.allow_iso_conversion and conv_mode == "2":
                what = "the GOD conversion"
            elif entry.allow_iso_conversion and conv_mode == "1":
                what = "the extracted XISO"
            else:
                what = "the extracted files"

            if want_ftp:
                destination = self.require_ftp_destination(
                    subtitle=(f"{entry.name}\n\nAfter downloading, {what} will be uploaded into the "
                              f"folder you pick on {self.ftp_config.host}.")
                )
                kind = "ftp"
            else:
                destination = self.require_hdd_destination(
                    subtitle=(f"{entry.name}\n\nAfter downloading, {what} will be written into the "
                              f"folder you pick on {self.hdd_config.label}.")
                )
                kind = "hdd"

            if not destination:
                return
            upload_job = (kind, destination)

        if self._queue_entries([entry], conv_mode, upload_job):
            self.select_tab("downloads")
            
    def _process_download_queue(self):
        """Download stage: fetches the queued games one at a time.

        A finished download is handed to the processing stage, which extracts,
        converts and uploads it. Whether the next download waits for that to
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
        before it has been extracted, converted and uploaded.
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
        """Processing stage: extract / convert / upload, one downloaded game at a time, in order."""
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

            dest = self.downloader.fetch(entry, DOWNLOAD_DIR, progress_callback=prog_cb, cancel_event=cancel_event)

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
        """Extract / convert a downloaded game, then upload it if that was asked for."""
        dl_item = job.row
        entry, conv_mode, cancel_event, upload_job = (
            job.entry, job.conv_mode, job.cancel_event, job.upload_job)

        if cancel_event.is_set():       # cancelled while it waited for its turn
            post_ui(lambda d=dl_item: d.update_status("Cancelled"))
            return

        try:
            def process_cb(msg):
                post_ui_latest(("dl-status", id(dl_item)),
                               lambda m=msg: dl_item.update_status(m))

            log_event(f"Processing {Path(dest).name} (mode {conv_mode})...")
            success = handle_post_download(dest, entry, conversion_mode=conv_mode, progress_callback=process_cb)
            if success:
                log_event(f"Processing finished: {Path(dest).name}")
                if upload_job and not cancel_event.is_set():
                    self._upload_after_download(dest, dl_item, upload_job, cancel_event, job=job)
                else:
                    god_dir = EXTRACT_DIR / Path(dest).stem / "GOD_Output"
                    fell_back = conv_mode == "2" and not (god_dir.exists() and any(god_dir.iterdir()))
                    done_text = ("Completed as XISO (GOD conversion failed)" if fell_back
                                 else "Completed!")

                    def _on_success(d=dl_item, t=done_text):
                        d.update_status(t)
                        d.check_iso_and_files()
                    post_ui(_on_success)
            else:
                if cancel_event.is_set():
                    post_ui(lambda d=dl_item: d.update_status("Cancelled"))
                else:
                    post_ui(lambda d=dl_item: d.update_status("Error: Post-processing failed (see Logs)."))
                    log_event(f"ERROR: post-processing failed for '{entry.name}' (reason above) - "
                              f"moving on to the next game.")
        except Exception as exc:
            if cancel_event.is_set():
                post_ui(lambda d=dl_item: d.update_status("Cancelled"))
            else:
                import traceback
                post_ui(lambda d=dl_item, e=exc: d.update_status(f"Error: {e}"))
                log_event(f"ERROR processing/uploading '{entry.name}': {exc} - moving on to the next game.\n"
                          f"{traceback.format_exc().rstrip()}")

    def _upload_after_download(self, archive_path: Path, dl_item, upload_job, cancel_event, job=None):
        kind, remote_dir = upload_job
        game_dir = EXTRACT_DIR / Path(archive_path).stem
        where = "FTP" if kind == "ftp" else "HDD"

        rename_to = None
        if job is not None:
            if not job.rename_ready.is_set():
                # Normally answered long before this, while the game was still downloading.
                post_ui(lambda d=dl_item: d.update_status("Waiting for a shorter name - answer the rename prompt"))
                while not job.rename_ready.wait(0.5):
                    if cancel_event.is_set():
                        post_ui(lambda d=dl_item: d.update_status("Cancelled"))
                        return
            if job.rename_declined:
                # Never uploaded under the long name: it stays downloaded, ready for the row's Upload button.
                def not_uploaded(d=dl_item):
                    d.is_active = False
                    try:
                        if d.cancel_event and d.cancel_btn.winfo_exists():
                            d.cancel_btn.pack_forget()
                    except Exception:
                        pass
                    d.status_lbl.configure(text="Downloaded - not uploaded (name too long, not renamed)")
                    d.check_iso_and_files()

                post_ui(not_uploaded)
                log_event(f"{where} upload skipped for {game_dir.name}: its name is too long and "
                          f"it was not renamed.")
                return
            rename_to = job.rename_to

        upload_slot = ("upload-progress", id(dl_item))

        def prog(percent, headline, detail):
            post_ui_latest(upload_slot,
                           lambda p=percent, h=headline, d=detail: dl_item.update_upload_progress(p, h, d))

        post_ui(lambda d=dl_item, w=where: d.update_status(f"Preparing {w} upload..."))
        if kind == "hdd":
            ok, message = upload_game_to_hdd(self.hdd_config, game_dir, remote_dir,
                                             progress_callback=prog, cancel_event=cancel_event,
                                             archive_hint=Path(archive_path),
                                             entry=getattr(dl_item, "entry", None),
                                             rename_to=rename_to)
        else:
            ok, message = upload_game_via_ftp(self.ftp_config, game_dir, remote_dir,
                                              progress_callback=prog, cancel_event=cancel_event,
                                              archive_hint=Path(archive_path),
                                              entry=getattr(dl_item, "entry", None),
                                              rename_to=rename_to)

        landed = remote_dir
        landed_catalog = catalog_folder_kind(game_dir, getattr(dl_item, "entry", None))
        if landed_catalog is not None:
            landed = catalog_upload_destination(
                kind, remote_dir, self.ftp_config.default_path if kind == "ftp" else "",
                landed_catalog.content_dir)

        deleted = False
        if ok and delete_after_upload_enabled():
            log_event(f"{where} upload done for {game_dir.name} - deleting the local copy (setting is on).")
            deleted = delete_local_copies(game_dir.name, Path(archive_path))

        def finish(d=dl_item, ok=ok, message=message, deleted=deleted, remote_dir=landed):
            d.is_active = False
            try:
                if d.cancel_event and d.cancel_btn.winfo_exists():
                    d.cancel_btn.pack_forget()
            except Exception:
                pass
            if ok:
                d.update_upload_progress(100, "Completed + Uploaded!" + (" (deleted from PC)" if deleted else ""),
                                         f"Uploaded to {remote_dir}")
            else:
                d.status_lbl.configure(text=f"Downloaded - {where} upload failed (see Logs)")
            d.check_iso_and_files()

        post_ui(finish)
        if not ok:
            log_event(f"{where} upload failed for {game_dir.name}: {message}")

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

    def _entry_for_filename(self, filename: str):
        index = getattr(self, "index", None)
        if index is None:
            return catalog_entry_for_filename(filename)
        lookup = getattr(self, "_entry_by_name", None)
        if lookup is None or getattr(self, "_entry_lookup_for", None) is not index:
            lookup = {}
            try:
                for candidate in index.entries:
                    base = Path(candidate.name).name
                    lookup.setdefault(base.lower(), candidate)
                    lookup.setdefault(Path(base).stem.lower(), candidate)
            except Exception:
                lookup = {}
            self._entry_by_name = lookup
            self._entry_lookup_for = index
        base = Path(filename).name
        return (lookup.get(base.lower()) or lookup.get(Path(base).stem.lower())
                or catalog_entry_for_filename(base))

    def _downloaded_items(self) -> list:
        """Everything finished on disk as (row name, status, stem), in the order the tab lists it."""
        items, names, stems = [], set(), set()
        if DOWNLOAD_DIR.exists():
            for path in sorted(DOWNLOAD_DIR.iterdir(), key=lambda p: p.name.upper()):
                if path.is_file() and path.suffix.lower() != ".part":
                    stem = path.stem
                    if path.name not in names and stem not in stems:
                        items.append((path.name, "Downloaded", stem))
                        names.add(path.name)
                        stems.add(stem)

        if EXTRACT_DIR.exists():
            for path in sorted(EXTRACT_DIR.iterdir(), key=lambda p: p.name.upper()):
                if path.is_dir():
                    stem = path.name
                    if stem not in stems:
                        matched_name = stem + ".zip"
                        archive_candidates = list(DOWNLOAD_DIR.glob(f"{stem}.*"))
                        if archive_candidates:
                            matched_name = archive_candidates[0].name

                        if matched_name not in names:
                            items.append((matched_name, "Extracted", stem))
                            names.add(matched_name)
                            stems.add(stem)
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
                if row.raw_filename in keep_names or Path(row.raw_filename).stem in keep_stems:
                    continue
                self._destroy_download_row(row)

        rows = self._download_rows()
        existing_filenames = {w.raw_filename for w in rows}
        processed_stems = {Path(w.raw_filename).stem for w in rows}

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

    def start_bulk_process(self, mode: str):
        if self._bulk_running:
            return
        self._bulk_running = True
        self._set_bulk_buttons("disabled")
        self._set_bulk_status("Scanning...", "gray")
        threading.Thread(target=self._bulk_worker, args=(mode,), daemon=True).start()

    def _set_bulk_buttons(self, state: str):
        for button in (self.bulk_god_btn, self.bulk_xex_btn, self.bulk_ftp_btn, self.bulk_hdd_btn):
            try:
                button.configure(state=state)
            except Exception:
                pass

    def start_bulk_ftp_upload(self):
        self._start_bulk_upload("ftp")

    def start_bulk_hdd_upload(self):
        self._start_bulk_upload("hdd")

    def _start_bulk_upload(self, kind: str):
        if self._bulk_running:
            return

        if kind == "hdd":
            if not self.hdd_online:
                self._set_bulk_status("No hard drive mounted - check Settings.", "#d97070")
                return
            title, where = "Upload All to HDD", self.hdd_config.label
        else:
            if not self.ftp_online:
                self._set_bulk_status("FTP is not connected - check Settings.", "#d97070")
                return
            title, where = "Upload All via FTP", self.ftp_config.host

        options = FtpFormatDialog(self, title=title)
        self.wait_window(options)
        mode = options.result
        if not mode:
            return

        if mode == "2":
            what = "Everything is converted to GOD first, then uploaded"
        elif mode == "1":
            what = "Everything is extracted as XISO (loose files) first, then uploaded"
        else:
            what = "Everything already converted or extracted gets uploaded as-is"

        subtitle = f"{what} into the folder you pick on {where}."
        remote_dir = (self.require_hdd_destination(subtitle=subtitle) if kind == "hdd"
                      else self.require_ftp_destination(subtitle=subtitle))
        if not remote_dir:
            return

        self._bulk_running = True
        self._set_bulk_buttons("disabled")
        self._set_bulk_status("Checking names...", "gray")
        threading.Thread(target=self._bulk_check_names,
                         args=(kind, mode, remote_dir), daemon=True).start()

    def _bulk_check_names(self, kind: str, mode: str, remote_dir: str):
        """Upload All, first step: find every game whose uploaded folder name would be too long.

        They are all asked about before anything is extracted, converted or uploaded,
        so the rest can run unattended.
        """
        try:
            folders = predict_upload_folders(mode)
        except Exception as exc:
            log_event(f"Upload All: could not check the names ({exc}) - carrying on without.")
            folders = []
        too_long = [(game, name) for game, name in folders if hdd_title_needs_rename(name)]
        taken = {name.lower() for _game, name in folders if not hdd_title_needs_rename(name)}
        renames: dict = {}

        def start():
            self._set_bulk_status("Scanning...", "gray")
            threading.Thread(target=self._bulk_upload_worker,
                             args=(kind, mode, remote_dir, renames), daemon=True).start()

        if not too_long:
            post_ui(start)
            return

        def on_pick(index, name):
            game, old = too_long[index]
            if name:
                renames[game] = name
                log_event(f"Rename: '{old}' will be uploaded as '{name}'.")

        def answered():
            missing = len(too_long) - len(renames)
            if not missing:
                start()
                return
            # The prompt was closed. Nothing goes up under a long name, so nothing goes up at all.
            where = "HDD" if kind == "hdd" else "FTP"
            log_event(f"=== {where} Upload All cancelled - {missing} long name(s) were not renamed. "
                      f"Nothing was uploaded. ===")
            self._bulk_finished()
            self._set_bulk_status(
                f"{where} Upload All cancelled - the rename window was closed. Nothing was uploaded.",
                "#d97070")

        def ask():
            self._set_bulk_status(
                f"{len(too_long)} name{'s are' if len(too_long) != 1 else ' is'} too long - "
                f"waiting for the rename prompt...", "gray")
            try:
                HddRenameDialog(self, [name for _game, name in too_long], on_pick, taken=taken,
                                on_finished=answered,
                                where="the HDD" if kind == "hdd" else "the FTP upload")
            except Exception as exc:
                log_event(f"Rename prompt failed: {exc}")
                answered()

        post_ui(ask)

    def _bulk_upload_worker(self, kind: str, mode: str, remote_dir: str, renames: dict | None = None):
        label = {"2": "GOD", "1": "XEX", "3": "Upload"}.get(mode, "Upload")
        where = "HDD" if kind == "hdd" else "FTP"
        renames = renames or {}
        uploaded = skipped = failed = 0
        try:
            archives = find_unextracted_archives()
            if archives:
                log_event(f"=== {where} UPLOAD ALL: {len(archives)} archive(s) to extract ===")
            for i, archive in enumerate(archives, 1):
                if self._stop_for_switch:
                    break                   # switching apps: stop after the last game
                post_ui(lambda t=f"Extracting {i}/{len(archives)}: {archive.name}": self._set_bulk_status(t))
                if not handle_post_download(archive, entry=None, conversion_mode="3"):
                    log_event(f"  extraction FAILED: {archive.name} (reason above) - moving on")

            if mode in ("1", "2"):
                isos = find_extracted_isos()
                total = len(isos)
                log_event(f"=== {where} UPLOAD ALL: {total} ISO(s) to convert to {label} ===")
                for i, iso in enumerate(isos, 1):
                    if self._stop_for_switch:
                        break                   # switching apps: stop after the last game
                    def prog(msg, idx=i, name=iso.name):
                        post_ui_latest(_BULK_STATUS_SLOT,
                                       lambda: self._set_bulk_status(f"{label} {idx}/{total}: {name}\n{msg}"))
                    post_ui(lambda t=f"{label} {i}/{total}: {iso.name}": self._set_bulk_status(t))
                    result = bulk_convert_iso(iso, mode, progress_callback=prog)
                    if result == "failed":
                        log_event(f"  conversion FAILED: {iso.name}")

            games = []
            if EXTRACT_DIR.exists():
                games = sorted([p for p in EXTRACT_DIR.iterdir() if p.is_dir()])
            log_event(f"=== {where} UPLOAD ALL: {len(games)} game folder(s) -> {remote_dir} ===")

            for i, game_dir in enumerate(games, 1):
                if self._stop_for_switch:
                    break                   # switching apps: stop after the last game
                try:
                    upload_kind, _path, _sub = analyze_upload_target(game_dir)
                except Exception as exc:
                    failed += 1
                    log_event(f"  [{i}/{len(games)}] FAILED {game_dir.name}: could not inspect it: {exc}")
                    continue
                if upload_kind in ("needs_convert", "missing", "empty"):
                    skipped += 1
                    log_event(f"  [{i}/{len(games)}] skipped {game_dir.name} ({upload_kind})")
                    continue

                def prog(percent, headline, detail, idx=i, name=game_dir.name):
                    post_ui_latest(_BULK_STATUS_SLOT, lambda: self._set_bulk_status(
                        f"Uploading {idx}/{len(games)}: {name}\n{headline}\n{detail}"))

                post_ui(lambda t=f"Uploading {i}/{len(games)}: {game_dir.name}": self._set_bulk_status(t))
                try:
                    if kind == "hdd":
                        ok, message = upload_game_to_hdd(self.hdd_config, game_dir, remote_dir,
                                                         progress_callback=prog,
                                                         rename_to=renames.get(game_dir.name))
                    else:
                        ok, message = upload_game_via_ftp(self.ftp_config, game_dir, remote_dir,
                                                          progress_callback=prog,
                                                          rename_to=renames.get(game_dir.name))
                except Exception as exc:
                    ok, message = False, f"unexpected error: {exc}"
                if ok:
                    uploaded += 1
                    if delete_after_upload_enabled():
                        delete_local_copies(game_dir.name)
                else:
                    failed += 1
                    log_event(f"  [{i}/{len(games)}] FAILED {game_dir.name}: {message}")

            summary = f"{where} Upload All finished - {uploaded} uploaded"
            if skipped:
                summary += f", {skipped} skipped"
            if failed:
                summary += f", {failed} failed"
            log_event(f"=== {summary} ===")
            color = "#d97070" if failed else "green"
            post_ui(lambda t=summary, c=color: self._set_bulk_status(t, c))
        except Exception as exc:
            log_event(f"{where} Upload All error: {exc}")
            post_ui(lambda e=exc, w=where: self._set_bulk_status(f"{w} Upload All error: {e}", "#d97070"))
        finally:
            self._bulk_running = False      # here too: after an app switch the window is gone
            post_ui(self._bulk_finished)

    def _bulk_worker(self, mode: str):
        label = "GOD" if mode == "2" else "XEX"
        done = skipped = failed = 0
        try:
            archives = find_unextracted_archives()
            log_event(f"=== {label} ALL: {len(archives)} archive(s) to extract ===")
            for i, archive in enumerate(archives, 1):
                if self._stop_for_switch:
                    break                   # switching apps: stop after the last game
                post_ui(lambda t=f"Extracting {i}/{len(archives)}: {archive.name}": self._set_bulk_status(t))
                log_event(f"  extracting {archive.name}")
                try:
                    if not handle_post_download(archive, entry=None, conversion_mode="3"):
                        log_event(f"  extraction failed: {archive.name}")
                except Exception as exc:
                    log_event(f"  extraction error for {archive.name}: {exc}")

            isos = find_extracted_isos()
            log_event(f"=== {label} ALL: {len(isos)} ISO(s) found under extracted/ ===")
            total = len(isos)
            for i, iso in enumerate(isos, 1):
                if self._stop_for_switch:
                    break                   # switching apps: stop after the last game
                def prog(msg, idx=i, name=iso.name):
                    post_ui_latest(_BULK_STATUS_SLOT,
                                   lambda: self._set_bulk_status(f"{label} {idx}/{total}: {name}\n{msg}"))
                post_ui(lambda t=f"{label} {i}/{total}: {iso.name}": self._set_bulk_status(t))
                log_event(f"  [{i}/{total}] {label}: {iso}")
                result = bulk_convert_iso(iso, mode, progress_callback=prog)
                if result == "done":
                    done += 1
                elif result == "skipped":
                    skipped += 1
                    log_event(f"    already converted, skipped")
                else:
                    failed += 1
                    log_event(f"    FAILED: {iso.name}")

            summary = f"{label} All finished - {done} converted"
            if skipped:
                summary += f", {skipped} skipped"
            if failed:
                summary += f", {failed} failed"
            log_event(f"=== {summary} ===")
            color = "#d97070" if failed else "green"
            post_ui(lambda t=summary, c=color: self._set_bulk_status(t, c))
        except Exception as exc:
            log_event(f"{label} All error: {exc}")
            post_ui(lambda e=exc: self._set_bulk_status(f"{label} All error: {e}", "#d97070"))
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
        header.grid_columnconfigure(4, weight=1)

        ctk.CTkButton(header, text="+ New Collection", width=150, fg_color="#2b8a3e",
                      hover_color="#236c31", command=self.new_collection_action).grid(row=0, column=0)
        ctk.CTkButton(header, text="Import Collection...", width=160, fg_color="#7a4bbd",
                      hover_color="#623a99", command=self.import_collection_action).grid(row=0, column=1, padx=8)
        ctk.CTkButton(header, text="Refresh", width=90, command=self.refresh_collections_view).grid(row=0, column=2)

        self._collections_filter = "all"
        self._make_kind_filter(header, self._on_collections_filter).grid(row=0, column=3, padx=(16, 0))

        self.collections_status_lbl = ctk.CTkLabel(header, text="", text_color="gray",
                                                   font=ctk.CTkFont(size=11), anchor="w")
        self.collections_status_lbl.grid(row=0, column=4, sticky="ew", padx=(12, 0))

        self.collections_scroll = ScrollFrame(self.collections_view, label_text="Your Collections")
        self.collections_scroll.grid(row=1, column=0, sticky="nsew")
        self.collections_scroll.grid_columnconfigure(0, weight=1)

        self._collection_expanded: set = set()
        self._collection_cards: dict = {}
        self._collections_message = None
        self._collections_after = None

    def _on_collections_filter(self, choice: str) -> None:
        self._collections_filter = self._kind_from_choice(choice)
        self.refresh_collections_view()

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
                "section": ctk.CTkFont(size=12, weight="bold"),
            }
        return fonts[kind]

    # ----- item kinds (games / themes / profile pics) in collections ------------
    KIND_FILTER_CHOICES = ("All", "Games", "Themes", "Profile Pics")

    @staticmethod
    def _kind_from_choice(choice: str) -> str:
        """'Themes' -> 'themes'; 'All' -> 'all'."""
        for kind, label in ITEM_KIND_LABELS.items():
            if label == choice:
                return kind
        return "all"

    def _make_kind_filter(self, parent, on_change):
        """The 'Show: All | Games | Themes | Profile Pics' switch."""
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(holder, text="Show:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        switch = ctk.CTkSegmentedButton(holder, values=list(self.KIND_FILTER_CHOICES),
                                        command=on_change)
        switch.set("All")
        switch.pack(side="left")
        return holder

    def _download_label(self, kind: str) -> str:
        return "Download All" if kind not in ITEM_KINDS else f"Download {ITEM_KIND_LABELS[kind]}"

    def _fill_item_sections(self, frame, items, kind_filter: str, scroll, on_remove=None) -> None:
        """List a collection's items under Games / Themes / Profile Pics headings.

        kind_filter 'all' shows every kind that has something in it; one kind
        shows just that section. on_remove(name) adds a Remove button per row."""
        groups = []
        for kind in ITEM_KINDS:
            if kind_filter in ITEM_KINDS and kind != kind_filter:
                continue
            members = [item for item in items if item_kind(item) == kind]
            if members or kind_filter == kind:
                groups.append((kind, members))
        if not groups:
            ctk.CTkLabel(frame, text="Nothing in here yet.", text_color="gray",
                         anchor="w").grid(row=0, column=0, sticky="w", padx=10, pady=8)
            return

        row = 0
        for kind, members in groups:
            ctk.CTkLabel(frame, text=f"{ITEM_KIND_LABELS[kind]} ({len(members):,})",
                         font=self._card_font("section"), anchor="w").grid(
                row=row, column=0, sticky="w", padx=10, pady=(8, 2))
            row += 1
            if not members:
                ctk.CTkLabel(frame, text=f"No {ITEM_KIND_LABELS[kind].lower()} in here.",
                             text_color="gray", anchor="w").grid(
                    row=row, column=0, sticky="w", padx=10, pady=(0, 8))
                row += 1
                continue
            section = ctk.CTkFrame(frame, fg_color="transparent")
            section.grid(row=row, column=0, sticky="ew", pady=(0, 4))
            section.grid_columnconfigure(0, weight=1)
            row += 1
            names = [item.name for item in members]
            handler = None
            if on_remove is not None:
                handler = lambda i, n=names: on_remove(n[i])
            build_game_rows(section, [item_label(item) for item in members],
                            button_text="Remove" if on_remove is not None else "",
                            on_button=handler, scroll=scroll)

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
                    "Find a game in Library / Search (or a theme / profile pic pack on the Themes or "
                    "Gamer Pictures tab), select it, and hit '+ Add to Collection'.",
                    justify="left", wraplength=640)
                return

            kind_filter = getattr(self, "_collections_filter", "all")
            if kind_filter in ITEM_KINDS:
                collections = [c for c in collections if c.kind_counts()[kind_filter]]
                if not collections:
                    sync_cards(self.collections_scroll, self._collection_cards, {}, [], None)
                    self._show_list_message(
                        self.collections_scroll, "_collections_message",
                        f"None of your collections have {ITEM_KIND_LABELS[kind_filter].lower()} in them."
                        f"\n\nPick 'All' above to see every collection.",
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
            buttons, text="Show Items", width=110,
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
            "download": download_btn,
        }
        # What the card shows right now; _update_collection_card only touches what differs.
        card._shown = {"meta": "", "description": "", "has_games": True, "expanded": False,
                       "games": None, "download_text": "Download All"}
        return card

    def _update_collection_card(self, card, collection: GameCollection) -> None:
        """Make a card show `collection` as it is now, changing only what is out of date."""
        parts, shown = card._parts, card._shown

        total = collection.total_size()
        meta = describe_item_counts(collection.kind_counts())
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

        kind_filter = getattr(self, "_collections_filter", "all")
        download_text = self._download_label(kind_filter)
        if download_text != shown["download_text"]:
            parts["download"].configure(text=download_text)
            shown["download_text"] = download_text

        expanded = collection.name in self._collection_expanded
        if expanded != shown["expanded"]:
            parts["toggle"].configure(text="Hide Items" if expanded else "Show Items")
            shown["expanded"] = expanded

        games = None
        if expanded:
            games = (kind_filter,) + tuple((game.name, game.archive_identifier, game.size)
                                           for game in collection.games)
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

        self._fill_item_sections(
            games_frame, collection.games, kind_filter, self.collections_scroll,
            on_remove=lambda name, c=collection.name: self.remove_game_from_collection(c, name))

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

        def hint(text: str) -> None:
            self.collection_hint_lbl.configure(text=text)
            self.after(6000, lambda: self.collection_hint_lbl.configure(text=""))

        self.add_entries_to_collection(entries, hint)

    def add_entries_to_collection(self, entries: list, hint=None) -> None:
        """Ask which collection, then put `entries` (games, themes or profile pic
        packs) in it. `hint(text)` shows the outcome next to the button."""
        entries = [e for e in entries or [] if e is not None]
        if not entries:
            return
        hint = hint or (lambda _text: None)

        if len(entries) == 1:
            entry = entries[0]
            shown = entry.name if item_kind(entry) == "games" else catalog_display_name(entry.name)
            dialog = CollectionPickerDialog(self, shown, game_names=[entry.name])
        else:
            dialog = CollectionPickerDialog(
                self, f"{describe_item_counts(count_item_kinds(entries))} selected",
                game_names=[e.name for e in entries],
            )
        self.wait_window(dialog)
        chosen = dialog.result
        if not chosen:
            return

        collection = get_collection(chosen)
        if collection is None:
            log_event(f"Collection '{chosen}' vanished before the item could be added.")
            return

        added = collection.add_many(entries)
        skipped = len(entries) - added
        if added:
            upsert_collection(collection)
            try:
                self.refresh_collections_view()
            except Exception:
                pass

        if len(entries) == 1:
            if added:
                hint(f"Added to '{collection.name}'.")
                log_event(f"Added '{entries[0].name}' to collection '{collection.name}'.")
            else:
                hint(f"Already in '{collection.name}'.")
        elif added:
            note = f"Added {added:,} to '{collection.name}'."
            if skipped:
                note += f" {skipped:,} already there."
            hint(note)
            log_event(f"Added {added} item(s) to collection '{collection.name}' "
                      f"({skipped} already there).")
        else:
            hint(f"All {len(entries):,} were already in '{collection.name}'.")

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
            f"Send that file to anyone running Bman Downloader - they hit "
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

        what = describe_item_counts(collection.kind_counts())
        log_event(f"Imported collection '{collection.name}' with {what} "
                  f"({matched} matched the local index).")
        self._set_collections_status(f"Imported '{collection.name}' ({what}).")

        if ask_confirm_popup(
            self, "Collection Imported",
            f"'{collection.name}' is in - {what}."
            f"{index_note}{renamed_note}\n\nQueue them all for download now?",
            confirm_text="Queue them up", cancel_text="Later",
        ):
            self.queue_collection(collection.name, kind="all")

    def download_collection_action(self, name: str):
        self.queue_collection(name)

    def queue_collection(self, name: str, kind: str | None = None):
        """Queue a collection's items; kind ('games'/'themes'/'gamerpics') queues just
        those. From the Collections tab it follows the Show filter."""
        if not self.require_api_keys():
            return
        collection = get_collection(name)
        if collection is None or not collection.games:
            return
        if kind is None:
            kind = getattr(self, "_collections_filter", "all")
        picked = collection.only(kind)
        if not picked.games:
            self._set_collections_status(
                f"'{name}' has no {ITEM_KIND_LABELS.get(kind, 'items').lower()} in it.")
            return
        self._run_collection_queue(picked, self._set_collections_status)

    def _run_collection_queue(self, collection: GameCollection, status=None):
        if not self.require_api_keys():
            return
        if collection is None or not collection.games:
            return
        if status is None:
            status = self._set_collections_status

        kinds = collection.kind_counts()
        what = describe_item_counts(kinds)
        dialog = CollectionQueueDialog(
            self, collection.name, len(collection.games),
            ftp_online=self.ftp_online, hdd_online=self.hdd_online,
            ftp_label=self.ftp_config.host if self.ftp_online else "",
            hdd_label=self.hdd_config.label if self.hdd_online else "",
            intro=f"{what[0].upper() + what[1:]} will be added to the download queue."
                  + (" Themes and profile pics go to the console's theme / gamer picture "
                     "folders automatically." if kinds["themes"] or kinds["gamerpics"] else ""),
            show_convert=bool(kinds["games"]),
        )
        self.wait_window(dialog)
        if not dialog.result:
            return

        mode = dialog.result.get("mode", "2")
        destination = dialog.result.get("destination", "local")

        upload_job = None
        if destination in ("ftp", "hdd") and all(
                self._upload_is_forced(g, mode if g.allow_iso_conversion else "3")
                for g in collection.games):
            upload_job = (destination, self._forced_upload_dir(destination))
            log_event(f"Everything in '{collection.name}' goes to "
                      f"{self._forced_upload_note(destination)} - no folder to pick.")
        elif destination in ("ftp", "hdd"):
            count = len(collection.games)
            result_what = {"2": "the GOD conversions", "1": "the extracted XISOs"}.get(
                mode, "the downloaded files")
            if destination == "ftp":
                where = self.require_ftp_destination(
                    subtitle=(f"Collection: {collection.name}\n\n"
                              f"All {count} item{'s' if count != 1 else ''} will be downloaded, then "
                              f"{result_what} will be uploaded into the folder you pick on {self.ftp_config.host}.")
                )
            else:
                where = self.require_hdd_destination(
                    subtitle=(f"Collection: {collection.name}\n\n"
                              f"All {count} item{'s' if count != 1 else ''} will be downloaded, then "
                              f"{result_what} will be written into the folder you pick on {self.hdd_config.label}.")
                )
            if not where:
                status("Cancelled - nothing was queued.")
                return
            upload_job = (destination, where)

        queued = self._queue_entries([game.to_files() for game in collection.games],
                                     mode, upload_job)

        where_note = ""
        if upload_job:
            where_note = f", then {'uploaded to ' if upload_job[0] == 'ftp' else 'written to '}{upload_job[1]}"
        log_event(f"Queued collection '{collection.name}': {what}, mode {mode}{where_note}.")
        status(f"Queued {what} from '{collection.name}'.")
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
        header.grid_columnconfigure(3, weight=1)

        self.community_refresh_btn = ctk.CTkButton(
            header, text="Refresh", width=90,
            command=lambda: self.refresh_community_view(force=True))
        self.community_refresh_btn.grid(row=0, column=0)

        self.community_search_var = tk.StringVar()
        self.community_search_var.trace_add("write", lambda *_: self._render_community_list())
        search = ctk.CTkEntry(header, width=240, placeholder_text="Filter by name or description...",
                              textvariable=self.community_search_var)
        search.grid(row=0, column=1, padx=8)

        self._community_filter = "all"
        self._make_kind_filter(header, self._on_community_filter).grid(row=0, column=2, padx=(8, 0))

        self.community_status_lbl = ctk.CTkLabel(header, text="", text_color="gray",
                                                 font=ctk.CTkFont(size=11), anchor="w")
        self.community_status_lbl.grid(row=0, column=3, sticky="ew", padx=(12, 0))

        self.community_scroll = ScrollFrame(
            self.community_view, label_text="Collections shared by other people")
        self.community_scroll.grid(row=1, column=0, sticky="nsew")
        self.community_scroll.grid_columnconfigure(0, weight=1)

    def _on_community_filter(self, choice: str) -> None:
        self._community_filter = self._kind_from_choice(choice)
        self._render_community_list()

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
        needle = (self.community_search_var.get() or "").strip().lower()
        kind = getattr(self, "_community_filter", "all")
        visible = []
        for e in self._community_entries:
            if needle and needle not in e.name.lower() and needle not in (e.description or "").lower():
                continue
            if kind in ITEM_KINDS and not e.kind_counts()[kind]:
                continue
            visible.append(e)
        return visible

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
                               "up here for everyone running Bman Downloader.")
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
            buttons, text="Show Items", width=110,
            fg_color="transparent", border_width=1, text_color=("black", "white"),
            command=lambda i=entry.id: self.community_toggle_games(i))
        show_btn.pack(side="left")

        card._parts = {
            "name": name_lbl, "meta": meta_lbl, "description": description_lbl,
            "buttons": buttons, "actions": (download_btn, save_btn, show_btn), "toggle": show_btn,
            "busy_lbl": None, "games_frame": None, "download": download_btn,
        }
        card._shown = {"name": "", "meta": "", "description": "", "busy": False,
                       "expanded": False, "games": None, "download_text": "Download All"}
        return card

    def _update_community_card(self, card, entry: CommunityEntry) -> None:
        """Make a card show `entry` as it is now, changing only what is out of date."""
        parts, shown = card._parts, card._shown

        if entry.name != shown["name"]:
            parts["name"].configure(text=entry.name)
            shown["name"] = entry.name

        meta = describe_item_counts(entry.kind_counts())
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

        kind_filter = getattr(self, "_community_filter", "all")
        download_text = self._download_label(kind_filter)
        if download_text != shown["download_text"]:
            parts["download"].configure(text=download_text)
            shown["download_text"] = download_text

        expanded = entry.id in self._community_expanded
        if expanded != shown["expanded"]:
            parts["toggle"].configure(text="Hide Items" if expanded else "Show Items")
            shown["expanded"] = expanded

        detail = self._community_details.get(entry.id) if expanded else None
        if not expanded:
            games = None
        elif detail is None:
            games = "loading"
        else:
            games = (id(detail), len(detail.games), kind_filter)
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
            ctk.CTkLabel(games_frame, text="Loading the list...", text_color="gray",
                         anchor="w").grid(row=0, column=0, sticky="w", padx=10, pady=8)
            return

        self._fill_item_sections(games_frame, detail.games, kind_filter, self.community_scroll)

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
        self._set_community_status(
            f"'{entry.name}' loaded - {describe_item_counts(collection.kind_counts())}.")
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

        what = describe_item_counts(saved.kind_counts())
        log_event(f"Saved community collection '{saved.name}' ({what}, {matched} matched the index).")
        self._set_community_status(f"Saved '{saved.name}' to your collections.")

        if ask_confirm_popup(
            self, "Saved to Your Collections",
            f"'{saved.name}' is in your Collections tab - "
            f"{what}.{index_note}{renamed}\n\n"
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
        kind = getattr(self, "_community_filter", "all")

        def queue(collection):
            picked = collection.only(kind)
            if not picked.games:
                self._set_community_status(
                    f"'{entry.name}' has no {ITEM_KIND_LABELS.get(kind, 'items').lower()} in it.")
                return
            self._run_collection_queue(picked, self._set_community_status)

        self._fetch_community_detail(entry, queue)


    def share_collection_action(self, name: str):
        collection = get_collection(name)
        if collection is None:
            return
        if not collection.games:
            show_info_popup(self, "Nothing to Share",
                            "That collection is empty - put some games, themes or profile pics in it first.")
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
        what = describe_item_counts(collection.kind_counts())
        if not ask_confirm_popup(
            self, "Update Shared Collection" if already else "Share Collection",
            (f"Update the shared copy of '{collection.name}' so everyone sees the "
             f"current {what}?"
             if already else
             f"Put '{collection.name}' ({what}) on the "
             f"Community Collections tab?\n\n"
             f"Everyone running Bman Downloader will be able to see and download it. "
             f"Shares are anonymous - the collection's name, description and item list go "
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

        self.setup_archive_auth_section()

        self.setup_ftp_section()

        self.setup_hdd_section()

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
            text="Download the next game while the current one extracts, converts or uploads",
            variable=self.download_ahead_var, checkbox_width=18, checkbox_height=18,
            command=self.on_download_ahead_toggle,
        )
        self.download_ahead_check.pack(anchor="w")

        self.delete_after_upload_var = ctk.BooleanVar(value=delete_after_upload_enabled())
        self.delete_after_upload_check = ctk.CTkCheckBox(
            self.settings_view,
            text=("Delete the downloaded files after they upload to FTP / HDD "
                  "(the download and its extracted folder)"),
            variable=self.delete_after_upload_var, checkbox_width=18, checkbox_height=18,
            command=self.on_delete_after_upload_toggle,
        )
        self.delete_after_upload_check.pack(anchor="w", pady=(6, 0))
        
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

        self.refresh_status_lbl = ctk.CTkLabel(self.settings_view, text=f"{len(self.index.entries)} files in library.", text_color="gray")
        self.refresh_status_lbl.pack(anchor="w", pady=(5, 0))
        
        self.update_section_lbl = ctk.CTkLabel(self.settings_view, text="Updates",
                                               font=ctk.CTkFont(size=18, weight="bold"))
        self.update_section_lbl.pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=(f"You are running version {APP_VERSION}."),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        update_row = ctk.CTkFrame(self.settings_view, fg_color="transparent")
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
        self.archive_support_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                               wraplength=600, justify="left")
        self.archive_support_lbl.pack(anchor="w", pady=(0, 15))
        try:
            problems = [f".{kind}: {problem}" for kind, problem in archive_support().items() if problem]
        except Exception as exc:
            problems = [str(exc)]
        if problems:
            self.archive_support_lbl.configure(text="\n".join(problems), text_color="orange")
        else:
            self.archive_support_lbl.configure(text="zip, 7z and rar support: ready", text_color="green")

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

    def setup_ftp_section(self):
        ctk.CTkLabel(self.settings_view, text="FTP Upload (Xbox / Console)",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view,
            text=("Aurora's FTP does not work, use XeXmenu"),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        ftp_grid = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        ftp_grid.pack(anchor="w", fill="x")

        cfg = self.ftp_config

        ctk.CTkLabel(ftp_grid, text="Console IP / host:", anchor="w", width=140).grid(
            row=0, column=0, sticky="w", pady=4)
        self.ftp_host_entry = ctk.CTkEntry(ftp_grid, width=200, placeholder_text="192.168.1.50")
        self.ftp_host_entry.grid(row=0, column=1, sticky="w", pady=4)
        self.ftp_host_entry.insert(0, cfg.host)

        ctk.CTkLabel(ftp_grid, text="Port:", anchor="w", width=60).grid(
            row=0, column=2, sticky="w", padx=(15, 0), pady=4)
        self.ftp_port_entry = ctk.CTkEntry(ftp_grid, width=80)
        self.ftp_port_entry.grid(row=0, column=3, sticky="w", pady=4)
        self.ftp_port_entry.insert(0, str(cfg.port))

        ctk.CTkLabel(ftp_grid, text="Username:", anchor="w", width=140).grid(
            row=1, column=0, sticky="w", pady=4)
        self.ftp_user_entry = ctk.CTkEntry(ftp_grid, width=200)
        self.ftp_user_entry.grid(row=1, column=1, sticky="w", pady=4)
        self.ftp_user_entry.insert(0, cfg.user)

        ctk.CTkLabel(ftp_grid, text="Timeout:", anchor="w", width=60).grid(
            row=1, column=2, sticky="w", padx=(15, 0), pady=4)
        self.ftp_timeout_entry = ctk.CTkEntry(ftp_grid, width=80)
        self.ftp_timeout_entry.grid(row=1, column=3, sticky="w", pady=4)
        self.ftp_timeout_entry.insert(0, str(cfg.timeout))

        ctk.CTkLabel(ftp_grid, text="Password:", anchor="w", width=140).grid(
            row=2, column=0, sticky="w", pady=4)
        self.ftp_pass_entry = ctk.CTkEntry(ftp_grid, width=200, show="*")
        self.ftp_pass_entry.grid(row=2, column=1, sticky="w", pady=4)
        self.ftp_pass_entry.insert(0, cfg.password)

        ctk.CTkLabel(ftp_grid, text="Default folder:", anchor="w", width=140).grid(
            row=3, column=0, sticky="w", pady=4)
        self.ftp_path_entry = ctk.CTkEntry(ftp_grid, width=200, placeholder_text="/Hdd1/Games")
        self.ftp_path_entry.grid(row=3, column=1, sticky="w", pady=4)
        self.ftp_path_entry.insert(0, cfg.default_path)

        self.ftp_browse_btn = ctk.CTkButton(ftp_grid, text="Browse...", width=90,
                                            command=self.browse_default_ftp_path)
        self.ftp_browse_btn.grid(row=3, column=2, columnspan=2, sticky="w", padx=(15, 0), pady=4)

        ctk.CTkLabel(ftp_grid, text="Files at once:", anchor="w", width=140).grid(
            row=4, column=0, sticky="w", pady=(10, 4))

        self.ftp_parallel_slider = ctk.CTkSlider(
            ftp_grid, from_=1, to=MAX_FTP_PARALLEL, number_of_steps=MAX_FTP_PARALLEL - 1,
            width=200, command=self._on_ftp_parallel_slider,
        )
        self.ftp_parallel_slider.grid(row=4, column=1, sticky="w", pady=(10, 4))
        self.ftp_parallel_slider.set(cfg.worker_count())

        self.ftp_parallel_lbl = ctk.CTkLabel(ftp_grid, text="", anchor="w", width=140)
        self.ftp_parallel_lbl.grid(row=4, column=2, columnspan=2, sticky="w", padx=(15, 0), pady=(10, 4))
        self._update_ftp_parallel_label(cfg.worker_count())

        ctk.CTkLabel(
            self.settings_view,
            text=(""),
            text_color="gray", font=ctk.CTkFont(size=11), wraplength=600, justify="left",
        ).pack(anchor="w", pady=(4, 8))

        ftp_buttons = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        ftp_buttons.pack(anchor="w", fill="x")

        self.ftp_save_btn = ctk.CTkButton(ftp_buttons, text="Save FTP Settings",
                                          command=self.save_ftp_action)
        self.ftp_save_btn.pack(side="left")

        self.ftp_test_btn = ctk.CTkButton(ftp_buttons, text="Test Connection", fg_color="#2b8a3e",
                                          hover_color="#236c31", command=self.test_ftp_action)
        self.ftp_test_btn.pack(side="left", padx=(10, 0))

        self.ftp_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                           wraplength=600, justify="left")
        self.ftp_status_lbl.pack(anchor="w", pady=(6, 0))

        if not cfg.is_configured():
            self.ftp_status_lbl.configure(text="Not set up yet - add your console's IP address above.")

    def _update_ftp_parallel_label(self, value: int):
        count = max(1, min(MAX_FTP_PARALLEL, int(round(value))))
        if count == 1:
            text = "1 file at a time"
        else:
            text = f"{count} files at once"
        if count == DEFAULT_FTP_PARALLEL:
            text += " (default)"
        try:
            self.ftp_parallel_lbl.configure(text=text)
        except Exception:
            pass

    def _on_ftp_parallel_slider(self, raw_value):
        count = max(1, min(MAX_FTP_PARALLEL, int(round(float(raw_value)))))
        try:
            if abs(float(self.ftp_parallel_slider.get()) - count) > 0.01:
                self.ftp_parallel_slider.set(count)
        except Exception:
            pass
        self._update_ftp_parallel_label(count)
        self.ftp_config.max_parallel = count
        save_ftp_config(self.ftp_config)

    def _read_ftp_form(self) -> FtpConfig:
        def _int(entry, fallback):
            try:
                return int((entry.get() or "").strip())
            except Exception:
                return fallback
        return FtpConfig(
            host=(self.ftp_host_entry.get() or "").strip(),
            port=_int(self.ftp_port_entry, DEFAULT_FTP_PORT),
            user=(self.ftp_user_entry.get() or "").strip(),
            password=self.ftp_pass_entry.get() or "",
            timeout=_int(self.ftp_timeout_entry, DEFAULT_FTP_TIMEOUT),
            default_path=ftp_normalize_path(self.ftp_path_entry.get() or "/"),
            max_parallel=self._read_ftp_parallel(),
        )

    def _read_ftp_parallel(self) -> int:
        try:
            return max(1, min(MAX_FTP_PARALLEL, int(round(float(self.ftp_parallel_slider.get())))))
        except Exception:
            return DEFAULT_FTP_PARALLEL

    def save_ftp_action(self):
        self.ftp_config = self._read_ftp_form()
        save_ftp_config(self.ftp_config)
        self._set_ftp_status("FTP settings saved. Testing the connection...")
        self._start_ftp_check(manual=True)

    def test_ftp_action(self):
        self.ftp_config = self._read_ftp_form()
        save_ftp_config(self.ftp_config)
        self._start_ftp_check(manual=True)

    def browse_default_ftp_path(self):
        self.ftp_config = self._read_ftp_form()
        save_ftp_config(self.ftp_config)
        if not self.ftp_config.is_configured():
            self._set_ftp_status("Enter the console's IP address first.", error=True)
            return
        chosen = prompt_ftp_destination(
            self, self.ftp_config,
            subtitle="Pick the folder the upload picker should open on by default.",
        )
        if chosen:
            self.ftp_path_entry.delete(0, "end")
            self.ftp_path_entry.insert(0, chosen)
            self._set_ftp_status(f"Default upload folder set to {chosen}")
            if not self.ftp_online:
                self._set_ftp_online(True)


    def setup_hdd_section(self):
        ctk.CTkLabel(self.settings_view, text="Hard Drive (direct)",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(25, 5))
        ctk.CTkLabel(
            self.settings_view, justify="left", anchor="w", text_color="gray",
            wraplength=720, font=ctk.CTkFont(size=11),
            text=("Connect your hard drive via sata or usb and transfer directly to the drive. "
                  "Xbox 360 (FATX) mode also works with devkit drives, memory units and USB "
                  "sticks the console has set up as storage."),
        ).pack(anchor="w", pady=(0, 10))

        cfg = self.hdd_config
        self._hdd_drive_choices = []
        self._hdd_part_choices = []
        self._hdd_mode = cfg.mode if cfg.mode in (HDD_MODE_FATX, HDD_MODE_NORMAL) else HDD_MODE_FATX

        mode_row = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        mode_row.pack(anchor="w", fill="x", pady=(0, 8))
        ctk.CTkLabel(mode_row, text="Drive type:", anchor="w", width=140).pack(side="left")
        self.hdd_mode_btn = ctk.CTkSegmentedButton(
            mode_row, values=["Xbox 360 drive (FATX)", "Normal drive"],
            command=self._on_hdd_mode_changed)
        self.hdd_mode_btn.set("Normal drive" if self._hdd_mode == HDD_MODE_NORMAL
                              else "Xbox 360 drive (FATX)")
        self.hdd_mode_btn.pack(side="left")

        hdd_grid = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        hdd_grid.pack(anchor="w", fill="x")

        ctk.CTkLabel(hdd_grid, text="Hard drive:", anchor="w", width=140).grid(
            row=0, column=0, sticky="w", pady=4)
        self.hdd_drive_menu = ctk.CTkOptionMenu(
            hdd_grid, values=[cfg.device_label or "(click Refresh)"], width=430,
            command=self._on_hdd_drive_selected)
        self.hdd_drive_menu.grid(row=0, column=1, sticky="w", pady=4)
        self.hdd_refresh_btn = ctk.CTkButton(hdd_grid, text="Refresh", width=90,
                                             command=self.refresh_hdd_drives_action)
        self.hdd_refresh_btn.grid(row=0, column=2, sticky="w", padx=(15, 0), pady=4)

        ctk.CTkLabel(hdd_grid, text="Partition:", anchor="w", width=140).grid(
            row=1, column=0, sticky="w", pady=4)
        self.hdd_part_menu = ctk.CTkOptionMenu(
            hdd_grid, values=[cfg.partition_name or "(scan the drive)"], width=430)
        self.hdd_part_menu.grid(row=1, column=1, sticky="w", pady=4)
        self.hdd_scan_btn = ctk.CTkButton(hdd_grid, text="Scan", width=90,
                                          command=self.scan_hdd_partitions_action)
        self.hdd_scan_btn.grid(row=1, column=2, sticky="w", padx=(15, 0), pady=4)

        ctk.CTkLabel(hdd_grid, text="Default folder:", anchor="w", width=140).grid(
            row=2, column=0, sticky="w", pady=4)
        self.hdd_path_entry = ctk.CTkEntry(hdd_grid, width=430,
                                           placeholder_text="/Content/0000000000000000")
        self.hdd_path_entry.grid(row=2, column=1, sticky="w", pady=4)
        self.hdd_path_entry.insert(0, cfg.default_path or "/")
        self.hdd_browse_btn = ctk.CTkButton(hdd_grid, text="Browse...", width=90,
                                            command=self.browse_default_hdd_path)
        self.hdd_browse_btn.grid(row=2, column=2, sticky="w", padx=(15, 0), pady=4)

        ctk.CTkLabel(hdd_grid, text="Files at once:", anchor="w", width=140).grid(
            row=3, column=0, sticky="w", pady=(10, 4))
        hdd_parallel_row = ctk.CTkFrame(hdd_grid, fg_color="transparent")
        hdd_parallel_row.grid(row=3, column=1, columnspan=2, sticky="w", pady=(10, 4))
        self.hdd_parallel_slider = ctk.CTkSlider(
            hdd_parallel_row, from_=1, to=MAX_HDD_PARALLEL, number_of_steps=MAX_HDD_PARALLEL - 1,
            width=200, command=self._on_hdd_parallel_slider,
        )
        self.hdd_parallel_slider.pack(side="left")
        self.hdd_parallel_slider.set(cfg.worker_count())
        self.hdd_parallel_lbl = ctk.CTkLabel(hdd_parallel_row, text="", anchor="w")
        self.hdd_parallel_lbl.pack(side="left", padx=(15, 0))
        self._update_hdd_parallel_label(cfg.worker_count())

        hdd_buttons = ctk.CTkFrame(self.settings_view, fg_color="transparent")
        hdd_buttons.pack(anchor="w", fill="x", pady=(10, 0))

        self.hdd_save_btn = ctk.CTkButton(hdd_buttons, text="Save HDD Settings",
                                          command=self.save_hdd_action)
        self.hdd_save_btn.pack(side="left")

        self.hdd_test_btn = ctk.CTkButton(hdd_buttons, text="Test Drive", fg_color="#2b8a3e",
                                          hover_color="#236c31", command=self.test_hdd_action)
        self.hdd_test_btn.pack(side="left", padx=(10, 0))

        self.hdd_status_lbl = ctk.CTkLabel(self.settings_view, text="", text_color="gray",
                                           anchor="w", justify="left", wraplength=720)
        self.hdd_status_lbl.pack(anchor="w", pady=(6, 0))

        if fatx360 is None:
            self._set_hdd_status(f"FATX support unavailable: {FATX_IMPORT_ERROR}", error=True)
        elif not cfg.is_configured():
            self._set_hdd_status("Not set up yet - press Refresh, pick a drive, then Scan.")
        else:
            self._set_hdd_status(f"Saved: {cfg.label}{cfg.default_path}")

    def _update_hdd_parallel_label(self, value):
        count = max(1, min(MAX_HDD_PARALLEL, int(round(float(value)))))
        text = "1 file at a time" if count == 1 else f"{count} files at once"
        if count == DEFAULT_HDD_PARALLEL:
            text += " (default)"
        try:
            self.hdd_parallel_lbl.configure(text=text)
        except Exception:
            pass

    def _read_hdd_parallel(self) -> int:
        try:
            return max(1, min(MAX_HDD_PARALLEL, int(round(float(self.hdd_parallel_slider.get())))))
        except Exception:
            return self.hdd_config.worker_count()

    def _on_hdd_parallel_slider(self, raw_value):
        count = max(1, min(MAX_HDD_PARALLEL, int(round(float(raw_value)))))
        try:
            if abs(float(self.hdd_parallel_slider.get()) - count) > 0.01:
                self.hdd_parallel_slider.set(count)
        except Exception:
            pass
        self._update_hdd_parallel_label(count)
        self.hdd_config.max_parallel = count
        save_hdd_config(self.hdd_config)

    def _on_hdd_mode_changed(self, choice: str):
        self._hdd_mode = HDD_MODE_NORMAL if choice == "Normal drive" else HDD_MODE_FATX
        self._hdd_part_choices = []
        try:
            self.hdd_part_menu.configure(values=["(scanning...)"])
            self.hdd_part_menu.set("(scanning...)")
        except Exception:
            pass
        if self._selected_hdd_drive() is not None:
            self.scan_hdd_partitions_action()
        else:
            self._set_hdd_status("Press Refresh and pick a drive, then Scan.")

    def refresh_hdd_drives_action(self):
        if fatx360 is None:
            self._set_hdd_status(f"FATX support unavailable: {FATX_IMPORT_ERROR}", error=True)
            return
        self.hdd_refresh_btn.configure(state="disabled")
        self._set_hdd_status("Looking for drives...")

        fatx_mode = self._hdd_mode == HDD_MODE_FATX

        def worker():
            try:
                drives = fatx360.list_physical_drives()
                if fatx_mode:
                    # USB sticks the console set up as storage are not disks of
                    # their own - they are files on an ordinary drive letter.
                    try:
                        drives = drives + fatx360.list_usb_storage()
                    except Exception as exc:
                        log_event(f"HDD: could not look for Xbox 360 USB storage ({exc}).")
            except Exception as exc:
                post_ui(lambda e=exc: self._set_hdd_status(f"Drive scan failed: {e}", error=True))
                post_ui(lambda: self.hdd_refresh_btn.configure(state="normal"))
                return

            def apply():
                self.hdd_refresh_btn.configure(state="normal")
                self._hdd_drive_choices = drives
                labels = [d.label for d in drives] or ["(no drives found)"]
                self.hdd_drive_menu.configure(values=labels)
                keep = next((d.label for d in drives if d.device_id == self.hdd_config.device_id), None)
                self.hdd_drive_menu.set(keep or labels[0])
                if not drives:
                    self._set_hdd_status("No drives found.", error=True)
                    return
                selected = self._selected_hdd_drive()
                if (self._hdd_mode == HDD_MODE_FATX and not fatx360.is_admin()
                        and getattr(selected, "needs_admin", True)):
                    self._set_hdd_status(
                        f"Found {len(drives)} drive(s). Note: the app is not running as "
                        "Administrator, so opening a drive for the Xbox 360 (FATX) mode will "
                        "be denied. Normal drive mode does not need this.", error=True)
                    return
                self._set_hdd_status(f"Found {len(drives)} drive(s). Scanning the selected one...")
                self._on_hdd_drive_selected()
            post_ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    def _selected_hdd_drive(self):
        choice = self.hdd_drive_menu.get()
        for drive in getattr(self, "_hdd_drive_choices", []):
            if drive.label == choice:
                return drive
        return None

    def _on_hdd_drive_selected(self, _choice=None):
        self._hdd_part_choices = []
        try:
            self.hdd_part_menu.configure(values=["(scanning...)"])
            self.hdd_part_menu.set("(scanning...)")
        except Exception:
            pass
        self.scan_hdd_partitions_action()

    def scan_hdd_partitions_action(self):
        drive = self._selected_hdd_drive()
        if drive is None:
            self._set_hdd_status("Press Refresh and pick a drive first.", error=True)
            return
        if self._hdd_mode == HDD_MODE_NORMAL:
            self._scan_normal_partitions(drive)
        else:
            self._scan_fatx_partitions(drive)

    def _scan_normal_partitions(self, drive):
        self.hdd_scan_btn.configure(state="disabled")
        self._set_hdd_status(f"Looking for normal partitions on {drive.device_id}...")

        def worker():
            try:
                parts = list_normal_partitions(drive.device_id)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                post_ui(lambda m=message: self._set_hdd_status(f"Could not read the drive: {m}",
                                                                     error=True))
                post_ui(lambda: self.hdd_scan_btn.configure(state="normal"))
                return

            def apply():
                self.hdd_scan_btn.configure(state="normal")
                self._hdd_part_choices = parts
                if not parts:
                    self.hdd_part_menu.configure(values=["(no normal partitions found)"])
                    self.hdd_part_menu.set("(no normal partitions found)")
                    self._set_hdd_status(
                        "No mounted, already-formatted partitions found on that drive. If it "
                        "is a bare Xbox 360 drive it has no drive letter Windows can see -- "
                        "use \"Xbox 360 drive (FATX)\" mode instead.", error=True)
                    return
                labels = [p.label for p in parts]
                self.hdd_part_menu.configure(values=labels)
                biggest = max(parts, key=lambda p: p.size)
                self.hdd_part_menu.set(biggest.label)
                self._set_hdd_status(
                    f"{len(parts)} partition(s) found - selected {biggest.name or biggest.letter} "
                    f"({format_size(biggest.size)}), the largest.")
            post_ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_fatx_partitions(self, drive):
        if fatx360 is None:
            self._set_hdd_status(f"FATX support unavailable: {FATX_IMPORT_ERROR}", error=True)
            return

        self.hdd_scan_btn.configure(state="disabled")
        self._set_hdd_status(f"Reading {drive.device_id}...")

        def worker():
            dev = None
            try:
                with HDD_LOCK:
                    dev = fatx360.open_disk(drive.device_id, writable=False)
                    security = fatx360.read_security_sector(dev)
                    parts = fatx360.probe_partitions(dev)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                if "winerror 5" in message.lower():
                    message += "\nRun the app as Administrator."
                post_ui(lambda m=message: self._set_hdd_status(f"Could not read the drive: {m}",
                                                                     error=True))
                post_ui(lambda: self.hdd_scan_btn.configure(state="normal"))
                return
            finally:
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass

            def apply():
                self.hdd_scan_btn.configure(state="normal")
                self._hdd_part_choices = parts
                if not parts:
                    self.hdd_part_menu.configure(values=["(no FATX partitions found)"])
                    self.hdd_part_menu.set("(no FATX partitions found)")
                    self._set_hdd_status(
                        "No FATX partitions on that drive. Check it is the 360's own hard "
                        "drive rather than a blank replacement.", error=True)
                    return
                labels = [p.label for p in parts]
                self.hdd_part_menu.configure(values=labels)
                biggest = max(parts, key=lambda p: p.size)
                self.hdd_part_menu.set(biggest.label)
                if getattr(drive, "kind", "physical") == "usb":
                    note = "Xbox 360 USB storage."
                elif any(p.name.startswith("Devkit") for p in parts):
                    note = "Devkit partition table found."
                elif any(p.name.startswith("Memory Unit") for p in parts):
                    note = "Memory unit layout found."
                elif security.looks_valid:
                    note = security.describe()
                else:
                    note = "No 360 security sector found (still fine if the partitions read)."
                self._set_hdd_status(
                    f"{len(parts)} partition(s) found - selected {biggest.name} "
                    f"({format_size(biggest.size)}), the largest. {note}")
            post_ui(apply)

        threading.Thread(target=worker, daemon=True).start()

    def _read_hdd_form(self) -> HddConfig:
        mode = getattr(self, "_hdd_mode", HDD_MODE_FATX)
        cfg = HddConfig(default_path=hdd_normalize_path(self.hdd_path_entry.get() or "/"),
                        mode=mode, max_parallel=self._read_hdd_parallel())

        drive = self._selected_hdd_drive()
        if drive is not None:
            cfg.device_id = drive.device_id
            cfg.device_label = drive.label
        else:
            cfg.device_id = self.hdd_config.device_id
            cfg.device_label = self.hdd_config.device_label

        choice = self.hdd_part_menu.get()
        part = next((p for p in getattr(self, "_hdd_part_choices", []) if p.label == choice), None)

        if mode == HDD_MODE_NORMAL:
            if isinstance(part, NormalPartition):
                cfg.volume_path = f"{part.letter}\\"
                cfg.partition_name = part.name or f"Local Disk ({part.letter})"
                cfg.partition_size = part.size
            elif cfg.device_id == self.hdd_config.device_id and self.hdd_config.mode == HDD_MODE_NORMAL:
                cfg.volume_path = self.hdd_config.volume_path
                cfg.partition_name = self.hdd_config.partition_name
                cfg.partition_size = self.hdd_config.partition_size
            else:
                cfg.volume_path = ""
                cfg.partition_name = ""
                cfg.partition_size = 0
            cfg.partition_offset = -1
            return cfg

        if part is not None and hasattr(part, "offset"):
            cfg.partition_offset = part.offset
            cfg.partition_name = part.name
            cfg.partition_size = part.size
        elif cfg.device_id == self.hdd_config.device_id and self.hdd_config.mode == HDD_MODE_FATX:
            cfg.partition_offset = self.hdd_config.partition_offset
            cfg.partition_name = self.hdd_config.partition_name
            cfg.partition_size = self.hdd_config.partition_size
        else:
            cfg.partition_offset = -1
            cfg.partition_name = ""
            cfg.partition_size = 0
        return cfg

    def save_hdd_action(self):
        self.hdd_config = self._read_hdd_form()
        save_hdd_config(self.hdd_config)
        self._set_hdd_status("HDD settings saved. Checking the drive...")
        self._start_hdd_check(manual=True)

    def test_hdd_action(self):
        self.hdd_config = self._read_hdd_form()
        save_hdd_config(self.hdd_config)
        self._start_hdd_check(manual=True)

    def browse_default_hdd_path(self):
        self.hdd_config = self._read_hdd_form()
        save_hdd_config(self.hdd_config)
        if not self.hdd_config.is_configured():
            self._set_hdd_status("Pick a drive and a partition first.", error=True)
            return
        chosen = prompt_hdd_destination(
            self, self.hdd_config,
            subtitle="Pick the folder the upload picker should open on by default.",
        )
        if chosen:
            self.hdd_path_entry.delete(0, "end")
            self.hdd_path_entry.insert(0, chosen)
            self._set_hdd_status(f"Default upload folder set to {chosen}")
            if not self.hdd_online:
                self._set_hdd_online(True)

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

    def on_delete_after_upload_toggle(self):
        on = bool(self.delete_after_upload_var.get())
        data = load_settings()
        data[DELETE_AFTER_UPLOAD_KEY] = on
        save_settings(data)
        log_event("Delete after upload: " + (
            "ON - games are deleted from this PC once they upload successfully." if on else "OFF."))

    def on_download_ahead_toggle(self):
        on = bool(self.download_ahead_var.get())
        data = load_settings()
        data[DOWNLOAD_AHEAD_KEY] = on
        save_settings(data)
        self._set_download_ahead(on)

        if on:
            note = ("Saved - the next queued game now downloads while the current one is "
                    "extracting, converting or uploading.")
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

        data[DELETE_AFTER_UPLOAD_KEY] = False
        self.delete_after_upload_var.set(False)

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
        self.refresh_index_btn.configure(state="disabled")
        self.refresh_status_lbl.configure(text="Refreshing from archive.org...", text_color="gray")

        def work():
            try:
                new_index = Index.load_combined(use_cache=False)
            except Exception as exc:
                post_ui(lambda e=exc: self._on_manual_index_refreshed(None, str(e)))
                return
            post_ui(lambda i=new_index: self._on_manual_index_refreshed(i, None))

        threading.Thread(target=work, daemon=True).start()

    # Not named _on_index_refreshed: that name belongs to the background refresh
    # (see _refresh_index_if_needed), and a second method with the same name in this
    # class would replace it, leaving the "Checking archive.org..." banner stuck on.
    def _on_manual_index_refreshed(self, new_index: Index | None, error: str | None):
        self.refresh_index_btn.configure(state="normal")
        if new_index is None:
            self.refresh_status_lbl.configure(text=f"Refresh failed: {error}", text_color="#d97070")
            return

        self.index = new_index
        self._show_index_status()               # the banner describes self.index, which just changed
        if not self.is_downloading:
            try:
                self.downloader = Downloader(self.index)
            except Exception:
                pass
        self.refresh_status_lbl.configure(text=f"Library refreshed - {len(new_index.entries)} files.", text_color="green")

    def save_creds_action(self):
        try:
            save_archive_credentials(self.user_entry.get().strip(), self.sig_entry.get().strip())
        except AttributeError:
            return
        self._refresh_archive_credentials()


class DownloadItemFrame(ctk.CTkFrame):
    def __init__(self, master, filename: str, entry: Files | None = None, cancel_callback: threading.Event | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.filename = filename
        self.entry = entry
        self.cancel_event = cancel_callback
        self.is_active = cancel_callback is not None

        display_name = filename
        self.raw_filename = filename
        if filename.endswith(" (Extracted Only)"):
            self.raw_filename = filename.replace(" (Extracted Only)", "")

        self.name_lbl = ctk.CTkLabel(self, text=display_name, font=ctk.CTkFont(weight="bold"))
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
        self.del_archive_btn = ctk.CTkButton(self.btn_frame, text="Delete Archive", width=90, height=24, fg_color="#b53737", hover_color="#8c2929", command=self.delete_archive_file)
        self.del_extracted_btn = ctk.CTkButton(self.btn_frame, text="Delete Extracted", width=90, height=24, fg_color="#b53737", hover_color="#8c2929", command=self.delete_extracted_files)
        
        self.extract_zip_btn = ctk.CTkButton(self.btn_frame, text="Extract Zip", width=90, height=24, fg_color=accent_fg(), hover_color=accent_hover(), command=self.action_extract_zip)
        self.convert_god_btn = ctk.CTkButton(self.btn_frame, text="Convert to GOD", width=100, height=24, fg_color="#2b8a3e", hover_color="#236c31", command=self.action_convert_god)
        self.extract_xiso_btn = ctk.CTkButton(self.btn_frame, text="Extract XISO", width=90, height=24, fg_color="#2b8a3e", hover_color="#236c31", command=self.action_extract_xiso)
        self.ftp_upload_btn = ctk.CTkButton(self.btn_frame, text="Upload via FTP", width=100, height=24,
                                            fg_color="#7a4bbd", hover_color="#623a99",
                                            command=self.action_upload_ftp)
        self.ftp_upload_btn._accent_locked = True
        self.hdd_upload_btn = ctk.CTkButton(self.btn_frame, text="Upload to HDD", width=100, height=24,
                                            fg_color="#0f766e", hover_color="#0c5d56",
                                            command=self.action_upload_hdd)
        self.hdd_upload_btn._accent_locked = True
        self.is_uploading = False
        self.is_working = False

        if self.cancel_event:
            self.cancel_btn.pack(side="right", padx=2)

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

    def update_upload_progress(self, percent: int, headline: str, detail: str = ""):
        if not self._alive():
            return
        try:
            self.progress_bar.set(max(0.0, min(1.0, percent / 100.0)))
            self.status_lbl.configure(text=headline)
            if detail:
                self.iso_status_lbl.configure(text=detail, text_color="gray")
        except Exception:
            pass

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

        base_stem = Path(self.raw_filename).stem
        game_extract_dir = EXTRACT_DIR / base_stem
        archive_path = DOWNLOAD_DIR / self.raw_filename
        
        if not archive_path.exists():
            possible_archives = list(DOWNLOAD_DIR.glob(f"{base_stem}.*"))
            if possible_archives:
                archive_path = possible_archives[0]
                self.raw_filename = archive_path.name
                self.name_lbl.configure(text=self.raw_filename)

        iso_found = False
        god_found = False
        content_names: list[str] = []
        is_content_entry = entry_is_content(self.entry)

        if game_extract_dir.exists():
            iso_found = _has_iso(game_extract_dir)
            god_output_dir = game_extract_dir / "GOD_Output"
            if god_output_dir.exists() and any(god_output_dir.iterdir()):
                god_found = True

            looks_extracted_xiso = any(
                (game_extract_dir / marker).exists() for marker in ("default.xex", "default.xbe"))
            if not iso_found and not god_found and (is_content_entry or not looks_extracted_xiso):
                try:
                    _root, content_dirs = find_content_upload_roots(
                        game_extract_dir, max_depth=content_scan_depth(is_content_entry))
                    content_names = [p.name for p in content_dirs]
                except Exception:
                    content_names = []

        status_texts = []
        if iso_found:
            status_texts.append("ISO Found")
        if god_found:
            status_texts.append("GOD Format Found")
        if content_names:
            shown = ", ".join(content_names[:3])
            if len(content_names) > 3:
                shown += f" +{len(content_names) - 3} more"
            status_texts.append(f"Title ID: {shown}")

        row_catalog = catalog_for_entry(self.entry)
        if row_catalog is None and self.entry is None:
            row_catalog = catalog_for_entry(catalog_entry_for_filename(self.raw_filename))
        if row_catalog is not None:
            extracted_now = game_extract_dir.exists() and any(game_extract_dir.iterdir())
            self.iso_status_lbl.configure(
                text=(f"Xbox 360 {row_catalog.noun} - uploads go to {row_catalog.console_path[:-1]}"
                      if extracted_now else f"Xbox 360 {row_catalog.noun} - not extracted yet"),
                text_color="green" if extracted_now else "gray")
        elif status_texts:
            self.iso_status_lbl.configure(text=" | ".join(status_texts), text_color="green")
        elif is_content_entry:
            self.iso_status_lbl.configure(
                text="Title ID content (from this source) - not extracted yet", text_color="gray")
        else:
            self.iso_status_lbl.configure(text="No ISO/GOD detected yet", text_color="gray")

        has_archive = archive_path.exists()
        has_extracted = game_extract_dir.exists() and any(game_extract_dir.iterdir())
        is_zip_file = False
        try:
            if has_archive:
                is_zip_file = (zipfile.is_zipfile(archive_path)
                               or archive_path.suffix.lower() == ".zip"
                               or archive_path.suffix.lower() in RAR_7Z_SUFFIXES)
        except Exception:
            pass

        for btn in [self.cancel_btn, self.del_archive_btn, self.del_extracted_btn, self.extract_zip_btn, self.convert_god_btn, self.extract_xiso_btn, self.ftp_upload_btn, self.hdd_upload_btn]:
            btn.pack_forget()

        if has_extracted:
            self.del_extracted_btn.pack(side="right", padx=2)
        if has_archive:
            self.del_archive_btn.pack(side="right", padx=2)

        if self.is_active and self.cancel_event:
            self.cancel_btn.pack(side="right", padx=2)
        else:
            if has_archive and not has_extracted and is_zip_file:
                self.extract_zip_btn.pack(side="right", padx=2)

            allow_iso = True
            if self.entry and not self.entry.allow_iso_conversion:
                allow_iso = False

            if iso_found and allow_iso:
                self.convert_god_btn.pack(side="right", padx=2)
                self.extract_xiso_btn.pack(side="right", padx=2)

            if FTP_STATE.get("online") and has_extracted and not self.is_uploading:
                self.ftp_upload_btn.pack(side="right", padx=2)
            if HDD_STATE.get("online") and has_extracted and not self.is_uploading:
                self.hdd_upload_btn.pack(side="right", padx=2)

        if not has_archive and not has_extracted and not (self.is_active and self.cancel_event):
            for slot in ("dl-progress", "dl-status", "row-status", "upload-progress"):
                drop_ui_slot((slot, id(self)))
            self.destroy()

    def action_extract_zip(self):
        archive_path = DOWNLOAD_DIR / self.raw_filename
        if not archive_path.exists():
            possible = list(DOWNLOAD_DIR.glob(f"{Path(self.raw_filename).stem}.*"))
            if possible:
                archive_path = possible[0]
            else:
                return
        
        def run_extraction():
            self.is_working = True
            post_ui(lambda: self.update_status("Extracting zip/archive..."))
            try:
                def prog_cb(msg):
                    post_ui_latest(("row-status", id(self)),
                                   lambda m=msg: self.update_status(m))
                success = handle_post_download(archive_path, entry=self.entry, conversion_mode="3", progress_callback=prog_cb)
                if success:
                    post_ui(lambda: self.update_status("Extracted Successfully!"))
                else:
                    post_ui(lambda: self.update_status("Extraction Failed."))
            except Exception as e:
                post_ui(lambda e=e: self.update_status(f"Error: {e}"))
            self.is_working = False
            post_ui(self.check_iso_and_files)

        threading.Thread(target=run_extraction, daemon=True).start()

    def action_convert_god(self):
        archive_path = DOWNLOAD_DIR / self.raw_filename
        if not archive_path.exists():
            possible = list(DOWNLOAD_DIR.glob(f"{Path(self.raw_filename).stem}.*"))
            if possible:
                archive_path = possible[0]
        
        def run_conversion():
            self.is_working = True
            post_ui(lambda: self.update_status("Converting to GOD..."))
            try:
                def prog_cb(msg):
                    post_ui_latest(("row-status", id(self)),
                                   lambda m=msg: self.update_status(m))
                success = handle_post_download(archive_path, entry=self.entry, conversion_mode="2", progress_callback=prog_cb)
                if success:
                    post_ui(lambda: self.update_status("GOD Conversion Complete!"))
                else:
                    post_ui(lambda: self.update_status("GOD Conversion Failed."))
            except Exception as e:
                post_ui(lambda e=e: self.update_status(f"Error: {e}"))
            self.is_working = False
            post_ui(self.check_iso_and_files)

        threading.Thread(target=run_conversion, daemon=True).start()

    def action_extract_xiso(self):
        archive_path = DOWNLOAD_DIR / self.raw_filename
        if not archive_path.exists():
            possible = list(DOWNLOAD_DIR.glob(f"{Path(self.raw_filename).stem}.*"))
            if possible:
                archive_path = possible[0]

        def run_extraction():
            self.is_working = True
            post_ui(lambda: self.update_status("Extracting XISO..."))
            try:
                def prog_cb(msg):
                    post_ui_latest(("row-status", id(self)),
                                   lambda m=msg: self.update_status(m))
                success = handle_post_download(archive_path, entry=self.entry, conversion_mode="1", progress_callback=prog_cb)
                if success:
                    post_ui(lambda: self.update_status("XISO Extraction Complete!"))
                else:
                    post_ui(lambda: self.update_status("XISO Extraction Failed."))
            except Exception as e:
                post_ui(lambda e=e: self.update_status(f"Error: {e}"))
            self.is_working = False
            post_ui(self.check_iso_and_files)

        threading.Thread(target=run_extraction, daemon=True).start()

    def action_upload_ftp(self):
        self._action_upload("ftp")

    def action_upload_hdd(self):
        self._action_upload("hdd")

    def _action_upload(self, kind: str):
        if self.is_uploading:
            return

        base_stem = Path(self.raw_filename).stem
        game_dir = EXTRACT_DIR / base_stem
        upload_kind, upload_path, upload_sub = analyze_upload_target(
            game_dir, content_hint=entry_is_content(self.entry))

        if upload_kind in ("missing", "empty"):
            archive_path = DOWNLOAD_DIR / self.raw_filename
            try:
                if archive_path.suffix.lower() == ".iso" and archive_path.exists():
                    upload_kind = "needs_convert"
            except Exception:
                pass

        if upload_kind == "needs_convert":
            self.update_status("Convert this first")
            self.iso_status_lbl.configure(
                text=("Convert to God or XEX first"),
                text_color="#e0a458")
            return

        if upload_kind in ("missing", "empty"):
            self.update_status("Nothing to upload")
            self.iso_status_lbl.configure(
                text="Nothing extracted yet - press Extract Zip first.", text_color="#e0a458")
            return

        app = self.winfo_toplevel()
        where = "HDD" if kind == "hdd" else "FTP"
        button = self.hdd_upload_btn if kind == "hdd" else self.ftp_upload_btn
        cfg = app.hdd_config if kind == "hdd" else app.ftp_config
        remote_dir = None
        rename_to = None

        row_catalog = catalog_folder_kind(game_dir, self.entry)
        if row_catalog is not None:
            # Themes / gamer pictures always go under Content/0000000000000000/FFFE07D1: no folder to pick.
            online = HDD_STATE if kind == "hdd" else FTP_STATE
            if not online.get("online"):
                self.update_status(f"{where} is not connected")
                return
            remote_dir = cfg.default_path or "/"
            dest = catalog_upload_destination(kind, remote_dir,
                                              cfg.default_path if kind == "ftp" else "",
                                              row_catalog.content_dir)
            self.iso_status_lbl.configure(text=f"{row_catalog.noun.capitalize()} -> {dest}",
                                          text_color="gray")
        elif FORCE_TITLE_ID_TO_CONTENT and upload_kind in FORCED_CONTENT_KINDS:
            # GOD / title ID content always goes to Content/0000000000000000: no folder to pick.
            online = HDD_STATE if kind == "hdd" else FTP_STATE
            if not online.get("online"):
                self.update_status(f"{where} is not connected")
                return
            remote_dir = cfg.default_path or "/"
            dest = app._forced_upload_note(kind)
            self.iso_status_lbl.configure(
                text=f"{UPLOAD_KIND_LABELS.get(upload_kind, upload_kind)} -> {dest}",
                text_color="gray")
        else:
            try:
                what = UPLOAD_KIND_LABELS.get(upload_kind, "files")
                prompt = f"{base_stem}\n\nDetected as {what}. Pick the folder to copy it into."
                remote_dir = (app.require_hdd_destination(subtitle=prompt) if kind == "hdd"
                              else app.require_ftp_destination(subtitle=prompt))
            except Exception as exc:
                self.update_status(f"{where} error: {exc}")
                return

            if not remote_dir:
                return

            _source, folder_name = hdd_upload_folder(upload_kind, upload_path, upload_sub)
            if hdd_title_needs_rename(folder_name):
                rename_to = ask_hdd_rename(
                    app, folder_name, where="the HDD" if kind == "hdd" else "the FTP upload")
                if not rename_to:
                    return

        self.is_uploading = True
        button.pack_forget()

        def worker():
            def prog(percent, headline, detail):
                post_ui_latest(("upload-progress", id(self)),
                               lambda p=percent, h=headline, d=detail: self.update_upload_progress(p, h, d))

            if kind == "hdd":
                ok, message = upload_game_to_hdd(cfg, game_dir, remote_dir, progress_callback=prog,
                                                 entry=self.entry, rename_to=rename_to)
            else:
                ok, message = upload_game_via_ftp(cfg, game_dir, remote_dir, progress_callback=prog,
                                                  entry=self.entry, rename_to=rename_to)

            deleted = False
            if ok and delete_after_upload_enabled():
                log_event(f"{where} upload done for {base_stem} - deleting the local copy (setting is on).")
                deleted = delete_local_copies(base_stem, DOWNLOAD_DIR / self.raw_filename)

            def finish():
                self.is_uploading = False
                if ok:
                    self.update_progress(100, f"Uploaded to {where}!" + (" (deleted from PC)" if deleted else ""))
                    self.check_iso_and_files()
                else:
                    self.update_status("Upload failed")
                    try:
                        self.iso_status_lbl.configure(text=message, text_color="#d97070")
                        online = HDD_STATE if kind == "hdd" else FTP_STATE
                        if online.get("online"):
                            button.pack(side="right", padx=2)
                    except Exception:
                        pass
                    log_event(f"{where} upload failed for {base_stem}: {message}")

            post_ui(finish)

        threading.Thread(target=worker, daemon=True).start()

    def delete_archive_file(self):
        archive_path = DOWNLOAD_DIR / self.raw_filename
        try:
            if archive_path.exists():
                archive_path.unlink()
            else:
                possible = list(DOWNLOAD_DIR.glob(f"{Path(self.raw_filename).stem}.*"))
                for p in possible:
                    p.unlink()
            self.check_iso_and_files()
        except Exception as e:
            print(f"Error deleting archive: {e}")

    def delete_extracted_files(self):
        base_stem = Path(self.raw_filename).stem
        game_extract_dir = EXTRACT_DIR / base_stem
        try:
            if game_extract_dir.exists():
                shutil.rmtree(game_extract_dir)
            self.check_iso_and_files()
        except Exception as e:
            print(f"Error deleting extracted files: {e}")
