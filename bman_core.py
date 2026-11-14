"""Bman Downloader - everything that is not the window: sources, downloads, collections,
FTP / HDD transfers, conversions, settings, theming and the self-updater.

The interface lives in bman_ui.py; start the app from bman_downloader.py."""
from __future__ import annotations

import base64
import collections
import colorsys
import copy
import ftplib
import functools
import hashlib
import importlib
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict, field, fields as dataclass_fields, replace as dataclass_replace
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
import zipfile
import itertools
import shutil
import queue
import random
import contextlib
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ISO -> GOD and ISO -> loose files, done in Python (no external .exe tools);
# they must sit next to this file (PyInstaller bundles them automatically).
import xgd_to_god
import xgd_extract
import bman_archive
from bman_archive import RAR_7Z_SUFFIXES, ArchiveExtractError, archive_support
# Which xefu builds each Original Xbox game works on (ConsoleMods wiki list).
import xefu_compat_data

try:
    import fatx360
    FATX_IMPORT_ERROR = None
except Exception as _fatx_exc:
    fatx360 = None
    FATX_IMPORT_ERROR = str(_fatx_exc)

# Where the real .exe (or this .py file) lives.
# Nuitka doesn't set sys.frozen; it defines __compiled__ instead. In a Nuitka
# onefile build, __file__ and sys.executable point into the temporary unpack
# folder, while sys.argv[0] is the actual BmanDownloader.exe the user ran.
IS_NUITKA = "__compiled__" in globals()
if IS_NUITKA:
    EXE_PATH = Path(sys.argv[0]).resolve()
elif getattr(sys, "frozen", False):          # PyInstaller
    EXE_PATH = Path(sys.executable).resolve()
else:                                        # plain python
    EXE_PATH = Path(__file__).resolve()
IS_FROZEN = IS_NUITKA or bool(getattr(sys, "frozen", False))

RUN_DIR = EXE_PATH.parent

CREDS_FILE = "archive_creds.json"

INDEX_CACHE_FILE = "index_cache.json"
INDEX_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60     # archive.org game list: refreshed once a week
INDEX_CACHE_PARTIAL_TTL_SECONDS = 15 * 60
INDEX_CACHE_VERSION = 1
INDEX_FETCH_TIMEOUT = 12        # seconds per archive.org source
INDEX_FETCH_WORKERS = 6

SETTINGS_FILE = "app_settings.json"

FTP_SETTINGS_KEY = "ftp"
DEFAULT_FTP_PORT = 21
DEFAULT_FTP_USER = "xbox"
DEFAULT_FTP_PASS = "xbox"
DEFAULT_FTP_TIMEOUT = 15

FTP_BLOCK_SIZE = 1024 * 1024

DEFAULT_FTP_PARALLEL = 4
MAX_FTP_PARALLEL = 4

APP_VERSION = "1.83"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/BmanGames024/bman-downloader/refs/heads/main/version.json"

MIN_UPDATE_BYTES = 5 * 1024 * 1024

DEFAULT_MAX_SEGMENTS = 128
DEFAULT_MIN_SEGMENT_MB = 8
DEFAULT_SEGMENT_MIN_FILE_MB = 16
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF = 1.5

@dataclass(frozen=True)
class DownloadSetting:
    key: str
    attr: str
    label: str
    help_text: str
    minimum: float
    maximum: float
    step: float
    default: float
    scale: float = 1.0
    unit: str = ""
    decimals: int = 0

DOWNLOAD_SETTINGS = (
    DownloadSetting(
        key="max_segments", attr="max_segments",
        label="Parallel connections",
        help_text=("Connections opened per file, spread round-robin across every data node that "
                   "serves the item. archive.org throttles per connection AND per node, so with 2-3 "
                   "nodes in play 24-48 is usually where a gigabit line saturates. Past that you "
                   "start collecting 429s, which the retry backoff turns into slower downloads."),
        minimum=1, maximum=128, step=1, default=DEFAULT_MAX_SEGMENTS,
    ),
    DownloadSetting(
        key="min_segment_mb", attr="min_bytes_per_segment",
        label="Work chunk size",
        help_text=("The file is cut into chunks of this size and connections pull from a shared "
                   "queue, so a slow node just takes fewer chunks instead of stalling the whole "
                   "file. Smaller = smoother balancing, more requests. 4-16 MB is the sweet spot."),
        minimum=1, maximum=128, step=1, default=DEFAULT_MIN_SEGMENT_MB,
        scale=1024 * 1024, unit=" MB",
    ),
    DownloadSetting(
        key="segment_min_file_mb", attr="segment_min_file_size",
        label="Min file size to split",
        help_text=("Files smaller than this skip segmenting entirely and use one plain stream. "
                   "Splitting tiny files costs more in setup than it saves."),
        minimum=1, maximum=256, step=1, default=DEFAULT_SEGMENT_MIN_FILE_MB,
        scale=1024 * 1024, unit=" MB",
    ),
    DownloadSetting(
        key="max_retries", attr="max_retries",
        label="Retries per segment",
        help_text=("How many times a stalled or rate-limited segment is retried before the whole "
                   "download is failed. Raise it on a flaky connection."),
        minimum=1, maximum=15, step=1, default=DEFAULT_MAX_RETRIES,
    ),
    DownloadSetting(
        key="retry_backoff", attr="retry_backoff_base",
        label="Retry backoff",
        help_text=("Base seconds for the exponential wait between retries (attempt N waits "
                   "base x 2^N). Higher is gentler on archive.org and recovers better from 429s."),
        minimum=0.5, maximum=5.0, step=0.5, default=DEFAULT_RETRY_BACKOFF,
        unit="s", decimals=1,
    ),
)

ARCHIVE_SOURCES = {
    "XBOX_360_XBLA": False,
    "mxogcxpt2-xbox-ztm": False,
    "mxogcxo": False,
    "mxogcx-xbox-ztm": False,
    "XBOX_360_1": True,
    "XBOX_360_2": True,
    "XBOX_360_3": True,
    "XBOX_360_4": True,
    "XBOX_360_5": True,
    "XBOX_360_6": True,
    "xbox_360_indie_games": False,
    "XBOX_360_DLC_1": False,
    "XBOX_360_DLC_2": False,
    "XBOX_360_DLC_3": False,
    "XBOX_360_DLC_4": False,
    "XBOX_360_DLC_5": False,
    "XBOX_360_DLC_6": False,
    "XBOX_360_1_OTHER": True,
    "total-miner-forge-v-2.5": False,
    "castle-miner-z_202605": False,
    "call-of-duty-black-ops-2-dlc-5": False,
    "grand-theft-auto-v-god-format-with-dlc": False,
    "Bman-Downloader-Extras": False,
}

CONTENT_SOURCES = frozenset({
    "XBOX_360_XBLA",
    "xbox_360_indie_games",
    "XBOX_360_DLC_1",
    "XBOX_360_DLC_2",
    "XBOX_360_DLC_3",
    "XBOX_360_DLC_4",
    "XBOX_360_DLC_5",
    "XBOX_360_DLC_6",
    "castle-miner-z_202605",
    "grand-theft-auto-v-god-format-with-dlc",
    "total-miner-forge-v-2.5",
})

def is_content_source(identifier: str | None) -> bool:
    return bool(identifier) and identifier in CONTENT_SOURCES

def entry_is_content(entry) -> bool:
    if entry is None:
        return False
    flag = getattr(entry, "content_source", None)
    if flag is not None:
        return bool(flag)
    return is_content_source(getattr(entry, "archive_identifier", None))

MAIN_DIR = RUN_DIR
DOWNLOAD_DIR = MAIN_DIR / "downloads"      # your games stay next to the exe
EXTRACT_DIR = MAIN_DIR / "extracted"

# Everything the app makes for itself (settings, login, caches, logs, cover art
# and thumbnails) lives in <exe folder>\core so the exe folder stays clean.
CORE_DIR = RUN_DIR / "core"
COVER_IMAGE_DIR = CORE_DIR / "covers"
GAMERPIC_IMAGE_DIR = CORE_DIR / "gamerpics"

# Files/folders older versions wrote next to the exe; moved into core\ on start.
_LEGACY_CORE_ITEMS = (
    "archive_creds.json", "app_settings.json", "collections.json",
    "index_cache.json", "community_cache.json", "community_lists",
    "themes_cache.json", "gamerpics_cache.json", "gamerpics_site_cache.json",
    "theme_wallpapers_cache.json", "theme_wallpaper_thumbs",
    "dbox_covers_cache.json", "logs",
)


def _prepare_core_dir() -> None:
    try:
        CORE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in _LEGACY_CORE_ITEMS:
        old, new = RUN_DIR / name, CORE_DIR / name
        try:
            if not old.exists():
                continue
            if not new.exists():
                shutil.move(str(old), str(new))
            elif old.is_dir() and new.is_dir():
                for child in old.iterdir():              # e.g. logs: keep both sets
                    if not (new / child.name).exists():
                        shutil.move(str(child), str(new / child.name))
                shutil.rmtree(old, ignore_errors=True)
            # a file already in core\ is newer - leave the old one alone
        except OSError:
            pass


_prepare_core_dir()

EXCLUDED_EXTENSIONS = (".xml", ".sqlite", ".torrent")

def is_excluded_file(name: str) -> bool:
    return name.lower().endswith(EXCLUDED_EXTENSIONS)

def is_dlc_entry(entry) -> bool:
    identifier = (getattr(entry, "archive_identifier", None) or "").lower()
    if "dlc" in identifier:
        return True
    return "dlc" in (getattr(entry, "name", "") or "").lower()

REGION_WORLD = "world"
REGION_USA = "usa"
REGION_EUROPE = "europe"
REGION_JAPAN = "japan"

_BRACKET_RE = re.compile(r"[\(\[]([^)\]]*)[\)\]]")
_REGION_TOKEN_SPLIT_RE = re.compile(r"[\s,;/&+._\-]+")
_SCENE_TOKEN_SPLIT_RE = re.compile(r"[._\-]+")

_REGION_TEXT_FIXUPS = (
    ("ntsc-j", "ntscj"), ("ntsc j", "ntscj"),
    ("ntsc-u", "ntscu"), ("ntsc u", "ntscu"),
    ("region free", "regionfree"), ("region-free", "regionfree"),
    ("region_free", "regionfree"),
    ("north america", "usa"),
)

_SINGLE_LETTER_REGIONS = {
    "u": REGION_USA,
    "e": REGION_EUROPE,
    "j": REGION_JAPAN,
    "w": REGION_WORLD,
}

_REGION_WORDS = {
    "world": REGION_WORLD, "regionfree": REGION_WORLD, "rf": REGION_WORLD,
    "global": REGION_WORLD, "international": REGION_WORLD, "intl": REGION_WORLD,
    "worldwide": REGION_WORLD, "multiregion": REGION_WORLD,
    "usa": REGION_USA, "us": REGION_USA, "ntscu": REGION_USA, "ntsc": REGION_USA,
    "america": REGION_USA, "american": REGION_USA, "canada": REGION_USA,
    "mexico": REGION_USA, "brazil": REGION_USA, "latin": REGION_USA,
    "europe": REGION_EUROPE, "european": REGION_EUROPE, "eur": REGION_EUROPE,
    "eu": REGION_EUROPE, "pal": REGION_EUROPE, "uk": REGION_EUROPE,
    "england": REGION_EUROPE, "britain": REGION_EUROPE, "germany": REGION_EUROPE,
    "german": REGION_EUROPE, "france": REGION_EUROPE, "french": REGION_EUROPE,
    "spain": REGION_EUROPE, "spanish": REGION_EUROPE, "italy": REGION_EUROPE,
    "italian": REGION_EUROPE, "netherlands": REGION_EUROPE, "sweden": REGION_EUROPE,
    "scandinavia": REGION_EUROPE, "russia": REGION_EUROPE, "australia": REGION_EUROPE,
    "aus": REGION_EUROPE, "australian": REGION_EUROPE,
    "japan": REGION_JAPAN, "japanese": REGION_JAPAN, "jpn": REGION_JAPAN,
    "jap": REGION_JAPAN, "ntscj": REGION_JAPAN, "korea": REGION_JAPAN,
    "korean": REGION_JAPAN, "kor": REGION_JAPAN, "asia": REGION_JAPAN,
    "china": REGION_JAPAN, "chinese": REGION_JAPAN, "taiwan": REGION_JAPAN,
    "hongkong": REGION_JAPAN,
}

_SCENE_REGION_TAGS = {
    "pal": REGION_EUROPE, "eur": REGION_EUROPE, "europe": REGION_EUROPE,
    "usa": REGION_USA, "ntscu": REGION_USA, "ntsc": REGION_USA,
    "japan": REGION_JAPAN, "jpn": REGION_JAPAN, "ntscj": REGION_JAPAN,
    "regionfree": REGION_WORLD, "rf": REGION_WORLD,
}

def _normalize_region_text(text: str) -> str:
    lowered = (text or "").lower()
    for old, new in _REGION_TEXT_FIXUPS:
        if old in lowered:
            lowered = lowered.replace(old, new)
    return lowered

def detect_regions(name: str) -> set[str]:
    lowered = _normalize_region_text(name)
    found: set[str] = set()

    for group in _BRACKET_RE.findall(lowered):
        text = group.strip()
        if not text:
            continue
        if text in _SINGLE_LETTER_REGIONS:
            found.add(_SINGLE_LETTER_REGIONS[text])
            continue
        pieces = [p for p in _REGION_TOKEN_SPLIT_RE.split(text) if p]
        if not pieces:
            continue
        joined = "".join(pieces)
        if joined in _REGION_WORDS:
            found.add(_REGION_WORDS[joined])
            continue
        for piece in pieces:
            region = _REGION_WORDS.get(piece)
            if region:
                found.add(region)

    if not found and " " not in lowered:
        for token in _SCENE_TOKEN_SPLIT_RE.split(lowered):
            region = _SCENE_REGION_TAGS.get(token)
            if region:
                found.add(region)

    return found or {REGION_WORLD}

REGION_FILTER_ALL = "All regions"
REGION_SETTING_KEY = "region_filter"

REGION_FILTER_DEFAULT = REGION_FILTER_ALL

REGION_FILTER_OPTIONS = (
    (REGION_FILTER_ALL, None),
    ("Region Free / World", frozenset({REGION_WORLD})),
    ("USA / NTSC-U", frozenset({REGION_USA})),
    ("Europe / PAL", frozenset({REGION_EUROPE})),
    ("Japan / Asia", frozenset({REGION_JAPAN})),
)

REGION_FILTER_LABELS = [label for label, _ in REGION_FILTER_OPTIONS]
_REGION_FILTER_MAP = dict(REGION_FILTER_OPTIONS)

def region_filter_for_label(label: str):
    return _REGION_FILTER_MAP.get(label)

def entry_matches_region(entry, wanted) -> bool:
    if not wanted:
        return True
    return bool(detect_regions(getattr(entry, "name", "") or "") & wanted)

def resource_path(relative_path: str) -> Path:
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        base_path = Path(__file__).resolve().parent
    return base_path / relative_path

def _installed(module_name: str, pip_name: str | None = None):
    try:
        return importlib.import_module(module_name)
    except ImportError:
        if IS_FROZEN:
            # In a built exe, sys.executable is the app itself, so "-m pip"
            # would just launch another copy. The module must be bundled.
            raise
        pip_name = pip_name or module_name
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            pass
        return importlib.import_module(module_name)

_installed("customtkinter", pip_name="customtkinter")
_installed("PIL", pip_name="Pillow")
curl_cffi_requests = _installed("curl_cffi.requests", pip_name="curl_cffi")
for _mod, _pip in (("py7zr", "py7zr"), ("unrar.cffi.unrarlib", "unrar2-cffi")):
    try:
        _installed(_mod, pip_name=_pip)
    except Exception:
        pass        # reported when an archive needs it, and in Settings > Archives

import customtkinter as ctk

ctk.set_default_color_theme("dark-blue")
ctk.set_appearance_mode("Dark")

_ctk_original_set_appearance_mode = ctk.set_appearance_mode

def _force_dark_mode(mode_string: str = "Dark") -> None:
    _ctk_original_set_appearance_mode("Dark")

ctk.set_appearance_mode = _force_dark_mode

ARCHIVE_BASE_URL = "https://archive.org"
ARCHIVE_S3_BASE_URL = "https://s3.us.archive.org"
ARCHIVE_S3_KEYS_URL = "https://archive.org/account/s3.php"

def is_archive_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "archive.org" or host.endswith(".archive.org")

# ---------------------------------------------------------------------------
# Cover art - every cover comes from the BmanGames024 GitHub repo
# (github.com/BmanGames024/OG-Xbox-and-360-Cover-Art), see cover_repo_path.
# MobCat's Original Xbox list is still used to know each OG game's other names.
# ---------------------------------------------------------------------------
OG_XBOX_SOURCES = frozenset({
    "mxogcxpt2-xbox-ztm",
    "mxogcxo",
    "mxogcx-xbox-ztm",
})

_COVER_EXT_RE = re.compile(r"\.(rar|zip|7z|iso|god|tar|gz)$", re.IGNORECASE)
_COVER_TAG_RE = re.compile(r"[\[\(][^\]\)]*[\]\)]")
_COVER_ALIASES = {"gta": "Grand Theft Auto", "cod": "Call of Duty", "kof": "King of Fighters",
                  "pgr": "Project Gotham Racing", "nfs": "Need for Speed", "dbz": "Dragon Ball Z",
                  "tmnt": "Teenage Mutant Ninja Turtles", "wsop": "World Series of Poker"}
# Not part of the game's name: episode ranges ("Episode 1-5" = the whole season),
# title updates ("TU", "TU6"), version numbers ("v1.2").
# (Only after "Episode"/"Ep." - plain ranges are often the name: "Final Fantasy 13-2".)
_COVER_JUNK_RE = re.compile(
    r"\b(?i:ep(?:isode)?s?\.?)\s*\d+\s*-\s*\d+\b"     # Episode 1-5, Ep. 1-5
    r"|\bTU\d*\b"                                        # TU, TU6 (capitals only: not "Tu")
    r"|\b(?i:v)\d+(?:\.\d+)+\b")                         # v1.2, V2.5


def is_og_xbox_entry(entry) -> bool:
    return (getattr(entry, "archive_identifier", None) or "") in OG_XBOX_SOURCES


def cover_search_name(name: str, dlc: bool = False, expand: bool = True) -> str:
    """'Dead Rising [NTSCU][NTSCJ].rar' -> 'Dead Rising'.
    DLC ('Forza Motorsport 4 - January Pack (World) (Addon).zip') -> the game, 'Forza Motorsport 4'."""
    text = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    text = re.sub(r"\.~\d+~$", "", text)
    if re.search(r"\.(jpe?g|png|gif|xml|txt|sqlite|torrent)$", text, re.IGNORECASE):
        return ""                      # archive.org thumbnails / metadata, not games
    while True:
        stripped = _COVER_EXT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    if "(addon)" in text.lower():
        # DLC packs are "Game - Pack name (World) (Addon)": the cover is the game's.
        text = text.split(" - ", 1)[0]
    text = re.sub(r"_?_x27_", "'", text)                # "I__x27_ll" -> "I'll" (encoded apostrophe)
    text = _COVER_TAG_RE.sub(" ", text).replace("_", " ")
    text = _COVER_JUNK_RE.sub(" ", text)
    if expand:                                 # "GTA" -> "Grand Theft Auto"
        for short, full in _COVER_ALIASES.items():
            text = re.sub(rf"\b{short}\b", full, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip(" -.")


# Name matching. Both sides go through _cover_tokens, so "Black Ops 2" and
# "Call of Duty®: Black Ops II" become the same words; then the cover repo title
# sharing the most words with the file name wins.
_COVER_NUMBER_WORDS = {
    "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}
# Dropped from both names before comparing ("Borderlands GOTY" is "Borderlands").
_COVER_FILLER_WORDS = {"the", "and", "of", "a", "an", "goty", "edition"}
_COVER_FILLER_PHRASES = ("game of the year edition", "game of the year")
# Count as matched words, but a title sharing ONLY these isn't a match.
_COVER_GENERIC_WORDS = {"game", "games", "season", "episode", "ep", "part", "chapter", "series",
                        "collection", "hd", "live", "arcade", "xbox", "360", "pack", "complete",
                        "ultimate", "deluxe", "special", "edition", "telltale", "for", "in", "on",
                        "to", "vs", "from", "with", "dlc", "addon"}
# A candidate with one of these (and a wanted name without it) is probably not the game itself.
_COVER_SIDE_WORDS = {"demo", "beta", "trial", "bundle", "pack", "dlc", "soundtrack",
                     "theme", "themes", "gamerpic", "gamerpics", "picture", "pictures",
                     "avatar", "trailer", "video", "launch"}


def _cover_tokens(text: str) -> list[str]:
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"['\u2019\u00ae\u2122]", "", text)          # ' ’ ® ™
    for phrase in _COVER_FILLER_PHRASES:
        text = text.replace(phrase, " ")
    tokens = []
    for tok in re.findall(r"[a-z0-9]+", text):
        tok = _COVER_NUMBER_WORDS.get(tok, tok)
        if tok.isdigit():
            tok = str(int(tok))                                  # "07" == "7"
        if tok not in _COVER_FILLER_WORDS:
            tokens.append(tok)
    return tokens



def _cover_word_match(a: str, b: str) -> float:
    """1 for the same word, 0.9 for a near miss ("instinc"/"instinct", "strom"/"storm")."""
    if a == b:
        return 1.0
    if a.isdigit() or b.isdigit() or min(len(a), len(b)) < 4:
        return 0.0
    if a.startswith(b) or b.startswith(a):
        return 0.9
    import difflib
    return 0.9 if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 else 0.0


_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            if len(_KEY_LOCKS) > 2000:
                _KEY_LOCKS.clear()
            lock = _KEY_LOCKS[key] = threading.Lock()
        return lock


def _cover_image_path(url: str, folder: Path | None = None) -> Path:
    ext = ".png" if url.lower().endswith(".png") else ".jpg"
    return (folder or COVER_IMAGE_DIR) / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ext)


def load_saved_image(key: str, folder: Path | None = None) -> bytes | None:
    """An image saved earlier under core/ (covers by default), or None."""
    try:
        data = _cover_image_path(key, folder).read_bytes()
    except OSError:
        return None
    return data or None


def save_image(key: str, data: bytes, folder: Path | None = None) -> None:
    if data:
        _save_cover_image(_cover_image_path(key, folder), data)


def _save_cover_image(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.stem}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass



# ----- OG Xbox names: MobCat's Original Xbox Title ID DB ----------------------
# https://www.mobcat.zip/XboxIDs/ - its disc list (redump-xbox-info.json) knows every
# OG disc's XMID and name, including the other names a game was sold under. File
# names like "Elder.Scrolls.3.Morrowind.GOTY.Edition.USA.XBOX-ZTM.rar" are matched
# to those names ("The Elder Scrolls III: Morrowind: Game of the Year Edition") by
# their words; the XMID then finds the game's cover in the cover repo.
MOBCAT_BASE_URL = "https://raw.githubusercontent.com/MobCat/MobCats-original-xbox-game-list/main"
MOBCAT_INDEX_URL = f"{MOBCAT_BASE_URL}/redump-xbox-info.json"
MOBCAT_INDEX_FILE = "mobcat_xbox_index.json"
MOBCAT_INDEX_TTL_SECONDS = 7 * 24 * 60 * 60
MOBCAT_OFFLINE_PAUSE = 2 * 60        # after a network failure, don't retry for a bit
MOBCAT_TIMEOUT = 20
MOBCAT_MIN_MATCH_SCORE = 0.75        # below this, no cover beats the wrong cover

_MOBCAT_LOCK = threading.Lock()
_MOBCAT_GAMES: list | None = None    # [(name, tokens, [xmid, ...] best disc first per category)]
_MOBCAT_OFFLINE_UNTIL = 0.0

_MOBCAT_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
                 "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12", "xiii": "13"}
_MOBCAT_STOP_WORDS = {"the", "a", "an", "and", "1"}   # scene names add "1": "Serious.Sam.1"
# region word in the file name -> XMID region letters to prefer, best first
# (A = USA, E = Europe/PAL, J = Japan, K = USA+Japan, L = USA+Europe, W = world)
_MOBCAT_REGIONS = {
    "usa": "AKLWEJ", "us": "AKLWEJ", "ntsc": "AKLWEJ", "ntscu": "AKLWEJ", "can": "AKLWEJ",
    "eur": "ELWAKJ", "europe": "ELWAKJ", "pal": "ELWAKJ", "uk": "ELWAKJ", "aus": "ELWAKJ",
    "ger": "ELWAKJ", "fra": "ELWAKJ", "fre": "ELWAKJ", "ita": "ELWAKJ", "spa": "ELWAKJ",
    "jap": "JKWALE", "jpn": "JKWALE", "japan": "JKWALE", "ntscj": "JKWALE",
    "rf": "WLKAEJ", "world": "WLKAEJ",
}
_MOBCAT_DEFAULT_REGION = "AKLWEJ"
_MOBCAT_SCENE_JUNK = {"proper", "repack", "internal", "readnfo", "dvd", "dvd9", "multi",
                      "multi3", "multi5", "pal60"}
_MOBCAT_CATEGORY_RANK = {"Games": 0, "Bonus Discs": 1, "Add-Ons": 1, "Applications": 1,
                         "Coverdiscs": 2, "Demos": 3, "Preproduction": 4}


_MOBCAT_GERMAN = str.maketrans({"\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue", "\u00df": "ss"})


def _mobcat_tokens(text: str) -> list[str]:
    import unicodedata
    # "München" -> "muenchen" (how scene names spell it), "Increíbles" -> "increibles"
    text = unicodedata.normalize("NFKD", text.lower().translate(_MOBCAT_GERMAN))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"\bgoty\b", " game of the year ", text)
    words = re.split(r"[^a-z0-9]+", text)
    return [_MOBCAT_ROMAN.get(w, w) for w in words if w and w not in _MOBCAT_STOP_WORDS]


def _mobcat_query(file_name: str) -> tuple[list[str], str]:
    """'Elder.Scrolls.3.Morrowind.USA.XBOX-ZTM.rar' -> (['elder', 'scrolls', '3', 'morrowind'], 'AKLWEJ')."""
    text = (file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    while True:
        stripped = _COVER_EXT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"[.\s_-]*XBOX[-_][A-Za-z0-9]+$", "", text, flags=re.IGNORECASE)   # -XBOX-ZTM
    regions = _MOBCAT_DEFAULT_REGION
    found_region = False
    # "(USA)" style tags (redump names): note the region, then drop them
    for tag in re.findall(r"[\[\(]([^\]\)]*)[\]\)]", text):
        for word in re.split(r"[^a-z]+", tag.lower()):
            if word in _MOBCAT_REGIONS and not found_region:
                regions, found_region = _MOBCAT_REGIONS[word], True
    text = _COVER_TAG_RE.sub(" ", text)
    words = [w for w in re.split(r"[.\s_]+", text) if w]
    # Region, revision and release junk only come at the END of a scene name
    # ("...Rev.1.USA"): taken from there only, so "World Racing" and
    # "Dance: UK" keep "World" and "UK".
    while len(words) > 1:
        low = words[-1].lower()
        if low in _MOBCAT_REGIONS:
            if not found_region:
                regions, found_region = _MOBCAT_REGIONS[low], True
            words.pop()
        elif low in _MOBCAT_SCENE_JUNK:
            words.pop()
        elif low.isdigit() and len(words) > 2 and words[-2].lower() == "rev":
            del words[-2:]                         # "Rev.1": a disc revision, not the name
        else:
            break
    return _mobcat_tokens(" ".join(words)), regions


MOBCAT_MAIN_TITLE_WEIGHT = 0.9     # "Blinx 2" for "Blinx 2: Masters of Time & Space"


def _mobcat_alternatives(name: str) -> list[tuple[list[str], float, bool]]:
    """Ways a file can name this game: (tokens, weight, is_main_title_only).
    'Blinx 2: Masters of Time & Space ~ Blinx 2: Battle of Time & Space' is two
    names (US / Japan) in one: each counts on its own. The part before the
    subtitle counts too, a bit less, for files that leave the subtitle out."""
    alts = []
    for part in name.split("~"):
        full = _mobcat_tokens(part)
        if full:
            alts.append((full, 1.0, False))
        main = _mobcat_tokens(re.split(r":| - ", part, 1)[0])
        if main and main != full:
            alts.append((main, MOBCAT_MAIN_TITLE_WEIGHT, True))
    whole = _mobcat_tokens(name)
    if whole and all(whole != a[0] for a in alts):
        alts.append((whole, 1.0, False))
    return alts


def _mobcat_build_games(blob: dict) -> list:
    by_name: dict[str, list[tuple[str, str]]] = {}
    for xmid, info in (blob or {}).items():
        if not isinstance(info, dict) or not re.fullmatch(r"[A-Z0-9]{8}", str(xmid)):
            continue
        name = str(info.get("Name") or "").strip()
        if name:
            by_name.setdefault(name, []).append((xmid, str(info.get("Category") or "")))
    games = []
    for name, discs in by_name.items():
        alts = _mobcat_alternatives(name)
        if alts:
            games.append((name, alts, discs))
    return games


def _mobcat_games() -> list:
    """MobCat's game list: downloaded once a week, kept in core/. [] if unavailable."""
    global _MOBCAT_GAMES, _MOBCAT_OFFLINE_UNTIL
    with _MOBCAT_LOCK:
        if _MOBCAT_GAMES is not None:
            return _MOBCAT_GAMES
        path = CORE_DIR / MOBCAT_INDEX_FILE
        saved = None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            fresh = 0 <= time.time() - path.stat().st_mtime < MOBCAT_INDEX_TTL_SECONDS
        except Exception:
            fresh = False
        if saved and fresh:
            _MOBCAT_GAMES = _mobcat_build_games(saved)
            return _MOBCAT_GAMES
        if time.time() < _MOBCAT_OFFLINE_UNTIL:
            return []
        blob = None
        session = curl_cffi_requests.Session(impersonate="chrome124")
        try:
            resp = session.get(MOBCAT_INDEX_URL, timeout=MOBCAT_TIMEOUT)
            if resp.status_code == 200:
                blob = resp.json()
        except Exception as exc:
            log_event(f"MobCat Xbox game list download failed: {exc}")
        finally:
            try:
                session.close()
            except Exception:
                pass
        if isinstance(blob, dict) and blob:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(blob), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
            _MOBCAT_GAMES = _mobcat_build_games(blob)
            return _MOBCAT_GAMES
        _MOBCAT_OFFLINE_UNTIL = time.time() + MOBCAT_OFFLINE_PAUSE
        if saved:
            _MOBCAT_GAMES = _mobcat_build_games(saved)        # an old copy beats nothing
            return _MOBCAT_GAMES
        return []


def mobcat_cover_xmids(file_name: str) -> list[str]:
    """XMIDs (disc IDs) whose cover to try for an OG Xbox file name, best first."""
    import difflib
    query, regions = _mobcat_query(file_name)
    if not query:
        return []
    q = collections.Counter(query)
    scored = []          # (score, word order similarity, game, matched tokens, main-title-only)
    for game in _mobcat_games():
        best = None
        for tokens, weight, main_only in game[1]:
            common = sum((q & collections.Counter(tokens)).values())
            if not common:
                continue
            score = weight * 2 * common / (len(query) + len(tokens))
            if best is None or score > best[0]:
                best = (score, tokens, main_only)
        if best:
            order = difflib.SequenceMatcher(None, query, best[1]).ratio()
            scored.append((best[0], order, game, best[1], best[2]))
    if not scored:
        return []
    # Same words in a different order ("UFC 2: Tapout" / "UFC: Tapout 2"): word order decides.
    scored.sort(key=lambda r: (r[0], r[1], -len(r[2][0])), reverse=True)
    score, _, game, tokens, main_only = scored[0]
    if score < MOBCAT_MIN_MATCH_SCORE:
        return []
    if main_only and sum(1 for r in scored if r[4] and abs(r[0] - score) < 1e-9) > 1:
        return []        # "Star Wars" alone is the main title of many games: can't tell which
    if query != tokens and not any(not w.isdigit() for w in (q & collections.Counter(tokens))):
        return []                                    # only numbers in common: not a match
    q_nums = {w for w in query if w.isdigit()}
    c_nums = {w for w in tokens if w.isdigit()}
    if c_nums - q_nums or any(not re.fullmatch(r"(19|20)\d\d", n) for n in q_nums - c_nums):
        return []    # "Battlefront" vs "Battlefront II": a sequel's cover is wrong (an extra year is fine)

    def rank(disc):
        xmid, category = disc
        region = regions.find(xmid[-1])
        return (_MOBCAT_CATEGORY_RANK.get(category, 5), region if region >= 0 else 9, xmid)

    return [xmid for xmid, _ in sorted(game[2], key=rank)]



# ----- Cover art: the BmanGames024 cover repo ----------------------------------
# Every cover comes from one GitHub repo:
#   Original Xbox Covers/<Name> (<TitleID>) (<XMID>).jpg   one per game
#   Original Xbox Covers/<Name> (<Region>).jpg            games MobCat doesn't list
#   Xbox 360 Covers/<Name> (<TitleID>).jpg                retail, Arcade and Indie
# Its file list is downloaded once a day (one request) and kept in core\; after
# that, finding a game's cover happens right here, and only the picture itself
# is downloaded (then kept in core\covers).
#
# Finding the cover:
#   Xbox 360: the title ID in the file name if it has one, otherwise the same
#     word matching dbox.tools lookups used (most of the file name's words, few
#     extra words; rarer words count for more) against every name in the repo -
#     the best match wins, so nearly every game gets a cover.
#   OG Xbox: MobCat's name list first (it knows each game's other names: regional
#     titles, translations, "Blinx 2: Battle of Time & Space"...) -> XMID ->
#     title ID -> the repo's file. If that finds nothing, the same best-match
#     word matching against the repo's OG names.
COVER_REPO = "BmanGames024/OG-Xbox-and-360-Cover-Art"
COVER_REPO_BRANCH = "main"
COVER_REPO_OG_FOLDER = "Original Xbox Covers"
COVER_REPO_360_FOLDER = "Xbox 360 Covers"
COVER_REPO_TREE_URL = f"https://api.github.com/repos/{COVER_REPO}/git/trees/{COVER_REPO_BRANCH}?recursive=1"
COVER_REPO_LIST_URL = f"https://data.jsdelivr.com/v1/packages/gh/{COVER_REPO}@{COVER_REPO_BRANCH}?structure=flat"
COVER_REPO_IMAGE_URLS = (                    # tried in order
    f"https://raw.githubusercontent.com/{COVER_REPO}/{COVER_REPO_BRANCH}/{{path}}",
    f"https://cdn.jsdelivr.net/gh/{COVER_REPO}@{COVER_REPO_BRANCH}/{{path}}",
)
COVER_REPO_ALIASES = "aliases.json"         # other names for the covers (full titles, translations...)
COVER_REPO_INDEX_FILE = "cover_repo_index.json"
COVER_REPO_INDEX_VERSION = 2                 # bump when what's saved changes: older saved lists are re-downloaded
COVER_REPO_INDEX_TTL_SECONDS = 24 * 60 * 60
COVER_REPO_TIMEOUT = (5, 15)                 # (connect, read) seconds
COVER_OFFLINE_PAUSE = 2 * 60                 # after a network failure, don't retry for a bit
COVER_MIN_MATCH_SCORE = 0.01                 # as dbox: any title sharing a real word; the best wins
COVER_NUMBER_MISMATCH = 0.85                 # "Halo 3" for "Halo 2" only if nothing better

_COVER_LOCK = threading.Lock()
_COVER_INDEX = None                          # _CoverRepoIndex, built on first use
_COVER_OFFLINE_UNTIL = 0.0
_COVER_MATCHES: "OrderedDict[tuple, str | None]" = OrderedDict()   # (name, og, dlc) -> repo path
_COVER_IMAGES: "OrderedDict[str, bytes]" = OrderedDict()           # repo path -> picture
_COVER_MEMORY_MAX = 128

# (A lone "X" is left alone: "SBK X", "DeathSmiles2 X" aren't 10.)
_COVER_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
                "viii": "8", "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15"}
_COVER_YEAR_RE = re.compile(r"(19|20)\d\d")
# Everyday words that can't make a match on their own ("Some Game That Does Not
# Exist" isn't "The Suffering: Ties That Bind"); they still count once matched.
_COVER_COMMON_WORDS = {"that", "this", "these", "those", "is", "it", "its", "my", "your", "you",
                       "we", "me", "no", "not", "all", "at", "by", "up", "out", "some", "does",
                       "do", "are", "be", "was", "will", "can", "get", "got", "into", "over",
                       "under", "new", "world", "life", "time", "big", "little", "super", "x"}
_COVER_OG_FILE_RE = re.compile(r"^(?P<name>.*) \((?P<tid>[0-9A-F]{8})\) \((?P<xmid>[A-Z0-9]{8})\)$")
_COVER_360_FILE_RE = re.compile(r"^(?P<name>.*) \((?P<tid>[0-9A-Fa-f]{8})\)$")


def covers_offline() -> bool:
    """True while cover downloads are paused after a network failure."""
    return time.time() < _COVER_OFFLINE_UNTIL


def _cover_go_offline(why: str) -> None:
    global _COVER_OFFLINE_UNTIL
    if not covers_offline():
        log_event(f"Cover art: {why} - trying again in {COVER_OFFLINE_PAUSE // 60} min")
    _COVER_OFFLINE_UNTIL = time.time() + COVER_OFFLINE_PAUSE


def _cover_repo_tokens(text: str, og: bool = False, camel: bool = True, years: bool = True) -> list[str]:
    """Words to compare, the same way on both sides: "Halo III" == "Halo 3",
    "SplinterCellConviction" == "Splinter Cell Conviction", "GTA IV" == "Grand Theft Auto IV",
    "G.I. Joe" == "GI Joe", "NHL 04" == "NHL 2004", "Brütal" == "Brutal"."""
    import unicodedata
    text = re.sub(r"[\u2022\u00b7\u30fb]", ".", text or "")                   # "D•A•C" -> "D.A.C"
    text = re.sub(r"\b((?:[A-Za-z]\.){2,}[A-Za-z]?)", lambda m: m.group(1).replace(".", ""), text)
    if years:
        text = re.sub(r"(?<![\d.'])0(\d)(?![\d.])", r"200\1", text)      # "NHL 04" -> "NHL 2004"
    if camel:
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    for short, full in _COVER_ALIASES.items():
        text = re.sub(rf"\b{short}\b", full, text, flags=re.IGNORECASE)
    if og:
        text = " ".join(_mobcat_tokens(text))          # accents, "GOTY", scene "1"...
    else:
        # "Brütal" -> "Brutal". NFD, not NFKD: NFKD would turn "™" into "TM" ("2072™" -> "2072tm").
        folded = unicodedata.normalize("NFD", text)
        text = "".join(ch for ch in folded if not unicodedata.combining(ch))
    tokens = [_COVER_ROMAN.get(t, t) for t in _cover_tokens(text)]
    if not tokens:                                      # "アーマード・コアⅤ": the whole name, as one word
        whole = "".join(ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum())
        tokens = [whole] if whole else []
    return tokens


def _cover_token_variants(text: str, og: bool = False) -> list[list[str]]:
    """The words, plus the CamelCase-joined version if that differs:
    "BioShock" is both ["bio", "shock"] and ["bioshock"]; and "Rally 03" is both
    "Rally 2003" and "Rally 3" (scene names write either that way)."""
    out = []
    for years in (True, False):
        for camel in (True, False):
            tokens = _cover_repo_tokens(text, og, camel, years)
            if tokens and tokens not in out:
                out.append(tokens)
    return out


def _cover_plain(text: str) -> str:
    """For telling apart names that score the same ("Halo 3" / "Halo 3!"):
    lower case, without trademark signs, punctuation kept."""
    return re.sub(r"\s+", " ", re.sub(r"[\u00ae\u2122\u00a9]", "", (text or "").lower())).strip()


def _cover_numbers(tokens: list[str]) -> set[str]:
    return {t for t in tokens if t.isdigit()}


def _cover_numbers_agree(query: list[str], candidate: list[str]) -> bool:
    """Sequels differ by number; an extra year in the file name ("2005") is fine."""
    q, c = _cover_numbers(query), _cover_numbers(candidate)
    return not (c - q) and all(_COVER_YEAR_RE.fullmatch(n) for n in q - c)


class _CoverNameIndex:
    """Names -> repo files, with a word index so a lookup only scores names
    that share a word (or a near miss: the first 4 letters) with the file name."""

    def __init__(self, og: bool):
        self.og = og
        self.entries: list[tuple[list[str], str, str, bool, bool]] = []   # (tokens, path, plain, indie, alias)
        self.by_word: dict[str, list[int]] = collections.defaultdict(list)

    def add(self, name: str, path: str, indie: bool = False, alias: bool = False) -> None:
        for tokens in _cover_token_variants(name, self.og):
            i = len(self.entries)
            self.entries.append((tokens, path, _cover_plain(name), indie, alias))
            keys = {k for t in tokens for k in self._keys(t, True)} | {"=" + "".join(tokens)}
            for key in keys:
                self.by_word[key].append(i)

    @staticmethod
    def _keys(token: str, common: bool = False) -> list[str]:
        if token in _COVER_GENERIC_WORDS or (token in _COVER_COMMON_WORDS and not common):
            return []
        return [token] + (["~" + token[:4]] if len(token) >= 4 and not token.isdigit() else [])

    def finish(self) -> None:
        """Count how many names use each word: rare words ("Conviction") then count
        for more than common ones ("Tom", "Clancy's") when scoring."""
        df = collections.Counter(t for tokens, *_ in self.entries for t in set(tokens))
        n = len(self.entries) + 1
        self.weight = {t: math.log(n / (c + 0.5)) ** 2 for t, c in df.items()}   # squared: rare words stand out
        self.unknown_weight = math.log(n / 0.5) ** 2

    def _score(self, ta: list[str], tb: list[str]) -> float:
        """The dbox.tools scoring: mostly how many of the file name's words the
        title has, then how few extra words it has - with each word counted by
        how rare it is instead of all words counting the same."""
        if ta == tb:
            return 1.0
        weight = lambda t: self.weight.get(t, self.unknown_weight)
        unused = list(tb)
        matched, real = 0.0, False
        for word in ta:
            best, best_i = 0.0, -1
            for i, other in enumerate(unused):
                m = _cover_word_match(word, other)
                if m > best:
                    best, best_i = m, i
                    if m == 1.0:
                        break
            if best_i >= 0:
                matched += best * weight(word)
                unused.pop(best_i)
                if word not in _COVER_GENERIC_WORDS:
                    real = True
        if not real:
            return 0.0
        total_a = sum(weight(t) for t in ta) or 1.0
        total_b = sum(weight(t) for t in tb) or 1.0
        score = 0.65 * matched / total_a + 0.35 * min(1.0, matched / total_b)
        if (set(tb) & _COVER_SIDE_WORDS) - set(ta):
            score *= 0.9                         # demos, bundles, packs...
        return min(score, 0.99)

    def best(self, query: list[str], plain: str = "") -> tuple[str | None, float]:
        keys = {k for t in query for k in self._keys(t)}
        if not keys:            # "In Time": only everyday words, so let them count
            keys = {k for t in query for k in self._keys(t, True)}
        squashed = "".join(query)
        keys.add("=" + squashed)                 # "RUSE" finds "R.U.S.E", "Skydrift" finds "SkyDrift"
        candidates = {i for k in keys for i in self.by_word.get(k, ())}
        scored = []
        for i in candidates:
            tokens, path, name_plain, indie, _alias = self.entries[i]
            score = 1.0 if "".join(tokens) == squashed else self._score(query, tokens)
            if score <= 0:
                continue
            agree = _cover_numbers_agree(query, tokens)
            if not agree:
                score *= COVER_NUMBER_MISMATCH
            scored.append((score, agree, i))
        if not scored:
            return None, 0.0
        top = max((s, a) for s, a, _ in scored)
        if top[0] < COVER_MIN_MATCH_SCORE:
            return None, 0.0
        import difflib
        # Same score ("Halo 3" / "Halo 3!", "Dishonored" / "... Game of the Year Edition"):
        # a cover's own name before an alias (a bundle disc lists "Plants vs. Zombies" too),
        # then the name spelled most like the file name, then not an indie game, then the shorter.
        ties = [self.entries[i] for s, a, i in scored if (s, a) == top]
        best = min(ties, key=lambda e: (e[4], -(difflib.SequenceMatcher(None, plain, e[2]).ratio() if plain else 0),
                                        e[3], len(e[0]), e[1]))     # ...then the lower title ID
        return best[1], top[0]

    def best_of(self, queries: list) -> tuple[str | None, float]:
        """The best cover over several ways of writing the file name (earlier
        ones win a tie). Each query is a name, or (name, name as written) - the
        second one is what ties are judged by ("NFS Most Wanted" picks the cover
        called "NFS Most Wanted", not "Need for Speed Most Wanted")."""
        best, best_rank = None, None
        for n, query in enumerate(queries):
            text, written = query if isinstance(query, tuple) else (query, query)
            for tokens in _cover_token_variants(text, self.og):
                path, score = self.best(tokens, _cover_plain(written))
                if path and (best_rank is None or (score, -n) > best_rank):
                    best, best_rank = path, (score, -n)
        return (best, best_rank[0]) if best else (None, 0.0)


class _CoverRepoIndex:
    def __init__(self, paths: list[str], aliases: dict | None = None):
        self.og_by_tid: dict[str, str] = {}
        self.og_by_xmid: dict[str, str] = {}
        self.og_names = _CoverNameIndex(og=True)
        self.x360_by_tid: dict[str, str] = {}
        self.x360_names = _CoverNameIndex(og=False)
        for path in sorted(paths):
            folder, _, file = path.partition("/")
            if not file.lower().endswith(".jpg"):
                continue
            stem = file.rsplit("/", 1)[-1][:-4]
            if folder == COVER_REPO_OG_FOLDER:
                m = _COVER_OG_FILE_RE.match(stem)
                if m:
                    self.og_by_tid.setdefault(m["tid"], path)
                    self.og_by_xmid.setdefault(m["xmid"], path)
                    self.og_names.add(m["name"], path)
                else:                                   # "Name (USA)": not in MobCat
                    self.og_names.add(_COVER_TAG_RE.sub(" ", stem), path)
            elif folder == COVER_REPO_360_FOLDER:
                m = _COVER_360_FILE_RE.match(stem)
                if m:
                    self.x360_by_tid.setdefault(m["tid"].upper(), path)
                    self.x360_names.add(m["name"], path, indie=m["tid"].upper().startswith("5855"))
        # aliases.json: other names each cover goes by (full titles for short store
        # names like "PGR 4", regional and translated titles for OG games)
        self.has_og_aliases = False
        known = set(paths)
        for path, names in (aliases or {}).items():
            if path not in known or not isinstance(names, list):
                continue
            folder = path.partition("/")[0]
            for alias in names:
                if not isinstance(alias, str) or not alias.strip():
                    continue
                if folder == COVER_REPO_OG_FOLDER:
                    self.og_names.add(alias, path, alias=True)
                    self.has_og_aliases = True
                elif folder == COVER_REPO_360_FOLDER:
                    tid = _COVER_360_FILE_RE.match(path.rsplit("/", 1)[-1][:-4])
                    self.x360_names.add(alias, path, indie=bool(tid) and tid["tid"].upper().startswith("5855"),
                                        alias=True)
        self.og_names.finish()
        self.x360_names.finish()
        self.size = len(self.og_by_tid) + len(self.x360_by_tid)


def _cover_http_get(url: str):
    session = curl_cffi_requests.Session(impersonate="chrome124")
    try:
        return session.get(url, timeout=COVER_REPO_TIMEOUT)
    finally:
        try:
            session.close()
        except Exception:
            pass


def _cover_download_file_list() -> list[str] | None:
    """Every file in the repo: GitHub's file list, or jsDelivr's if GitHub won't answer."""
    try:
        resp = _cover_http_get(COVER_REPO_TREE_URL)
        if resp.status_code == 200:
            data = resp.json()
            paths = [e["path"] for e in data.get("tree") or [] if e.get("type") == "blob"]
            if paths and not data.get("truncated"):
                return paths
    except Exception as exc:
        log_event(f"Cover art: GitHub file list failed ({exc}), trying jsDelivr")
    try:
        resp = _cover_http_get(COVER_REPO_LIST_URL)
        if resp.status_code == 200:
            paths = [str(f.get("name", "")).lstrip("/") for f in resp.json().get("files") or []]
            return [p for p in paths if p] or None
    except Exception as exc:
        log_event(f"Cover art: jsDelivr file list failed ({exc})")
    return None


def _cover_download_aliases(paths: list[str]) -> dict:
    """aliases.json from the repo ({} if the repo doesn't have one)."""
    if COVER_REPO_ALIASES not in paths:
        return {}
    for template in COVER_REPO_IMAGE_URLS:              # GitHub, then the jsDelivr mirror
        try:
            resp = _cover_http_get(template.format(path=quote(COVER_REPO_ALIASES)))
            if resp.status_code == 200:
                data = resp.json()
                aliases = data.get("aliases") if isinstance(data, dict) else None
                if isinstance(aliases, dict):
                    return aliases
        except Exception as exc:
            log_event(f"Cover art: {COVER_REPO_ALIASES} download failed ({exc})")
    return {}


def _cover_index() -> _CoverRepoIndex | None:
    """The repo's file list (and aliases.json), downloaded once a day.
    None if it can't be had at all."""
    global _COVER_INDEX
    with _COVER_LOCK:
        if _COVER_INDEX is not None:
            return _COVER_INDEX
        path = CORE_DIR / COVER_REPO_INDEX_FILE
        saved, saved_aliases = None, {}
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            if blob.get("repo") == COVER_REPO:
                saved = blob.get("paths") or None
                saved_aliases = blob.get("aliases") or {}
                # A list saved by an older version (no aliases.json in it) is never
                # "fresh": it's used only if GitHub can't be reached.
                fresh = (blob.get("v") == COVER_REPO_INDEX_VERSION and
                         0 <= time.time() - float(blob.get("t", 0)) < COVER_REPO_INDEX_TTL_SECONDS)
            else:
                fresh = False
        except Exception:
            fresh = False
        paths, aliases = (saved, saved_aliases) if saved and fresh else (None, {})
        if paths is None and not covers_offline():
            paths = _cover_download_file_list()
            if paths:
                aliases = _cover_download_aliases(paths) or saved_aliases
                try:
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"repo": COVER_REPO, "v": COVER_REPO_INDEX_VERSION,
                                               "t": time.time(), "paths": paths, "aliases": aliases}),
                                   encoding="utf-8")
                    os.replace(tmp, path)
                except OSError:
                    pass
            else:
                _cover_go_offline("couldn't get the cover list")
        if paths is None:
            paths, aliases = saved, saved_aliases       # an old list beats none
        if not paths:
            return None
        _COVER_INDEX = _CoverRepoIndex(paths, aliases)
        log_event(f"Cover art: {_COVER_INDEX.size} covers listed in {COVER_REPO}"
                  f" ({sum(len(v) for v in aliases.values() if isinstance(v, list))} other names)")
        return _COVER_INDEX


def _og_title_ids(xmid: str) -> list[str]:
    """Title IDs an OG disc may be filed under: MS10003W -> 4D530064 (and the
    add-on form with 0x8000 set, which a few demo/bonus discs use)."""
    if not re.fullmatch(r"[A-Z]{2}\d{3}.*", xmid or ""):
        return []
    base = int(xmid[2:5])
    return [f"{ord(xmid[0]):02X}{ord(xmid[1]):02X}{n:04X}" for n in (base, base | 0x8000)]


def _cover_queries(name: str, dlc: bool) -> list[str]:
    """Ways to write an Xbox 360 file name for matching, best first.
    DLC "Call of Duty - Black Ops II - Revolution (World) (Addon)" -> the game:
    "Call of Duty - Black Ops II", then "Call of Duty" (the pack name is dropped)."""
    text = re.sub(r"_?_40_", "(", name or "")           # archive.org's "___40_World__41_" = "(World)"
    text = re.sub(r"_41_?_?", ")", text)
    addon = "(addon)" in text.lower()
    if addon:
        text = re.sub(r"\s*\(addon\)", "", text, flags=re.I)
    clean = cover_search_name(text, dlc=False)
    written = cover_search_name(text, dlc=False, expand=False)
    if not _cover_repo_tokens(clean):                   # "[PROTOTYPE].iso": all of it was in brackets
        stem = _COVER_EXT_RE.sub("", text.replace("\\", "/").rsplit("/", 1)[-1])
        clean = written = re.sub(r"[\[\]()]", " ", re.sub(r"\((USA|Europe|Japan|World)[^)]*\)", " ", stem, flags=re.I))
    pairs = [(clean, written)]
    if dlc or addon:
        parts, parts_w = re.split(r"\s+-\s+", clean), re.split(r"\s+-\s+", written)
        if len(parts) == len(parts_w):
            pairs = [(" - ".join(parts[:k]), " - ".join(parts_w[:k])) for k in range(len(parts) - 1, 0, -1)] + pairs
    # "F E A R" / "F_E_A_R" -> "FEAR" (the repo's "F.E.A.R" becomes "FEAR" too)
    join = lambda t: re.sub(r"\b(?:[A-Za-z] ){1,}[A-Za-z]\b", lambda m: m.group(0).replace(" ", ""), t)
    pairs += [(join(c), join(w)) for c, w in pairs if join(c) != c]
    return [(c, w) for c, w in pairs if c.strip()]


def cover_match(name: str, og: bool, dlc: bool = False) -> tuple[str | None, str, float]:
    """(repo path or None, how it was found, score 0..1) for a file name.
    "how" is "title ID", "MobCat <XMID>" (both score 1) or "name match"."""
    index = _cover_index()
    if index is None:
        return None, "no cover list", 0.0
    if og:
        if not index.has_og_aliases:
            # No aliases.json in the repo: MobCat's list knows the other names instead.
            try:
                xmids = mobcat_cover_xmids(name)
            except Exception:
                xmids = []
            for xmid in xmids:
                path = index.og_by_xmid.get(xmid) or next(
                    (index.og_by_tid[t] for t in _og_title_ids(xmid) if t in index.og_by_tid), None)
                if path:
                    return path, f"MobCat {xmid}", 1.0
        words = _mobcat_query(name)[0]
        # Scene names break short forms at the dots: "Operation.V.I.D.E.O.G.A.M.E" ->
        # v i d e o g a m e. Join runs of single letters back into one word.
        joined = re.sub(r"\b(?:[a-z] ){1,}[a-z]\b", lambda m: m.group(0).replace(" ", ""), " ".join(words))
        path, score = index.og_names.best_of([joined, " ".join(words)])
        return path, "name match", score
    tid = ""
    try:
        tid = (catalog_title_id(name) or "").upper()
    except Exception:
        pass
    if tid and tid in index.x360_by_tid:
        return index.x360_by_tid[tid], "title ID", 1.0
    path, score = index.x360_names.best_of(_cover_queries(name, dlc))
    return path, "name match", score


def cover_repo_path(name: str, og: bool, dlc: bool = False) -> str | None:
    """Which cover in the repo goes with this file name (None if none at all)."""
    key = (name, og, dlc)
    with _COVER_LOCK:
        if key in _COVER_MATCHES:
            _COVER_MATCHES.move_to_end(key)
            return _COVER_MATCHES[key]
    if _cover_index() is None:
        return None                                 # no list: don't remember a miss
    path, how, score = cover_match(name, og, dlc)
    if path and how == "name match" and score < 1.0:
        log_event(f"Cover art: {name!r} -> {path.rsplit('/', 1)[-1]!r} (name match {score:.2f})")
    with _COVER_LOCK:
        _COVER_MATCHES[key] = path
        while len(_COVER_MATCHES) > 4000:
            _COVER_MATCHES.popitem(last=False)
    return path


def _cover_repo_image(path: str) -> bytes | None:
    with _COVER_LOCK:
        data = _COVER_IMAGES.get(path)
    if data:
        return data
    with _key_lock("img:" + path):
        data = load_saved_image("repo:" + path)
        if not data and not covers_offline():
            quoted = quote(path, safe="/")
            reachable = False
            for template in COVER_REPO_IMAGE_URLS:      # GitHub, then the jsDelivr mirror
                try:
                    resp = _cover_http_get(template.format(path=quoted))
                except Exception as exc:
                    log_event(f"Cover art: download failed ({exc})")
                    continue
                if resp.status_code < 500 and resp.status_code != 429:
                    reachable = True
                if resp.status_code == 200 and resp.content:
                    data = resp.content
                    save_image("repo:" + path, data)
                    break
            if not data and not reachable:
                _cover_go_offline("couldn't download covers")
        if data:
            with _COVER_LOCK:
                _COVER_IMAGES[path] = data
                while len(_COVER_IMAGES) > _COVER_MEMORY_MAX:
                    _COVER_IMAGES.popitem(last=False)
        return data or None


def fetch_cover(name: str, og: bool, dlc: bool = False) -> bytes | None:
    """The cover picture for a game file name, or None."""
    path = cover_repo_path(name, og, dlc)
    return _cover_repo_image(path) if path else None


def cover_warm_up() -> None:
    """At start-up, get the cover list (and MobCat's names) ready in the
    background, so the first cover you click doesn't wait for them."""
    def work():
        try:
            _cover_index()
            _mobcat_games()
        except Exception as exc:
            log_event(f"Cover art: start-up preparation failed: {exc}")

    threading.Thread(target=work, daemon=True, name="cover-warmup").start()


# ----- background prefetch -----------------------------------------------------
# After a search, covers for the first results are fetched quietly so clicking
# one shows its cover straight away. A new search cancels the old queue.
COVER_PREFETCH_COUNT = 8
COVER_PREFETCH_WORKERS = 2
_PREFETCH_QUEUE: "queue.Queue" = queue.Queue()
_PREFETCH_STARTED = False
_PREFETCH_GEN = 0
_PREFETCH_LOCK = threading.Lock()


def _prefetch_worker() -> None:
    while True:
        gen, name, og, dlc = _PREFETCH_QUEUE.get()
        if gen != _PREFETCH_GEN or covers_offline():
            continue                                 # replaced by a newer search, or offline
        try:
            fetch_cover(name, og, dlc)
        except Exception:
            pass


def prefetch_covers(items: list[tuple[str, bool, bool]], replace: bool = True) -> None:
    """items: (file name, is_og_xbox, is_dlc), most important first.
    replace=True drops anything still queued from an earlier call.
    Daemon threads: a download still running can't stop the app from closing."""
    global _PREFETCH_STARTED, _PREFETCH_GEN
    if not items:
        return
    with _PREFETCH_LOCK:
        if not _PREFETCH_STARTED:
            for i in range(COVER_PREFETCH_WORKERS):
                threading.Thread(target=_prefetch_worker, daemon=True,
                                 name=f"cover-prefetch-{i}").start()
            _PREFETCH_STARTED = True
        if replace:
            _PREFETCH_GEN += 1
        gen = _PREFETCH_GEN
    for name, og, dlc in items:
        _PREFETCH_QUEUE.put((gen, name, og, dlc))


SPOOF = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

LOG_QUEUE: "queue.Queue" = queue.Queue()

def log_event(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}")


# --- Log file -----------------------------------------------------------------
# Everything printed (log_event, print, tracebacks) is also written to
# logs/bman_<date>_<time>.log next to the app, one file per run.
LOG_DIR = CORE_DIR / "logs"
LOG_FILES_TO_KEEP = 30
LOG_FILE_PATH: Path | None = None
_LOG_FILE_LOCK = threading.Lock()
_LOG_FILE_HANDLE = None


class _LogFileTee:
    """Passes writes on to the original stream and also appends them to the log file."""

    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, message):
        if self.original_stream:
            try:
                self.original_stream.write(message)
            except Exception:
                pass
        if message and _LOG_FILE_HANDLE is not None:
            with _LOG_FILE_LOCK:
                try:
                    _LOG_FILE_HANDLE.write(message)
                    _LOG_FILE_HANDLE.flush()
                except Exception:
                    pass
        return len(message) if message else 0

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _prune_old_logs() -> None:
    try:
        logs = sorted(LOG_DIR.glob("bman_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in logs[LOG_FILES_TO_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def _log_uncaught(exc_type, exc, tb, where: str = "") -> None:
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log_event(f"UNEXPECTED ERROR{(' in ' + where) if where else ''}:\n{text.rstrip()}")


def start_file_logging() -> Path | None:
    """Start writing the log to a file. Safe to call more than once."""
    global LOG_FILE_PATH, _LOG_FILE_HANDLE
    if _LOG_FILE_HANDLE is not None:
        return LOG_FILE_PATH
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _prune_old_logs()
        LOG_FILE_PATH = LOG_DIR / f"bman_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log"
        _LOG_FILE_HANDLE = open(LOG_FILE_PATH, "a", encoding="utf-8", errors="replace")
    except OSError:
        LOG_FILE_PATH = None
        _LOG_FILE_HANDLE = None
        return None

    sys.stdout = _LogFileTee(sys.stdout)
    sys.stderr = _LogFileTee(sys.stderr)

    # Crashes in the main thread or any worker thread end up in the log too.
    sys.excepthook = lambda t, e, tb: _log_uncaught(t, e, tb)
    threading.excepthook = lambda a: _log_uncaught(
        a.exc_type, a.exc_value, a.exc_traceback,
        where=f"thread '{getattr(a.thread, 'name', '?')}'")

    log_event(f"Bman Downloader started - logging to {LOG_FILE_PATH}")
    return LOG_FILE_PATH


start_file_logging()

UI_PUMP_MS = 60
UI_PUMP_BUDGET = 300
UI_PENDING_CAP = 5000

_UI_PENDING: "OrderedDict" = OrderedDict()
_UI_LOCK = threading.Lock()
_UI_SEQ = itertools.count()

def post_ui(func, *args, **kwargs) -> None:
    if func is None:
        return
    with _UI_LOCK:
        if len(_UI_PENDING) >= UI_PENDING_CAP:
            return
        _UI_PENDING[("once", next(_UI_SEQ))] = (func, args, kwargs)

def post_ui_latest(slot, func, *args, **kwargs) -> None:
    if func is None:
        return
    with _UI_LOCK:
        _UI_PENDING.pop(slot, None)
        _UI_PENDING[slot] = (func, args, kwargs)

def drop_ui_slot(slot) -> None:
    with _UI_LOCK:
        _UI_PENDING.pop(slot, None)

def _drain_ui_pending() -> list:
    with _UI_LOCK:
        keys = list(_UI_PENDING.keys())[:UI_PUMP_BUDGET]
        return [(key, _UI_PENDING.pop(key)) for key in keys]

_BULK_STATUS_SLOT = ("bulk-status",)

class TextRedirector:
    def __init__(self, text_widget, original_stream):
        self.text_widget = text_widget
        self.original_stream = original_stream

    def write(self, message):
        if self.original_stream:
            try:
                self.original_stream.write(message)
            except Exception:
                pass
        if message:
            LOG_QUEUE.put(message)

    def flush(self):
        if self.original_stream:
            try:
                self.original_stream.flush()
            except Exception:
                pass

class ArchiveError(Exception): pass
class IndexLoadError(ArchiveError): pass
class DownloadError(ArchiveError): pass
class _TransientDownloadError(Exception):
    pass

@dataclass(frozen=True)
class Files:
    id: str
    name: str
    size: int | None
    date: str | None
    source_type: str = "archive"
    archive_identifier: str | None = None
    allow_iso_conversion: bool = True
    content_source: bool = False

def format_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"

def grab_archive_credentials() -> dict:
    creds_path = CORE_DIR / CREDS_FILE
    if creds_path.exists():
        try:
            data = json.loads(creds_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (data.get("cookies") or data.get("s3")):
                return {
                    "cookies": data.get("cookies") or [],
                    "s3": data.get("s3") or {},
                    "screenname": data.get("screenname", ""),
                }
        except Exception:
            pass
    return {"cookies": [], "s3": {}, "screenname": ""}

def has_archive_keys() -> bool:
    s3 = grab_archive_credentials().get("s3") or {}
    return bool(s3.get("access") and s3.get("secret"))

def load_settings() -> dict:
    try:
        data = json.loads((CORE_DIR / SETTINGS_FILE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def save_settings(data: dict) -> None:
    try:
        (CORE_DIR / SETTINGS_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Could not save settings: {exc}")


DELETE_AFTER_UPLOAD_KEY = "delete_after_upload"   # settings.json: delete local copies once uploaded


def delete_after_upload_enabled() -> bool:
    return bool(load_settings().get(DELETE_AFTER_UPLOAD_KEY, False))


def _inside(folder: Path, path: Path) -> bool:
    try:
        return folder.resolve() in path.resolve().parents
    except OSError:
        return False


def delete_local_copies(game_name: str, archive_path: Path | None = None) -> bool:
    """After a successful upload: delete the game's download in downloads/ and its folder in extracted/.

    Only ever touches files directly inside those two folders. Returns True if everything
    that existed was deleted; failures are logged.
    """
    targets: list[Path] = []
    if archive_path is not None:
        targets.append(Path(archive_path))
    try:
        for p in DOWNLOAD_DIR.iterdir():
            if p.is_file() and p.stem == game_name:     # exact name match only
                targets.append(p)
    except OSError:
        pass
    game_dir = EXTRACT_DIR / game_name

    all_ok = True
    seen: set = set()
    for path in targets:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if not path.exists() or not _inside(DOWNLOAD_DIR, path):
            continue
        try:
            _retry_os(path.unlink, tries=8)
            log_event(f"  Deleted after upload: {path}")
        except OSError as exc:
            all_ok = False
            log_event(f"  Could not delete {path} after upload: {exc}")

    if game_dir.exists() and _inside(EXTRACT_DIR, game_dir):
        try:
            shutil.rmtree(game_dir)
            log_event(f"  Deleted after upload: {game_dir}")
        except OSError as exc:
            all_ok = False
            log_event(f"  Could not fully delete {game_dir} after upload: {exc}")
    return all_ok


FTP_STATE = {"online": False}

COLLECTIONS_FILE = "collections.json"
COLLECTION_EXPORT_EXT = ".bmc"
COLLECTION_FORMAT = "bman-collection"
COLLECTION_FORMAT_VERSION = 1


@dataclass
class CollectionGame:
    name: str
    archive_identifier: str | None = None
    size: int | None = None
    date: str | None = None
    source_type: str = "archive"
    allow_iso_conversion: bool = True
    content_source: bool = False

    @classmethod
    def from_entry(cls, entry) -> "CollectionGame":
        return cls(
            name=entry.name,
            archive_identifier=getattr(entry, "archive_identifier", None),
            size=getattr(entry, "size", None),
            date=getattr(entry, "date", None),
            source_type=getattr(entry, "source_type", "archive"),
            allow_iso_conversion=bool(getattr(entry, "allow_iso_conversion", True)),
            content_source=bool(getattr(entry, "content_source", False)),
        )

    def to_files(self) -> Files:
        return Files(
            id=self.name,
            name=self.name,
            size=self.size,
            date=self.date,
            source_type=self.source_type or "archive",
            archive_identifier=self.archive_identifier,
            allow_iso_conversion=bool(self.allow_iso_conversion),
            content_source=bool(self.content_source),
        )

    def to_dict(self) -> dict:
        # Written out by hand, in field order: asdict() deep-copies every value,
        # which was most of the time spent saving or comparing a big collection.
        return {
            "name": self.name,
            "archive_identifier": self.archive_identifier,
            "size": self.size,
            "date": self.date,
            "source_type": self.source_type,
            "allow_iso_conversion": self.allow_iso_conversion,
            "content_source": self.content_source,
        }

    def copy(self) -> "CollectionGame":
        return CollectionGame(self.name, self.archive_identifier, self.size, self.date,
                              self.source_type, self.allow_iso_conversion, self.content_source)

    @classmethod
    def from_dict(cls, data: dict) -> "CollectionGame | None":
        if not isinstance(data, dict):
            return None
        name = (data.get("name") or "").strip()
        if not name:
            return None
        known = _COLLECTION_GAME_FIELDS
        clean = {k: v for k, v in data.items() if k in known}
        clean["name"] = name
        try:
            return cls(**clean)
        except Exception:
            return cls(name=name, archive_identifier=data.get("archive_identifier"))


_COLLECTION_GAME_FIELDS = frozenset(f.name for f in dataclass_fields(CollectionGame))


# What a collection item is. Themes and gamer pictures ("profile pics") come
# from their own archive.org items (see CATALOGS); everything else is a game.
ITEM_KINDS = ("games", "themes", "gamerpics")
ITEM_KIND_LABELS = {"games": "Games", "themes": "Themes", "gamerpics": "Profile Pics"}
_ITEM_KIND_NOUNS = {"games": ("game", "games"), "themes": ("theme", "themes"),
                    "gamerpics": ("profile pic pack", "profile pic packs")}


def item_kind(item) -> str:
    """'games', 'themes' or 'gamerpics' for a Files entry or a CollectionGame."""
    cat = catalog_for_entry(item)
    return cat.key if cat is not None and cat.key in ITEM_KINDS else "games"


def count_item_kinds(items) -> dict:
    counts = {kind: 0 for kind in ITEM_KINDS}
    for item in items:
        counts[item_kind(item)] += 1
    return counts


def describe_item_counts(counts: dict, empty: str = "empty") -> str:
    """{'games': 3, 'themes': 1, 'gamerpics': 0} -> '3 games, 1 theme'."""
    parts = []
    for kind in ITEM_KINDS:
        n = int(counts.get(kind) or 0)
        if n:
            singular, plural = _ITEM_KIND_NOUNS[kind]
            parts.append(f"{n:,} {singular if n == 1 else plural}")
    return ", ".join(parts) or empty


def item_label(item) -> str:
    """How an item is listed inside a collection."""
    size = format_size(getattr(item, "size", None))
    name = getattr(item, "name", "") or ""
    if item_kind(item) != "games":
        title_id = catalog_title_id(name)          # tells same-named packs apart
        return f"{catalog_display_name(name)}{f'  [{title_id}]' if title_id else ''}  ({size})"
    source = getattr(item, "archive_identifier", None)
    return f"{f'[{source}] ' if source else ''}{name}  ({size})"


@dataclass
class GameCollection:
    name: str
    description: str = ""
    created: str = ""
    updated: str = ""
    games: list = field(default_factory=list)
    shared_id: str = ""

    def has(self, game_name: str) -> bool:
        target = (game_name or "").lower()
        return any((g.name or "").lower() == target for g in self.games)

    def name_set(self) -> set:
        """Lower-cased names of the games in here, for checking many names at once."""
        return {(g.name or "").lower() for g in self.games}

    def add(self, entry) -> bool:
        if self.has(getattr(entry, "name", "")):
            return False
        self.games.append(CollectionGame.from_entry(entry))
        self.touch()
        return True

    def add_many(self, entries) -> int:
        """Add every entry that is not in here yet; returns how many were added."""
        present = self.name_set()
        added = 0
        for entry in entries:
            key = (getattr(entry, "name", "") or "").lower()
            if key in present:
                continue
            present.add(key)
            self.games.append(CollectionGame.from_entry(entry))
            added += 1
        if added:
            self.touch()
        return added

    def copy(self) -> "GameCollection":
        return GameCollection(self.name, self.description, self.created, self.updated,
                              [g.copy() for g in self.games], self.shared_id)

    def remove(self, game_name: str) -> bool:
        target = (game_name or "").lower()
        before = len(self.games)
        self.games = [g for g in self.games if (g.name or "").lower() != target]
        if len(self.games) != before:
            self.touch()
            return True
        return False

    def kind_counts(self) -> dict:
        """How many games, themes and profile pic packs are in here."""
        return count_item_kinds(self.games)

    def only(self, kind: str | None) -> "GameCollection":
        """A copy holding just one kind of item ('games' / 'themes' / 'gamerpics');
        kind None or 'all' keeps everything."""
        copy = self.copy()
        if kind in ITEM_KINDS:
            copy.games = [g for g in copy.games if item_kind(g) == kind]
        return copy

    def total_size(self) -> int | None:
        sizes = [g.size for g in self.games if isinstance(g.size, int)]
        if not sizes:
            return None
        return sum(sizes)

    def touch(self) -> None:
        self.updated = time.strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "created": self.created,
            "updated": self.updated,
            "shared_id": self.shared_id,
            "games": [g.to_dict() for g in self.games],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GameCollection | None":
        if not isinstance(data, dict):
            return None
        name = (data.get("name") or "").strip()
        if not name:
            return None
        games = []
        for raw in data.get("games") or []:
            game = CollectionGame.from_dict(raw)
            if game is not None:
                games.append(game)
        return cls(
            name=name,
            description=str(data.get("description") or ""),
            created=str(data.get("created") or ""),
            updated=str(data.get("updated") or ""),
            shared_id=str(data.get("shared_id") or ""),
            games=games,
        )


def _collections_path() -> Path:
    return CORE_DIR / COLLECTIONS_FILE


# The parsed collections file, kept until the file changes. Every screen that
# touches collections reads the whole file, often several times for one click,
# so re-parsing it each time was a large part of what made those screens slow.
# Callers always get their own copies, so nothing they do can change the cache.
_COLLECTIONS_LOCK = threading.RLock()
_COLLECTIONS_CACHE = {"stamp": None, "items": []}


def _collections_stamp():
    try:
        info = _collections_path().stat()
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None


def _read_collections_file() -> list:
    try:
        raw = json.loads(_collections_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("collections") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out = []
    for entry in items:
        collection = GameCollection.from_dict(entry)
        if collection is not None:
            out.append(collection)
    return out


def load_collections() -> list:
    with _COLLECTIONS_LOCK:
        stamp = _collections_stamp()
        if stamp is None or stamp != _COLLECTIONS_CACHE["stamp"]:
            _COLLECTIONS_CACHE["stamp"] = None
            _COLLECTIONS_CACHE["items"] = _read_collections_file()
            _COLLECTIONS_CACHE["stamp"] = stamp
        return [c.copy() for c in _COLLECTIONS_CACHE["items"]]


def _encode_collections(collections: list) -> str:
    """The collections file as JSON: indented like before, but one line per game.

    json.dumps(indent=2) cannot use Python's fast encoder; on a library of a few
    thousand games it took longer than everything else a save does put together.
    """
    dumps = json.dumps
    blocks = []
    for collection in collections:
        body = collection.to_dict()
        games = body.pop("games")
        lines = ["    {"]
        for key, value in body.items():
            lines.append(f"      {dumps(key)}: {dumps(value)},")
        if games:
            lines.append('      "games": [')
            lines.append(",\n".join("        " + dumps(game) for game in games))
            lines.append("      ]")
        else:
            lines.append('      "games": []')
        lines.append("    }")
        blocks.append("\n".join(lines))
    inner = ("[\n" + ",\n".join(blocks) + "\n  ]") if blocks else "[]"
    return ("{\n"
            f'  "format": {dumps(COLLECTION_FORMAT)},\n'
            f'  "version": {dumps(COLLECTION_FORMAT_VERSION)},\n'
            f'  "collections": {inner}\n'
            "}")


def save_collections(collections: list) -> None:
    with _COLLECTIONS_LOCK:
        path = _collections_path()
        tmp = path.with_suffix(".tmp")
        # Whatever happens here, the next load reads the file again rather than
        # trusting what was handed in: reading also tidies the data (it drops
        # games without a name, for one), and the file is the truth.
        _COLLECTIONS_CACHE["stamp"] = None
        tmp.write_text(_encode_collections(collections), encoding="utf-8")
        tmp.replace(path)


def get_collection(name: str) -> GameCollection | None:
    target = (name or "").lower()
    for collection in load_collections():
        if collection.name.lower() == target:
            return collection
    return None


def upsert_collection(collection: GameCollection) -> None:
    collections = load_collections()
    for index, existing in enumerate(collections):
        if existing.name.lower() == collection.name.lower():
            collections[index] = collection
            break
    else:
        if not collection.created:
            collection.created = time.strftime("%Y-%m-%d %H:%M:%S")
        collections.append(collection)
    collection.touch()
    save_collections(collections)


def delete_collection(name: str) -> bool:
    collections = load_collections()
    remaining = [c for c in collections if c.name.lower() != (name or "").lower()]
    if len(remaining) == len(collections):
        return False
    save_collections(remaining)
    return True


def unique_collection_name(base: str) -> str:
    existing = {c.name.lower() for c in load_collections()}
    candidate = (base or "Imported Collection").strip() or "Imported Collection"
    if candidate.lower() not in existing:
        return candidate
    counter = 2
    while f"{candidate} ({counter})".lower() in existing:
        counter += 1
    return f"{candidate} ({counter})"


def safe_collection_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "_", name or "collection").strip() or "collection"
    return f"{cleaned}{COLLECTION_EXPORT_EXT}"


def export_collection(collection: GameCollection, destination: Path) -> Path:
    destination = Path(destination)
    body = collection.to_dict()
    body.pop("shared_id", None)
    payload = {
        "format": COLLECTION_FORMAT,
        "version": COLLECTION_FORMAT_VERSION,
        "app_version": APP_VERSION,
        "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
        "collection": body,
    }
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination


def read_collection_file(path: Path) -> GameCollection:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"That file isn't readable as a collection ({exc}).") from exc

    if isinstance(raw, dict) and "collection" in raw:
        version = raw.get("version") or 1
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = 1
        if version > COLLECTION_FORMAT_VERSION:
            raise ValueError(
                f"This collection was made by a newer version of Bman Downloader "
                f"(file version {version}, this build reads {COLLECTION_FORMAT_VERSION}). "
                f"Update from Settings and try again."
            )
        body = raw.get("collection")
    elif isinstance(raw, dict) and "games" in raw:
        body = raw
    elif isinstance(raw, list):
        body = {"name": path.stem, "games": raw}
    else:
        raise ValueError("That file doesn't look like a collection.")

    collection = GameCollection.from_dict(body if isinstance(body, dict) else {})
    if collection is None:
        raise ValueError("That collection file has no name in it.")
    if not collection.games:
        raise ValueError("That collection is empty - there's nothing to import.")
    collection.shared_id = ""
    return collection


def refresh_games_against_index(collection: GameCollection, index) -> int:
    if index is None:
        return 0
    lookup = {}
    try:
        for entry in index.entries:
            lookup.setdefault(entry.name.lower(), entry)
    except Exception:
        return 0
    # Themes and profile pic packs aren't in the game index; use their saved lists.
    for cat in CATALOGS:
        try:
            for entry in known_catalog_entries(cat):
                lookup.setdefault(entry.name.lower(), entry)
        except Exception:
            pass

    matched = 0
    for game in collection.games:
        entry = lookup.get((game.name or "").lower())
        if entry is None:
            continue
        matched += 1
        if game.size is None:
            game.size = entry.size
        if not game.date:
            game.date = entry.date
        if not game.archive_identifier:
            game.archive_identifier = entry.archive_identifier
        game.allow_iso_conversion = bool(entry.allow_iso_conversion)
        game.content_source = bool(entry.content_source)
    return matched



COMMUNITY_REPO_OWNER = "BmanGames024"
COMMUNITY_REPO_NAME = "bman-community"
COMMUNITY_BRANCH = "main"

COMMUNITY_INDEX_PATH = "/collections/index.json"
COMMUNITY_ITEM_DIR = "collections"
COMMUNITY_FORMAT = "bman-community-index"
COMMUNITY_FORMAT_VERSION = 1

COMMUNITY_API_BASE = f"https://api.github.com/repos/{COMMUNITY_REPO_OWNER}/{COMMUNITY_REPO_NAME}/contents"
COMMUNITY_RAW_BASE = (f"https://raw.githubusercontent.com/{COMMUNITY_REPO_OWNER}/"
                      f"{COMMUNITY_REPO_NAME}/{COMMUNITY_BRANCH}")

COMMUNITY_CACHE_FILE = "community_cache.json"
COMMUNITY_LISTS_DIR = "community_lists"      # game lists already fetched, one file per shared collection
COMMUNITY_CACHE_TTL_SECONDS = 5 * 60

COMMUNITY_MAX_GAMES = 5000
COMMUNITY_MAX_DESCRIPTION = 400
COMMUNITY_MAX_NAME = 80

COMMUNITY_SETTINGS_KEY = "community"
COMMUNITY_TIMEOUT = 20


COMMUNITY_TOKEN_PARTS = (
    "github_pat_11AODNBHQ08ot3K0wdpd",
    "Dt_bVRAxwrutgChJRlRdqbkxCxgSvPM",
    "oturyTtn7yVi9yD6WCELRMWa06afIM5",
)


class CommunityError(Exception):
    pass


class CommunityConflict(CommunityError):
    pass


def community_token() -> str:
    from_env = (os.environ.get("BMAN_COMMUNITY_TOKEN") or "").strip()
    if from_env:
        return from_env
    try:
        stored = (load_settings().get(COMMUNITY_SETTINGS_KEY) or {}).get("token")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    except Exception:
        pass
    return "".join(COMMUNITY_TOKEN_PARTS).strip()


def community_can_share() -> bool:
    return bool(community_token())


def _community_session():
    if curl_cffi_requests is None:
        raise CommunityError("The HTTP library isn't available, so the community list can't be reached.")
    return curl_cffi_requests.Session(impersonate="chrome124")


def _community_headers(token: str | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"BmanDownloader/{APP_VERSION}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _community_path(path: str) -> str:
    """'/collections/index.json' -> 'collections/index.json'. A leading slash made the
    API URL '.../contents//collections/index.json', which GitHub won't write to."""
    return quote((path or "").strip().lstrip("/"))


def _github_message(resp) -> str:
    """GitHub's own explanation of a failed request, for the log."""
    try:
        data = resp.json()
        message = str(data.get("message") or "").strip()
        errors = data.get("errors")
        if errors:
            message += f" {errors}"
        return message[:300]
    except Exception:
        return ""


def _community_api_read(path: str) -> tuple:
    session = None
    try:
        session = _community_session()
        # The cache buster and no-cache header make sure this is the file as it is
        # right now: an older copy has an old sha, and writing with it always fails.
        url = (f"{COMMUNITY_API_BASE}/{_community_path(path)}?ref={quote(COMMUNITY_BRANCH)}"
               f"&_={time.time_ns()}")
        headers = _community_headers(community_token())
        headers["Cache-Control"] = "no-cache"
        resp = session.get(url, headers=headers, timeout=COMMUNITY_TIMEOUT)
        if resp.status_code == 404:
            return None, None
        if resp.status_code in (401, 403):
            raise CommunityError(f"GitHub refused the request (HTTP {resp.status_code}).")
        if resp.status_code != 200:
            raise CommunityError(f"GitHub returned HTTP {resp.status_code}.")
        body = resp.json()
        sha = body.get("sha")
        encoded = body.get("content")
        if encoded:
            raw = base64.b64decode(encoded)
        else:
            # Files over 1 MB come back without their content (a big shared
            # collection). Ask for the raw bytes instead - still the live file.
            headers["Accept"] = "application/vnd.github.raw"
            raw_resp = session.get(url, headers=headers, timeout=COMMUNITY_TIMEOUT)
            if raw_resp.status_code != 200:
                raise CommunityError(f"GitHub returned HTTP {raw_resp.status_code} for a large file.")
            raw = raw_resp.content
        return json.loads(raw.decode("utf-8")), sha
    except CommunityError:
        raise
    except Exception as exc:
        raise CommunityError(f"Couldn't reach the community list ({exc}).") from exc
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _community_raw_read(path: str):
    session = None
    try:
        session = _community_session()
        url = f"{COMMUNITY_RAW_BASE}/{_community_path(path)}?cb={int(time.time())}"
        resp = session.get(url, headers={"User-Agent": f"BmanDownloader/{APP_VERSION}"},
                           timeout=COMMUNITY_TIMEOUT)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CommunityError(f"GitHub returned HTTP {resp.status_code}.")
        return resp.json()
    except CommunityError:
        raise
    except Exception as exc:
        raise CommunityError(f"Couldn't reach the community list ({exc}).") from exc
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def community_read_json(path: str, need_sha: bool = False) -> tuple:
    try:
        return _community_api_read(path)
    except CommunityError as exc:
        if need_sha:
            raise
        log_event(f"Community: API read of {path} failed ({exc}); falling back to raw.")
        return _community_raw_read(path), None


def _community_write_json(path: str, payload: dict, sha: str | None, message: str) -> str:
    token = community_token()
    if not token:
        raise CommunityError("This build has no community write token, so it can't share collections.")

    body = {
        "message": message,
        "branch": COMMUNITY_BRANCH,
        "content": base64.b64encode(
            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        body["sha"] = sha

    session = None
    try:
        session = _community_session()
        url = f"{COMMUNITY_API_BASE}/{_community_path(path)}"
        resp = session.put(url, json=body, headers=_community_headers(token),
                           timeout=COMMUNITY_TIMEOUT)
        if resp.status_code in (200, 201):
            try:
                return (resp.json().get("content") or {}).get("sha") or ""
            except Exception:
                return ""
        detail = _github_message(resp)
        # Only these mean "the file changed since it was read" - worth a re-read and retry.
        # Any other 422 (bad path, content too big, ...) would fail the same way every time.
        if resp.status_code == 409 or (resp.status_code == 422 and "sha" in detail.lower()):
            raise CommunityConflict(f"The file changed since it was read ({detail or resp.status_code}).")
        if resp.status_code in (401, 403):
            raise CommunityError(
                "GitHub rejected the write. The sharing token for this build has "
                "probably expired or been revoked - browsing still works, and an "
                f"app update will fix sharing. ({detail or resp.status_code})")
        raise CommunityError(f"GitHub returned HTTP {resp.status_code} while sharing"
                             f"{f': {detail}' if detail else '.'}")
    except CommunityError:
        raise
    except Exception as exc:
        raise CommunityError(f"Couldn't publish to the community list ({exc}).") from exc
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _community_delete(path: str, sha: str, message: str) -> None:
    token = community_token()
    if not token:
        raise CommunityError("This build has no community write token.")
    session = None
    try:
        session = _community_session()
        url = f"{COMMUNITY_API_BASE}/{_community_path(path)}"
        resp = session.request(
            "DELETE", url,
            json={"message": message, "branch": COMMUNITY_BRANCH, "sha": sha},
            headers=_community_headers(token), timeout=COMMUNITY_TIMEOUT,
        )
        if resp.status_code not in (200, 204):
            detail = _github_message(resp)
            raise CommunityError(f"GitHub returned HTTP {resp.status_code} while removing the file"
                                 f"{f': {detail}' if detail else '.'}")
    except CommunityError:
        raise
    except Exception as exc:
        raise CommunityError(f"Couldn't remove that community file ({exc}).") from exc
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


@dataclass
class CommunityEntry:
    id: str
    name: str
    description: str = ""
    games: int = 0
    size: int | None = None
    shared: str = ""
    updated: str = ""
    app_version: str = ""
    # "games" above is every item (older app versions read it that way);
    # these say how many of them are themes / profile pic packs.
    themes: int = 0
    gamerpics: int = 0

    def kind_counts(self) -> dict:
        themes, pics = max(self.themes, 0), max(self.gamerpics, 0)
        return {"games": max(self.games - themes - pics, 0), "themes": themes, "gamerpics": pics}

    @property
    def path(self) -> str:
        return f"{COMMUNITY_ITEM_DIR}/{self.id}.json"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CommunityEntry | None":
        if not isinstance(data, dict):
            return None
        entry_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        if not entry_id or not name:
            return None
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", entry_id):
            return None
        try:
            games = int(data.get("games") or 0)
        except (TypeError, ValueError):
            games = 0
        size = data.get("size")
        if not isinstance(size, int):
            size = None

        def count(key):
            try:
                return max(int(data.get(key) or 0), 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            id=entry_id,
            name=name[:COMMUNITY_MAX_NAME],
            description=str(data.get("description") or "")[:COMMUNITY_MAX_DESCRIPTION],
            games=max(games, 0),
            size=size,
            shared=str(data.get("shared") or ""),
            updated=str(data.get("updated") or ""),
            app_version=str(data.get("app_version") or ""),
            themes=count("themes"),
            gamerpics=count("gamerpics"),
        )


def _community_cache_path() -> Path:
    return CORE_DIR / COMMUNITY_CACHE_FILE


def load_community_cache() -> tuple:
    try:
        raw = json.loads(_community_cache_path().read_text(encoding="utf-8"))
        fetched = float(raw.get("fetched") or 0)
        entries = []
        for row in raw.get("collections") or []:
            entry = CommunityEntry.from_dict(row)
            if entry is not None:
                entries.append(entry)
        return entries, max(time.time() - fetched, 0.0)
    except Exception:
        return [], float("inf")


def save_community_cache(entries: list) -> None:
    try:
        _community_cache_path().write_text(json.dumps({
            "fetched": time.time(),
            "collections": [e.to_dict() for e in entries],
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def community_entry_stamp(entry: CommunityEntry) -> str:
    """Changes whenever the shared collection is updated; what a saved game list is checked against."""
    return entry.updated or entry.shared or ""


def _community_list_path(entry_id: str) -> Path:
    return CORE_DIR / COMMUNITY_LISTS_DIR / f"{entry_id}.json"


def load_cached_community_collection(entry: CommunityEntry) -> GameCollection | None:
    """The game list of a shared collection if this PC already fetched this version of it."""
    stamp = community_entry_stamp(entry)
    if not stamp:
        return None
    try:
        raw = json.loads(_community_list_path(entry.id).read_text(encoding="utf-8"))
        if raw.get("stamp") != stamp:
            return None
        collection = GameCollection.from_dict(raw.get("collection"))
    except Exception:
        return None
    if collection is None or not collection.games:
        return None
    collection.shared_id = ""
    return collection


def save_cached_community_collection(entry: CommunityEntry, collection: GameCollection) -> None:
    stamp = community_entry_stamp(entry)
    if not stamp:
        return
    try:
        path = _community_list_path(entry.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"stamp": stamp, "collection": collection.to_dict()}),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def prune_community_list_cache(live_ids) -> None:
    """Drop saved game lists of collections that are no longer shared."""
    live = set(live_ids)
    try:
        for path in (CORE_DIR / COMMUNITY_LISTS_DIR).iterdir():
            if path.suffix in (".json", ".tmp") and path.stem not in live:
                try:
                    path.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _parse_community_index(data) -> list:
    rows = data.get("collections") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out = []
    seen = set()
    for row in rows:
        entry = CommunityEntry.from_dict(row)
        if entry is None or entry.id in seen:
            continue
        seen.add(entry.id)
        out.append(entry)
    out.sort(key=lambda e: (e.updated or e.shared or ""), reverse=True)
    return out


def fetch_community_index(need_sha: bool = False) -> tuple:
    data, sha = community_read_json(COMMUNITY_INDEX_PATH, need_sha=need_sha)
    if data is None:
        return [], sha
    return _parse_community_index(data), sha


def fetch_community_collection(entry: CommunityEntry) -> GameCollection:
    data, _ = community_read_json(entry.path)
    if data is None:
        raise CommunityError(
            f"'{entry.name}' is listed but its file is missing from the community "
            f"repo. It was probably removed - hit Refresh.")
    body = data.get("collection") if isinstance(data, dict) and "collection" in data else data
    collection = GameCollection.from_dict(body if isinstance(body, dict) else {})
    if collection is None:
        raise CommunityError(f"'{entry.name}' couldn't be read - the file looks damaged.")
    if not collection.games:
        raise CommunityError(f"'{entry.name}' is empty - there's nothing in it to download.")
    collection.shared_id = ""
    # Only keep it for next time when it is certainly the version the list
    # describes; a copy served from GitHub's cache can lag behind an update.
    if isinstance(data, dict) and data.get("shared") and data.get("shared") == entry.updated:
        save_cached_community_collection(entry, collection)
    return collection


def make_community_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]
    return f"{slug or 'collection'}-{os.urandom(4).hex()}"


def _community_write_item(path: str, payload: dict, message: str, attempts: int = 3) -> None:
    for attempt in range(attempts):
        sha = None
        try:
            _, sha = community_read_json(path, need_sha=True)
        except CommunityError:
            sha = None
        try:
            _community_write_json(path, payload, sha, message)
            return
        except CommunityConflict as exc:
            log_event(f"Community: {path} changed under us, retrying ({attempt + 1}/{attempts}) - {exc}")
            time.sleep(0.5 + attempt * 0.7 + random.random() * 0.5)
    raise CommunityError(
        "GitHub is busy right now and that collection couldn't be written. "
        "Give it a moment and try again.")


def _community_rewrite_index(mutate, message: str, attempts: int = 4) -> list:
    for attempt in range(attempts):
        entries, sha = fetch_community_index(need_sha=True)
        updated = mutate(list(entries))
        if updated is None:
            return entries
        payload = {
            "format": COMMUNITY_FORMAT,
            "version": COMMUNITY_FORMAT_VERSION,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "collections": [e.to_dict() for e in updated],
        }
        try:
            _community_write_json(COMMUNITY_INDEX_PATH, payload, sha, message)
            return updated
        except CommunityConflict as exc:
            log_event(f"Community: index changed under us, retrying ({attempt + 1}/{attempts}) - {exc}")
            time.sleep(0.7 + attempt * 0.8 + random.random() * 0.7)
    raise CommunityError(
        "The community list is busy right now - a few people shared at once. "
        "Give it a moment and try again.")


def publish_community_collection(collection: GameCollection) -> CommunityEntry:
    if not collection.games:
        raise CommunityError("There's nothing in that collection to share.")
    if len(collection.games) > COMMUNITY_MAX_GAMES:
        raise CommunityError(
            f"That collection has {len(collection.games):,} items in it. The community "
            f"list caps entries at {COMMUNITY_MAX_GAMES:,} - split it up and share the parts.")

    name = (collection.name or "").strip()[:COMMUNITY_MAX_NAME]
    if not name:
        raise CommunityError("That collection needs a name before it can be shared.")

    existing_id = (collection.shared_id or "").strip()
    is_update = bool(existing_id)
    entry_id = existing_id or make_community_id(name)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    body = collection.to_dict()
    body.pop("shared_id", None)
    body["name"] = name
    body["description"] = (collection.description or "")[:COMMUNITY_MAX_DESCRIPTION]

    payload = {
        "format": COMMUNITY_FORMAT,
        "version": COMMUNITY_FORMAT_VERSION,
        "app_version": APP_VERSION,
        "shared": now,
        "collection": body,
    }

    item_path = f"{COMMUNITY_ITEM_DIR}/{entry_id}.json"
    _community_write_item(
        item_path, payload,
        f"{'Update' if is_update else 'Share'} collection: {name}",
    )

    kinds = collection.kind_counts()
    entry = CommunityEntry(
        id=entry_id,
        name=name,
        description=body["description"],
        games=len(collection.games),
        size=collection.total_size(),
        shared=now,
        updated=now,
        app_version=APP_VERSION,
        themes=kinds["themes"],
        gamerpics=kinds["gamerpics"],
    )

    def mutate(entries: list) -> list:
        kept = [e for e in entries if e.id != entry_id]
        if is_update:
            for old in entries:
                if old.id == entry_id and old.shared:
                    entry.shared = old.shared
                    break
        kept.insert(0, entry)
        return kept

    try:
        _community_rewrite_index(
            mutate, f"{'Update' if is_update else 'Add'} index entry: {name}")
    except CommunityError:
        if not is_update:
            # The list never got the entry, so nobody can see this file - take it back down.
            try:
                _, item_sha = community_read_json(item_path, need_sha=True)
                if item_sha:
                    _community_delete(item_path, item_sha, f"Remove unlisted share: {name}")
            except Exception as exc:
                log_event(f"Community: couldn't remove the unlisted file {item_path}: {exc}")
        raise
    return entry


def unshare_community_collection(shared_id: str) -> bool:
    shared_id = (shared_id or "").strip()
    if not shared_id:
        return False

    removed = {"hit": False}

    def mutate(entries: list):
        kept = [e for e in entries if e.id != shared_id]
        if len(kept) == len(entries):
            return None
        removed["hit"] = True
        return kept

    _community_rewrite_index(mutate, f"Remove index entry: {shared_id}")

    path = f"{COMMUNITY_ITEM_DIR}/{shared_id}.json"
    try:
        _, sha = community_read_json(path, need_sha=True)
        if sha:
            _community_delete(path, sha, f"Remove collection file: {shared_id}")
    except CommunityError as exc:
        log_event(f"Community: index row for {shared_id} removed but the file wasn't ({exc}).")
    return removed["hit"]


APP_WINDOW: dict = {"ref": None}


class FtpTransferCancelled(Exception):
    pass

@dataclass
class FtpConfig:
    host: str = ""
    port: int = DEFAULT_FTP_PORT
    user: str = DEFAULT_FTP_USER
    password: str = DEFAULT_FTP_PASS
    timeout: int = DEFAULT_FTP_TIMEOUT
    default_path: str = "/"
    max_parallel: int = DEFAULT_FTP_PARALLEL

    def is_configured(self) -> bool:
        return bool((self.host or "").strip())

    def worker_count(self) -> int:
        try:
            return max(1, min(MAX_FTP_PARALLEL, int(self.max_parallel)))
        except Exception:
            return DEFAULT_FTP_PARALLEL

def load_ftp_config() -> FtpConfig:
    raw = load_settings().get(FTP_SETTINGS_KEY)
    if not isinstance(raw, dict):
        return FtpConfig()
    def _int(value, fallback):
        try:
            return int(value)
        except Exception:
            return fallback
    return FtpConfig(
        host=str(raw.get("host", "") or "").strip(),
        port=_int(raw.get("port"), DEFAULT_FTP_PORT),
        user=str(raw.get("user", DEFAULT_FTP_USER) or ""),
        password=str(raw.get("password", DEFAULT_FTP_PASS) or ""),
        timeout=_int(raw.get("timeout"), DEFAULT_FTP_TIMEOUT),
        default_path=str(raw.get("default_path", "/") or "/"),
        max_parallel=max(1, min(MAX_FTP_PARALLEL, _int(raw.get("max_parallel"), DEFAULT_FTP_PARALLEL))),
    )

def save_ftp_config(cfg: FtpConfig) -> None:
    data = load_settings()
    data[FTP_SETTINGS_KEY] = asdict(cfg)
    save_settings(data)

def ftp_normalize_path(path: str) -> str:
    text = (path or "/").replace("\\", "/").strip()
    while "//" in text:
        text = text.replace("//", "/")
    if not text.startswith("/"):
        text = "/" + text
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text or "/"

def ftp_join(base: str, *parts) -> str:
    pieces = [str(p) for p in parts if str(p)]
    if not pieces:
        return ftp_normalize_path(base)
    return ftp_normalize_path(ftp_normalize_path(base) + "/" + "/".join(pieces))

def ftp_parent(path: str) -> str:
    normalized = ftp_normalize_path(path)
    if normalized == "/":
        return "/"
    return ftp_normalize_path(normalized.rsplit("/", 1)[0] or "/")

# Data-connection socket buffer. Python's default (64 KB on Windows) keeps too little
# data in flight to the console; FileZilla uses large buffers too.
FTP_SOCKET_BUFFER = 4 * 1024 * 1024
# How long to wait for a passive data connection before falling back to active mode.
FTP_PASSIVE_CONNECT_TIMEOUT = 5

# Hosts where passive mode did not work this session; they use active mode instead.
_FTP_ACTIVE_ONLY_HOSTS: set[str] = set()
_FTP_MODE_LOCK = threading.Lock()


def _ftp_tune_socket(sock) -> None:
    for option, value in ((socket.SO_SNDBUF, FTP_SOCKET_BUFFER), (socket.SO_RCVBUF, FTP_SOCKET_BUFFER)):
        try:
            sock.setsockopt(socket.SOL_SOCKET, option, value)
        except OSError:
            pass
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass


class _BmanFTP(ftplib.FTP):
    """ftplib.FTP with bigger data-socket buffers and passive mode that falls back to active.

    Passive mode (what FileZilla uses by default) is tried first. If the console
    refuses PASV or the data connection cannot be opened, this connection and every
    later one to the same host switch to active mode for the rest of the session.
    """

    def makeport(self):
        sock = super().makeport()
        _ftp_tune_socket(sock)          # accepted data sockets inherit the buffer sizes
        return sock

    def ntransfercmd(self, cmd, rest=None):
        if not self.passiveserver:
            conn, size = super().ntransfercmd(cmd, rest)
            _ftp_tune_socket(conn)
            return conn, size

        # Passive. Only a failed PASV or a data connection that cannot be opened
        # switches to active mode - not errors from the command itself.
        try:
            host, port = self.makepasv()
            conn = socket.create_connection((host, port), FTP_PASSIVE_CONNECT_TIMEOUT,
                                            source_address=self.source_address)
        except (OSError, ftplib.error_perm, ftplib.error_reply, ftplib.error_temp) as exc:
            with _FTP_MODE_LOCK:
                first = self.host not in _FTP_ACTIVE_ONLY_HOSTS
                _FTP_ACTIVE_ONLY_HOSTS.add(self.host)
            if first:
                log_event(f"FTP: passive mode failed on {self.host} ({str(exc).strip() or type(exc).__name__}) "
                          f"- using active mode instead.")
            self.passiveserver = False
            return self.ntransfercmd(cmd, rest)

        try:
            conn.settimeout(self.timeout)
            _ftp_tune_socket(conn)
            if rest is not None:
                self.sendcmd("REST %s" % rest)
            resp = self.sendcmd(cmd)
            if resp[0] == "2":
                resp = self.getresp()
            if resp[0] != "1":
                raise ftplib.error_reply(resp)
        except BaseException:
            conn.close()
            raise
        size = None
        if resp[:3] == "150":
            size = ftplib.parse150(resp)
        return conn, size


def ftp_mode_name(cfg: FtpConfig) -> str:
    return "active mode" if (cfg.host or "").strip() in _FTP_ACTIVE_ONLY_HOSTS else "passive mode"


def ftp_connect(cfg: FtpConfig) -> ftplib.FTP:
    if not cfg.is_configured():
        raise ArchiveError("No FTP host set. Fill in the FTP section in Settings first.")

    ftp = _BmanFTP()
    try:
        ftp.encoding = "latin-1"
    except Exception:
        pass

    timeout = max(5, int(cfg.timeout or DEFAULT_FTP_TIMEOUT))
    ftp.connect(cfg.host.strip(), int(cfg.port or DEFAULT_FTP_PORT), timeout=timeout)
    ftp.login(cfg.user or "", cfg.password or "")

    with _FTP_MODE_LOCK:
        ftp.set_pasv(ftp.host not in _FTP_ACTIVE_ONLY_HOSTS)

    try:
        ftp.sendcmd("TYPE I")
    except Exception:
        pass
    return ftp

def ftp_test_connection(cfg: FtpConfig) -> tuple[bool, str]:
    if not cfg.is_configured():
        return False, "No FTP host set."
    ftp = None
    try:
        ftp = ftp_connect(cfg)
        try:
            where = ftp.pwd()
        except Exception:
            where = "/"
        return True, f"Connected to {cfg.host} ({ftp_mode_name(cfg)}). Current folder: {where}"
    except ftplib.error_perm as exc:
        return False, f"Login rejected: {exc}"
    except socket.timeout:
        return False, f"Timed out connecting to {cfg.host}:{cfg.port}."
    except OSError as exc:
        return False, f"Could not reach {cfg.host}:{cfg.port} - {exc}"
    except Exception as exc:
        return False, f"FTP error: {exc}"
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

_UNIX_LIST_RE = re.compile(
    r"^(?P<perm>[dl\-][rwxsStTl\-]{9})\S*\s+.*?\s"
    r"(?:[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4})"
    r"|\d{4}-\d{2}-\d{2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)"
    r"\s+(?P<name>.+)$"
)

_DOS_LIST_RE = re.compile(
    r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s+\d{1,2}:\d{2}\s*(?:[AaPp][Mm])?\s+"
    r"(?P<kind><DIR>|\d+)\s+(?P<name>.+)$"
)


def _parse_list_line(line: str):
    text = (line or "").rstrip("\r\n")
    if not text.strip():
        return None
    if text.lower().startswith("total "):
        return None

    match = _DOS_LIST_RE.match(text.strip())
    if match:
        name = match.group("name").strip()
        if not name or name in (".", ".."):
            return None
        return name, match.group("kind").upper() == "<DIR>"

    if "<DIR>" in text.upper():
        idx = text.upper().index("<DIR>")
        name = text[idx + 5:].strip()
        return (name, True) if name and name not in (".", "..") else None

    match = _UNIX_LIST_RE.match(text)
    if match:
        name = match.group("name")
        perm = match.group("perm")
        if perm[0] == "l" and " -> " in name:
            name = name.split(" -> ", 1)[0]
        name = name.strip()
        if not name or name in (".", ".."):
            return None
        if perm[0] == "d":
            return name, True
        if perm[0] == "l":
            return name, None
        return name, False

    if text[0] in "dl-":
        parts = text.split(None, 8)
        if len(parts) >= 9:
            name = parts[8]
            if " -> " in name:
                name = name.split(" -> ", 1)[0]
            name = name.strip()
            if name and name not in (".", ".."):
                return name, (True if text[0] == "d" else None if text[0] == "l" else False)
        return None

    name = text.split()[-1].strip()
    if not name or name in (".", ".."):
        return None
    return name, None


def _ftp_probe_is_dir(ftp: ftplib.FTP, base: str, name: str) -> bool:
    try:
        ftp.cwd(ftp_join(base, name))
        return True
    except Exception:
        return False
    finally:
        try:
            ftp.cwd(base)
        except Exception:
            pass


def _sorted_entries(entries: dict) -> list[tuple[str, bool]]:
    return sorted(entries.items(), key=lambda e: (not e[1], e[0].lower()))


def ftp_list_dir(ftp: ftplib.FTP, path: str) -> tuple[str, list[tuple[str, bool]]]:
    target = ftp_normalize_path(path)
    ftp.cwd(target)
    try:
        actual = ftp_normalize_path(ftp.pwd())
    except Exception:
        actual = target

    try:
        entries: dict = {}
        typed_all = True
        for name, facts in ftp.mlsd():
            kind = str(facts.get("type", "")).lower()
            if kind in ("cdir", "pdir") or name in (".", ".."):
                continue
            if not kind:
                typed_all = False
                break
            if kind.startswith("os.unix=slink") or kind.startswith("os.unix=symlink"):
                entries[name] = _ftp_probe_is_dir(ftp, actual, name)
            else:
                entries[name] = kind == "dir"
        if typed_all and entries:
            return actual, _sorted_entries(entries)
    except Exception:
        pass

    entries = {}
    listed_ok = False
    uncertain: list[str] = []
    for command in ("LIST -a", "LIST"):
        lines: list[str] = []
        try:
            ftp.retrlines(command, lines.append)
        except Exception:
            continue
        parsed = [p for p in (_parse_list_line(l) for l in lines) if p]
        if command == "LIST -a" and not parsed and lines:
            continue
        listed_ok = True
        for name, kind in parsed:
            if kind is None:
                uncertain.append(name)
                entries[name] = False
            else:
                entries[name] = kind
        break

    nlst_names: list[str] = []
    try:
        nlst_names = [n.rsplit("/", 1)[-1].strip() for n in ftp.nlst()]
    except Exception:
        nlst_names = []
    for name in nlst_names:
        if name and name not in (".", "..") and name not in entries:
            uncertain.append(name)
            entries[name] = False

    if not listed_ok and not entries:
        return actual, []

    for name in dict.fromkeys(uncertain):
        entries[name] = _ftp_probe_is_dir(ftp, actual, name)

    return actual, _sorted_entries(entries)


def ftp_ensure_dir(ftp: ftplib.FTP, path: str) -> None:
    target = ftp_normalize_path(path)
    ftp.cwd("/")
    if target == "/":
        return
    for part in [p for p in target.split("/") if p]:
        try:
            ftp.cwd(part)
        except ftplib.all_errors:
            try:
                ftp.mkd(part)
            except ftplib.all_errors:
                pass
            ftp.cwd(part)

def ftp_store_file(ftp: ftplib.FTP, local_path: Path, remote_name: str,
                   on_bytes=None, cancel_event=None) -> None:
    with open(local_path, "rb") as handle:
        def callback(block):
            if cancel_event is not None and cancel_event.is_set():
                raise FtpTransferCancelled()
            if on_bytes:
                on_bytes(len(block))
        ftp.storbinary(f"STOR {remote_name}", handle, blocksize=FTP_BLOCK_SIZE, callback=callback)

def ftp_collect_files(root: Path, include_roots: list[Path] | None = None) -> list[Path]:
    sources = [p for p in (include_roots or []) if p is not None] or [root]
    files = []
    seen = set()
    for source in sources:
        try:
            for path in sorted(source.rglob("*")):
                try:
                    if path.is_file() and path not in seen:
                        seen.add(path)
                        files.append(path)
                except Exception:
                    continue
        except Exception:
            continue
    return sorted(files)

def ftp_upload_tree(cfg: FtpConfig, local_root: Path, remote_root: str,
                    progress_callback=None, cancel_event=None,
                    max_parallel: int | None = None,
                    include_roots: list[Path] | None = None) -> tuple[bool, str]:
    if not local_root.exists():
        return False, f"Nothing to upload - {local_root.name} is missing."

    files = ftp_collect_files(local_root, include_roots=include_roots)
    if not files:
        return False, f"Nothing to upload - {local_root.name} is empty."

    name_plan, renamed = plan_upload_names(files, local_root, console=True)
    _log_renamed("FTP", renamed)

    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except Exception:
            pass
    total_bytes = max(1, total_bytes)

    remote_root = ftp_normalize_path(remote_root)
    log_event(f"=== FTP upload: {len(files)} file(s), {format_size(total_bytes)} -> {cfg.host}:{remote_root} ===")

    def emit(percent, headline, detail):
        if progress_callback:
            try:
                progress_callback(int(percent), headline, detail)
            except Exception:
                pass

    workers_wanted = cfg.worker_count() if max_parallel is None else max(1, min(MAX_FTP_PARALLEL, int(max_parallel)))
    workers_wanted = min(workers_wanted, len(files))

    emit(0, "Connecting to FTP...", f"{cfg.host}:{cfg.port} ({ftp_mode_name(cfg)})")

    try:
        first_ftp = ftp_connect(cfg)
    except Exception as exc:
        return False, f"FTP connect failed: {exc}"

    work_queue: "queue.Queue" = queue.Queue()
    for index, local_file in enumerate(files, 1):
        work_queue.put((index, local_file))

    lock = threading.Lock()
    mkdir_lock = threading.Lock()
    stop = threading.Event()
    started = time.time()
    shared = {"sent": 0, "uploaded": 0, "last_emit": 0.0, "error": None, "connected": 0,
              "samples": collections.deque()}

    def should_stop() -> bool:
        return stop.is_set() or (cancel_event is not None and cancel_event.is_set())

    def record_error(message: str):
        with lock:
            if shared["error"] is None:
                shared["error"] = message
        stop.set()

    def worker(ftp, worker_id: int):
        current_remote_dir = None
        try:
            while not should_stop():
                try:
                    index, local_file = work_queue.get_nowait()
                except queue.Empty:
                    return

                try:
                    relative = local_file.relative_to(local_root)
                except ValueError:
                    relative = Path(local_file.name)
                remote_parts = name_plan[local_file]
                target_dir = ftp_join(remote_root, *remote_parts[:-1])
                try:
                    file_size = local_file.stat().st_size
                except Exception:
                    file_size = 0

                file_sent = 0

                def on_bytes(count):
                    nonlocal file_sent
                    if should_stop():
                        raise FtpTransferCancelled()
                    file_sent += count
                    now = time.time()
                    with lock:
                        shared["sent"] += count
                        if now - shared["last_emit"] < 0.16:
                            return
                        shared["last_emit"] = now
                        sent_now = shared["sent"]
                        samples = shared["samples"]
                        samples.append((now, sent_now))
                        while len(samples) > 2 and now - samples[0][0] > 3.0:
                            samples.popleft()
                        then, sent_then = samples[0]
                    if now - then >= 0.5:
                        speed = ((sent_now - sent_then) / (1024 ** 2)) / (now - then)
                    else:
                        speed = (sent_now / (1024 ** 2)) / max(0.001, now - started)
                    pct = min(99, int(sent_now / total_bytes * 100))
                    suffix = f" | {workers_wanted} at once" if workers_wanted > 1 else ""
                    emit(pct,
                         f"Uploading via FTP {pct}% | {speed:.2f} MB/s{suffix}",
                         f"FTP {index}/{len(files)}: {relative.as_posix()}")

                for attempt in range(2):
                    try:
                        if current_remote_dir != target_dir:
                            with mkdir_lock:
                                ftp_ensure_dir(ftp, target_dir)
                            current_remote_dir = target_dir
                        with lock:
                            sent_now = shared["sent"]
                        emit(min(99, int(sent_now / total_bytes * 100)),
                             f"Uploading via FTP ({index}/{len(files)})",
                             f"FTP {index}/{len(files)}: {relative.as_posix()} ({format_size(file_size)})")
                        ftp_store_file(ftp, local_file, remote_parts[-1],
                                       on_bytes=on_bytes, cancel_event=cancel_event)
                        with lock:
                            shared["uploaded"] += 1
                        break
                    except FtpTransferCancelled:
                        with lock:
                            shared["sent"] -= file_sent
                        return
                    except Exception as exc:
                        with lock:
                            shared["sent"] -= file_sent
                        file_sent = 0
                        reason = str(exc).strip() or type(exc).__name__
                        if attempt == 1:
                            record_error(f"Upload failed on {relative.as_posix()}: {reason}")
                            return
                        log_event(f"  FTP retry on {relative.as_posix()} - {reason}")
                        try:
                            ftp.close()
                        except Exception:
                            pass
                        current_remote_dir = None
                        try:
                            ftp = ftp_connect(cfg)
                        except Exception as reconnect_exc:
                            record_error(f"FTP reconnect failed: {reconnect_exc}")
                            return
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    def spawn_extra(worker_id: int):
        try:
            extra_ftp = ftp_connect(cfg)
        except Exception as exc:
            log_event(f"  FTP: extra connection {worker_id} refused ({str(exc).strip() or type(exc).__name__}) "
                      f"- continuing with fewer.")
            return
        with lock:
            shared["connected"] += 1
        worker(extra_ftp, worker_id)

    with lock:
        shared["connected"] = 1

    threads = [threading.Thread(target=spawn_extra, args=(i,), daemon=True, name=f"ftp-upload-{i}")
               for i in range(2, workers_wanted + 1)]
    for thread in threads:
        thread.start()

    worker(first_ftp, 1)

    for thread in threads:
        thread.join()

    if cancel_event is not None and cancel_event.is_set():
        return False, "Upload cancelled."
    if shared["error"]:
        return False, shared["error"]

    uploaded = shared["uploaded"]
    if uploaded < len(files):
        return False, f"Upload stopped early - {uploaded} of {len(files)} file(s) sent."

    elapsed = max(0.001, time.time() - started)
    speed = (total_bytes / (1024 ** 2)) / elapsed
    summary = f"FTP upload complete - {uploaded} file(s), {format_size(total_bytes)} at {speed:.2f} MB/s"
    if shared["connected"] > 1:
        summary += f" ({shared['connected']} connections)"
    log_event(f"=== {summary} ===")
    emit(100, "FTP upload complete!", f"Uploaded to {remote_root}")
    return True, summary


TITLE_ID_DIR_RE = re.compile(r"^[0-9A-Fa-f]{8}$")

CONTENT_SCAN_DEPTH = 6

CONTENT_SCAN_DEPTH_HINTED = CONTENT_SCAN_DEPTH + 2

def content_scan_depth(content_hint: bool = False) -> int:
    return CONTENT_SCAN_DEPTH_HINTED if content_hint else CONTENT_SCAN_DEPTH

def _has_iso(root: Path, max_depth: int = 6) -> bool:
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                for item in it:
                    try:
                        if item.is_file() and item.name.lower().endswith(".iso"):
                            return True
                        if item.is_dir(follow_symlinks=False) and depth < max_depth:
                            stack.append((Path(item.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return False

def _looks_like_content_dir(path: Path) -> bool:
    return bool(TITLE_ID_DIR_RE.match(path.name))

def find_content_upload_roots(game_dir: Path, max_depth: int = CONTENT_SCAN_DEPTH
                              ) -> tuple[Path | None, list[Path]]:
    try:
        if not game_dir.exists() or not game_dir.is_dir():
            return None, []
    except Exception:
        return None, []

    level = [game_dir]
    for _ in range(max(1, int(max_depth))):
        children = []
        for parent in level:
            try:
                for child in sorted(parent.iterdir()):
                    try:
                        if child.is_dir() and child.name != "GOD_Output":
                            children.append(child)
                    except Exception:
                        continue
            except Exception:
                continue
        if not children:
            return None, []

        matches = [c for c in children if _looks_like_content_dir(c)]
        if matches:
            by_parent: dict[Path, list[Path]] = {}
            for match in matches:
                by_parent.setdefault(match.parent, []).append(match)
            parent = max(by_parent, key=lambda p: (len(by_parent[p]), str(p)))
            return parent, sorted(by_parent[parent])

        level = children

    return None, []

def analyze_upload_target(game_dir: Path, content_hint: bool = False) -> tuple[str, Path | None, str]:
    try:
        if not game_dir.exists() or not game_dir.is_dir():
            return "missing", None, ""
    except Exception:
        return "missing", None, ""

    god_dir = game_dir / "GOD_Output"
    try:
        if god_dir.exists() and any(god_dir.iterdir()):
            return "god", god_dir, ""
    except Exception:
        pass

    if content_hint:
        content_root, content_dirs = find_content_upload_roots(
            game_dir, max_depth=CONTENT_SCAN_DEPTH_HINTED)
        if content_root is not None and content_dirs:
            return "content", content_root, ""

    has_executable = False
    for marker in ("default.xex", "default.xbe"):
        try:
            for hit in game_dir.rglob(marker):
                if "GOD_Output" in hit.parts:
                    continue
                has_executable = True
                break
        except Exception:
            pass
        if has_executable:
            break

    if not has_executable:
        content_root, content_dirs = find_content_upload_roots(game_dir)
        if content_root is not None and content_dirs:
            return "content", content_root, ""

    for marker in ("default.xex", "default.xbe"):
        try:
            for hit in game_dir.rglob(marker):
                if "GOD_Output" in hit.parts:
                    continue
                root = hit.parent
                if root == game_dir:
                    return "files", game_dir, ""
                return "xiso", root, root.name
        except Exception:
            pass

    try:
        isos = [p for p in game_dir.rglob("*.iso") if "GOD_Output" not in p.parts and p.is_file()]
    except Exception:
        isos = []
    if isos:
        return "needs_convert", isos[0], ""

    try:
        if any(game_dir.iterdir()):
            return "files", game_dir, ""
    except Exception:
        pass
    return "empty", None, ""

UPLOAD_KIND_LABELS = {
    "god": "GOD format",
    "content": "title ID content",
    "xiso": "extracted XISO",
    "files": "extracted files",
}

NEEDS_CONVERT_MESSAGE = ("Convert to God or XEX first")

FORCE_TITLE_ID_TO_CONTENT = True
FORCED_CONTENT_KINDS = ("content", "god")
FORCED_CONTENT_SUBPATH = ("Content", "0000000000000000")

FTP_DRIVE_RE = re.compile(r"^(hdd\w*|usb\w*|mu\w*|intmu\w*|flash|memunit\w*|sfc\w*)$",
                          re.IGNORECASE)
FTP_FALLBACK_DRIVE = "Hdd1"

def _path_parts(path: str) -> list[str]:
    return [p for p in ftp_normalize_path(path).split("/") if p]

def forced_content_destination(remote_dir: str, drive_prefixed: bool,
                               fallback_path: str = "") -> str:
    parts = _path_parts(remote_dir)

    for idx, part in enumerate(parts):
        if part.lower() == "content":
            return ftp_join("/" + "/".join(parts[:idx]), *FORCED_CONTENT_SUBPATH)

    if not drive_prefixed:
        return ftp_join("/", *FORCED_CONTENT_SUBPATH)

    drive = ""
    if parts and FTP_DRIVE_RE.match(parts[0]):
        drive = parts[0]
    else:
        fallback_parts = _path_parts(fallback_path) if fallback_path else []
        if fallback_parts and FTP_DRIVE_RE.match(fallback_parts[0]):
            drive = fallback_parts[0]
        else:
            drive = FTP_FALLBACK_DRIVE
    return ftp_join("/" + drive, *FORCED_CONTENT_SUBPATH)

def upload_game_via_ftp(cfg: FtpConfig, game_dir: Path, remote_dir: str,
                        progress_callback=None, cancel_event=None,
                        archive_hint: Path | None = None,
                        max_parallel: int | None = None,
                        entry: "Files | None" = None,
                        rename_to: str | None = None) -> tuple[bool, str]:
    """rename_to: the name to give the game's folder on the console instead of its own."""
    catalog = catalog_folder_kind(game_dir, entry)
    if catalog is not None:
        return upload_catalog_via_ftp(cfg, game_dir, remote_dir, catalog=catalog,
                                      progress_callback=progress_callback,
                                      cancel_event=cancel_event, max_parallel=max_parallel)
    content_hint = entry_is_content(entry)
    kind, path, remote_subfolder = analyze_upload_target(game_dir, content_hint=content_hint)

    rename_to = (rename_to or "").strip()
    if rename_to:
        source, folder_name = hdd_upload_folder(kind, path, remote_subfolder)
        if folder_name and folder_name != rename_to:
            log_event(f"FTP: uploading '{folder_name}' as '{rename_to}'")
            path, remote_subfolder = source, rename_to

    if remote_subfolder:
        cleaned = clean_upload_name(remote_subfolder, console=True)
        if cleaned != remote_subfolder:
            log_event(f"FTP: folder '{remote_subfolder}' renamed to '{cleaned}' "
                      f"(removed characters the drive can't take)")
            remote_subfolder = cleaned

    if kind == "needs_convert":
        return False, NEEDS_CONVERT_MESSAGE
    if kind in ("missing", "empty") or path is None:
        try:
            if archive_hint is not None and archive_hint.suffix.lower() == ".iso" and archive_hint.exists():
                return False, NEEDS_CONVERT_MESSAGE
        except Exception:
            pass
        return False, f"Nothing to upload for {game_dir.name} - extract it first."

    destination = ftp_join(remote_dir, remote_subfolder) if remote_subfolder else ftp_normalize_path(remote_dir)
    if FORCE_TITLE_ID_TO_CONTENT and kind in FORCED_CONTENT_KINDS:
        forced = forced_content_destination(remote_dir, drive_prefixed=True,
                                            fallback_path=getattr(cfg, "default_path", ""))
        if forced != destination:
            log_event(f"FTP: {game_dir.name} is title ID content - forcing "
                      f"{destination} -> {forced}")
        destination = forced

    include_roots = None
    if kind == "content":
        _root, include_roots = find_content_upload_roots(
            game_dir, max_depth=content_scan_depth(content_hint))
        if not include_roots:
            include_roots = None

    detected = UPLOAD_KIND_LABELS.get(kind, kind)
    if include_roots:
        detected += " (" + ", ".join(p.name for p in include_roots) + ")"
    if content_hint:
        detected += " [known title ID source]"
    log_event(f"FTP: {game_dir.name} detected as {detected} -> {destination}")
    return ftp_upload_tree(cfg, path, destination,
                           progress_callback=progress_callback, cancel_event=cancel_event,
                           max_parallel=max_parallel, include_roots=include_roots)


HDD_SETTINGS_KEY = "hdd"
HDD_STATE = {"online": False}

HDD_POLL_MS = 20000

HDD_LOCK = threading.Lock()

HDD_MAX_NAME = 42

# A game whose folder name is longer than this is given a shorter one before it is
# uploaded (Xbox drive, ordinary drive or FTP). Kept a little under HDD_MAX_NAME on purpose.
HDD_RENAME_AT = 40

_HDD_NAME_BAD_CHARS_RE = re.compile(r"[^A-Za-z0-9 !#$%&'()\-.@\[\]^_`{}~+,;=]")
_HDD_TRAILING_TAG_RE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")
_HDD_DISC_TAG_RE = re.compile(r"\b(disc|disk|cd|dvd)\s*\w+", re.IGNORECASE)
_WINDOWS_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_upload_name(name: str, console: bool = True) -> str:
    """A file or folder name with the characters the target cannot take removed.

    console=True is for the Xbox (FTP and FATX drives): accents are taken off
    letters (e-acute -> e) and anything else outside the characters FATX accepts
    is deleted, so "Assassin’s Creed.txt" becomes "Assassins Creed.txt".
    console=False is for an ordinary drive: only what Windows refuses is deleted.
    """
    import unicodedata
    text = name or ""
    if console:
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
        text = _HDD_NAME_BAD_CHARS_RE.sub("", text)
    else:
        text = _WINDOWS_BAD_CHARS_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(" .")
    return text or "_"


def plan_upload_names(files: list, local_root: Path, console: bool = True) -> tuple[dict, list]:
    """Work out the name every file and folder gets on the target.

    Returns ({local file: path parts on the target}, [(old path, new path), ...] for
    anything that had to be renamed). Names that are already fine keep their place;
    a cleaned name that clashes with one in the same folder gets " (2)" and so on.
    """
    rel_paths = []
    for path in files:
        try:
            parts = Path(path).relative_to(local_root).parts
        except ValueError:
            parts = (Path(path).name,)
        rel_paths.append((path, parts))

    children: dict = {}
    for _path, parts in rel_paths:
        for depth in range(len(parts)):
            children.setdefault(parts[:depth], {})[parts[depth]] = depth < len(parts) - 1

    new_name: dict = {}
    renamed_parts: list = []
    for parent, names in children.items():
        used: set = set()
        pending = []
        for name, is_dir in names.items():
            clean = clean_upload_name(name, console)
            if clean == name and name.lower() not in used:
                used.add(name.lower())
                new_name[parent + (name,)] = name
            else:
                pending.append((name, clean, is_dir))
        for name, clean, is_dir in sorted(pending):
            stem, ext = (clean, "") if is_dir else os.path.splitext(clean)
            candidate, number = clean, 2
            while candidate.lower() in used:
                candidate = f"{stem} ({number}){ext}"
                number += 1
            used.add(candidate.lower())
            new_name[parent + (name,)] = candidate
            renamed_parts.append(parent + (name,))

    plan = {path: tuple(new_name[parts[:i + 1]] for i in range(len(parts)))
            for path, parts in rel_paths}
    renamed = [("/".join(p), "/".join(new_name[p[:i + 1]] for i in range(len(p))))
               for p in renamed_parts]
    return plan, renamed


def _log_renamed(where: str, renamed: list) -> None:
    for old, new in renamed[:20]:
        log_event(f"  {where}: renamed '{old}' -> '{new}' (removed characters the drive can't take)")
    if len(renamed) > 20:
        log_event(f"  {where}: ...and {len(renamed) - 20} more renamed.")


def hdd_title_needs_rename(title: str, limit: int = HDD_RENAME_AT) -> bool:
    return len(title or "") > limit


def hdd_name_problem(name: str, limit: int = HDD_RENAME_AT) -> str | None:
    """Why a name cannot be used for an uploaded game folder, or None when it is fine."""
    name = name or ""
    if not name.strip():
        return "Type a name first."
    if len(name) > limit:
        return f"That is {len(name)} characters - it has to be {limit} or fewer."
    bad = _HDD_NAME_BAD_CHARS_RE.findall(name)
    if bad:
        shown = " ".join(dict.fromkeys(bad))
        return f"The drive will not accept: {shown}"
    if name.strip(". ") == "":
        return "Use some letters or numbers."
    return None


def hdd_suggest_name(title: str, limit: int = HDD_RENAME_AT, taken=()) -> str:
    """A shorter name for a title: drops the (Region) (Languages) tags, then trims.

    A disc tag is kept, so a two-disc game does not end up as one folder. Names in
    'taken' (lowercase) are avoided.
    """
    taken = {str(n).lower() for n in (taken or ())}
    import unicodedata
    name = unicodedata.normalize("NFKD", title or "")       # e-acute -> e, and so on
    name = _HDD_NAME_BAD_CHARS_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()

    disc = ""
    for tag in re.findall(r"[(\[]([^()\[\]]*)[)\]]", name):
        if _HDD_DISC_TAG_RE.fullmatch(tag.strip()):
            disc = tag.strip()
            break

    # Drop tags off the end until it fits. With a disc tag they all go - it is put back after.
    base = name
    while disc or len(base) > limit:
        shorter = _HDD_TRAILING_TAG_RE.sub("", base).strip()
        if shorter == base or not shorter:
            break
        base = shorter
    base = base.strip(" .-_") or name.strip(" .-_") or "Game"

    def build(stem: str, extra: str) -> str:
        room = max(1, limit - len(extra))
        cut = stem[:room]
        if len(stem) > room and stem[room] != " " and " " in cut[int(room * 0.6):]:
            cut = cut[:cut.rindex(" ")]         # stop at a whole word rather than mid-word
        return (cut.rstrip(" .-_") or "Game") + extra

    suffix = f" ({disc})" if disc else ""
    candidate = build(base, suffix)
    number = 2
    while candidate.lower() in taken:
        candidate = build(base, f"{suffix} {number}")
        number += 1
    return candidate


DEFAULT_HDD_PARALLEL = 4
MAX_HDD_PARALLEL = 6

HDD_BLOCK_SIZE = 1024 * 1024

HDD_COMMON_PATHS = (
    ("Root", "/"),
    ("Content/0's", "/Content/0000000000000000")
)
HDD_COMMON_PATHS_NORMAL = (
    ("Root", "/"),
    ("Content/0's", "/Content/0000000000000000")
)

hdd_normalize_path = ftp_normalize_path
hdd_join = ftp_join
hdd_parent = ftp_parent
hdd_collect_files = ftp_collect_files


HDD_MODE_FATX = "fatx"
HDD_MODE_NORMAL = "normal"

NORMAL_MAX_NAME = 255


@dataclass
class HddConfig:
    device_id: str = ""
    device_label: str = ""
    partition_offset: int = -1
    partition_name: str = ""
    partition_size: int = 0
    default_path: str = "/Content/0000000000000000"
    mode: str = HDD_MODE_FATX
    volume_path: str = ""
    max_parallel: int = DEFAULT_HDD_PARALLEL

    def worker_count(self) -> int:
        try:
            return max(1, min(MAX_HDD_PARALLEL, int(self.max_parallel)))
        except Exception:
            return DEFAULT_HDD_PARALLEL

    def is_configured(self) -> bool:
        if self.mode == HDD_MODE_NORMAL:
            return bool((self.volume_path or "").strip())
        return bool((self.device_id or "").strip()) and int(self.partition_offset) >= 0

    @property
    def label(self) -> str:
        base = self.device_label or self.device_id or "(no drive)"
        if self.mode == HDD_MODE_NORMAL:
            vol = self.partition_name or self.volume_path or "partition"
            return f"{vol} on {base}"
        return f"{self.partition_name or 'partition'} on {base}"


def load_hdd_config() -> HddConfig:
    raw = load_settings().get(HDD_SETTINGS_KEY)
    if not isinstance(raw, dict):
        return HddConfig()

    def _int(value, fallback):
        try:
            return int(value)
        except Exception:
            return fallback

    mode = str(raw.get("mode", HDD_MODE_FATX) or HDD_MODE_FATX).strip().lower()
    if mode not in (HDD_MODE_FATX, HDD_MODE_NORMAL):
        mode = HDD_MODE_FATX

    return HddConfig(
        device_id=str(raw.get("device_id", "") or "").strip(),
        device_label=str(raw.get("device_label", "") or ""),
        partition_offset=_int(raw.get("partition_offset"), -1),
        partition_name=str(raw.get("partition_name", "") or ""),
        partition_size=_int(raw.get("partition_size"), 0),
        default_path=hdd_normalize_path(raw.get("default_path") or "/"),
        mode=mode,
        volume_path=str(raw.get("volume_path", "") or "").strip(),
        max_parallel=max(1, min(MAX_HDD_PARALLEL,
                                _int(raw.get("max_parallel"), DEFAULT_HDD_PARALLEL))),
    )


def save_hdd_config(cfg: HddConfig) -> None:
    data = load_settings()
    data[HDD_SETTINGS_KEY] = asdict(cfg)
    save_settings(data)


def hdd_available() -> bool:
    return fatx360 is not None



def _hdd_disk_number_from_device_id(device_id: str) -> int | None:
    match = re.search(r"PHYSICALDRIVE(\d+)", device_id or "", re.IGNORECASE)
    return int(match.group(1)) if match else None


def _hdd_volume_disk_number(drive_letter: str) -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080

    class STORAGE_DEVICE_NUMBER(ctypes.Structure):
        _fields_ = [("DeviceType", wintypes.DWORD),
                    ("DeviceNumber", wintypes.DWORD),
                    ("PartitionNumber", wintypes.DWORD)]

    handle = ctypes.windll.kernel32.CreateFileW(
        f"\\\\.\\{drive_letter}", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None)
    if handle in (0, -1, 0xFFFFFFFF):
        return None
    try:
        info = STORAGE_DEVICE_NUMBER()
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
            ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned), None)
        return int(info.DeviceNumber) if ok else None
    except Exception:
        return None
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


@dataclass
class NormalPartition:
    letter: str
    name: str
    size: int
    free: int

    @property
    def label(self) -> str:
        base = self.name or "Local Disk"
        return (f"{base} ({self.letter}) - {format_size(self.free)} free "
                f"of {format_size(self.size)}")


def list_normal_partitions(device_id: str) -> list[NormalPartition]:
    if sys.platform != "win32":
        return []
    disk_number = _hdd_disk_number_from_device_id(device_id)
    if disk_number is None:
        return []

    import ctypes

    DRIVE_REMOVABLE = 2
    DRIVE_FIXED = 3

    results: list[NormalPartition] = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []

    for i in range(26):
        if not (mask & (1 << i)):
            continue
        letter = f"{chr(65 + i)}:"
        root = f"{letter}\\"
        try:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        except Exception:
            continue
        if drive_type not in (DRIVE_FIXED, DRIVE_REMOVABLE):
            continue
        if _hdd_volume_disk_number(letter) != disk_number:
            continue

        label = ""
        try:
            label_buf = ctypes.create_unicode_buffer(261)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                root, label_buf, 260, None, None, None, None, 0)
            if ok:
                label = label_buf.value
        except Exception:
            pass

        try:
            usage = shutil.disk_usage(root)
        except Exception:
            continue

        results.append(NormalPartition(letter=letter, name=label,
                                       size=usage.total, free=usage.free))
    return results


class NormalVolume:

    # Ordinary filesystem: the OS serialises its own metadata, so several files can be
    # written at the same time. hdd_upload_tree only goes parallel on volumes that say so.
    thread_safe = True

    class _Entry:
        __slots__ = ("name", "is_dir")

        def __init__(self, name: str, is_dir: bool):
            self.name = name
            self.is_dir = is_dir

    def __init__(self, root: str):
        self.root = Path(root)
        try:
            usage = shutil.disk_usage(self.root)
            self.size = usage.total
        except Exception:
            self.size = 0
        self.bytes_per_cluster = 0

    def _resolve(self, path: str) -> Path:
        rel = (path or "/").strip("/")
        return (self.root / rel) if rel else self.root

    def listdir(self, path: str):
        target = self._resolve(path)
        if not target.exists():
            return []
        entries = []
        with os.scandir(target) as it:
            for child in it:
                try:
                    is_dir = child.is_dir()
                except OSError:
                    is_dir = os.path.isdir(child.path)
                entries.append(self._Entry(child.name, is_dir))
        return entries

    def ensure_dir(self, path: str) -> None:
        self._resolve(path).mkdir(parents=True, exist_ok=True)

    def free_space(self) -> tuple[int, int]:
        usage = shutil.disk_usage(self.root)
        return usage.free, usage.total

    def write_file(self, target_dir: str, name: str, local_file: Path,
                   progress=None, cancel=None, overwrite: bool = True) -> None:
        dest_dir = self._resolve(target_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists() and not overwrite:
            raise FileExistsError(str(dest))

        try:
            total = local_file.stat().st_size
        except Exception:
            total = 0

        done = 0
        finished = False
        try:
            with open(local_file, "rb") as src, open(dest, "wb") as out:
                # Reserve the whole file up front so files written side by side
                # do not end up interleaved (fragmented) on the drive.
                reserved = False
                if total > 0:
                    try:
                        out.truncate(total)
                        reserved = True
                    except OSError:
                        reserved = False
                while True:
                    if cancel is not None and cancel():
                        raise Exception("cancelled")
                    chunk = src.read(HDD_BLOCK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
                if reserved and done != total:
                    out.truncate(done)
            finished = True
        finally:
            if not finished:
                # A reserved file is already full length, so a half-written one would
                # look complete. Never leave it behind.
                try:
                    dest.unlink()
                except OSError:
                    pass


class _NullDevice:

    def __init__(self, size: int = 0):
        self.size = size

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass


def hdd_open_volume(cfg: HddConfig, writable: bool = False):
    if not cfg.is_configured():
        raise ArchiveError("No hard drive chosen. Pick one in Settings first.")

    if cfg.mode == HDD_MODE_NORMAL:
        root = cfg.volume_path
        if not Path(root).exists():
            raise ArchiveError(f"{root} is not available - is the drive still plugged in?")
        vol = NormalVolume(root)
        return _NullDevice(vol.size), vol

    if fatx360 is None:
        raise ArchiveError(f"FATX support is unavailable: {FATX_IMPORT_ERROR}")

    dev = fatx360.open_disk(cfg.device_id, writable=writable)
    try:
        size = int(cfg.partition_size or 0)
        if size <= 0:
            size = max(0, dev.size - int(cfg.partition_offset))
        vol = fatx360.FatxVolume(dev, int(cfg.partition_offset), size)
    except Exception:
        try:
            dev.close()
        except Exception:
            pass
        raise
    return dev, vol


def hdd_test_connection(cfg: HddConfig) -> tuple[bool, str]:
    if not cfg.is_configured():
        return False, "No hard drive selected."
    if cfg.mode != HDD_MODE_NORMAL and fatx360 is None:
        return False, f"FATX support is unavailable: {FATX_IMPORT_ERROR}"

    with HDD_LOCK:
        dev = None
        try:
            dev, vol = hdd_open_volume(cfg, writable=False)
            vol.listdir("/")
            if cfg.mode == HDD_MODE_NORMAL:
                free_bytes, _total = vol.free_space()
                return True, (f"{cfg.label} ready - {format_size(vol.size)} total, "
                              f"{format_size(free_bytes)} free.")
            return True, (f"{cfg.partition_name or 'Partition'} ready on "
                          f"{cfg.device_label or cfg.device_id} - "
                          f"{format_size(vol.size)}, {format_size(vol.bytes_per_cluster)} clusters.")
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if "winerror 5" in message.lower() or "access denied" in message.lower():
                if cfg.mode != HDD_MODE_NORMAL:
                    message += "\nRun the app as Administrator - raw disk access requires it."
            elif "winerror 2" in message.lower():
                message = "That drive is no longer connected."
            return False, message
        finally:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass


def hdd_check_names(files: list[Path], local_root: Path, max_name: int = HDD_MAX_NAME,
                    plan: dict | None = None) -> str | None:
    for path in files:
        if plan is not None and path in plan:
            parts = plan[path]
        else:
            try:
                parts = path.relative_to(local_root).parts
            except ValueError:
                parts = (path.name,)
        for part in parts:
            if len(part) > max_name:
                limit_name = "FATX allows" if max_name == HDD_MAX_NAME else "The drive allows"
                return (f"'{part}' is {len(part)} characters - {limit_name} {max_name}. "
                        f"Rename it and try again.")
            if any(ch in part for ch in '\\/:*?"<>|'):
                return f"'{part}' contains a character the drive will not accept."
    return None


def hdd_upload_tree(cfg: HddConfig, local_root: Path, remote_root: str,
                    progress_callback=None, cancel_event=None,
                    include_roots: list[Path] | None = None) -> tuple[bool, str]:
    if not local_root.exists():
        return False, f"Nothing to upload - {local_root.name} is missing."

    files = hdd_collect_files(local_root, include_roots=include_roots)
    if not files:
        return False, f"Nothing to upload - {local_root.name} is empty."

    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except Exception:
            pass
    total_bytes = max(1, total_bytes)

    remote_root = hdd_normalize_path(remote_root)

    def emit(percent, headline, detail):
        if progress_callback:
            try:
                progress_callback(int(percent), headline, detail)
            except Exception:
                pass

    max_name = NORMAL_MAX_NAME if cfg.mode == HDD_MODE_NORMAL else HDD_MAX_NAME
    name_plan, renamed = plan_upload_names(files, local_root,
                                           console=True)
    _log_renamed("HDD", renamed)
    problem = hdd_check_names(files, local_root, max_name=max_name, plan=name_plan)
    if problem:
        return False, problem

    log_event(f"=== HDD copy: {len(files)} file(s), {format_size(total_bytes)} "
              f"-> {cfg.label}{remote_root} ===")
    emit(0, "Opening the hard drive...", cfg.label)

    def cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    with HDD_LOCK:
        dev = None
        try:
            dev, vol = hdd_open_volume(cfg, writable=True)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if cfg.mode != HDD_MODE_NORMAL and ("winerror 5" in message.lower()
                                                 or "access denied" in message.lower()):
                message += " -- run the app as Administrator."
            return False, f"Could not open the hard drive: {message}"

        started = time.time()
        shared = {"sent": 0, "copied": 0, "last_emit": 0.0, "error": None}
        lock = threading.Lock()
        dir_lock = threading.Lock()
        stop = threading.Event()
        workers = 1

        def should_stop() -> bool:
            return stop.is_set() or cancelled()

        try:
            emit(0, "Checking free space...", cfg.label)
            try:
                free_bytes, _total = vol.free_space()
                if free_bytes < total_bytes:
                    return False, (f"Not enough room on {cfg.partition_name or 'the partition'}: "
                                   f"{format_size(total_bytes)} needed, "
                                   f"{format_size(free_bytes)} free.")
            except Exception as exc:
                log_event(f"HDD: could not read free space ({exc}) - continuing anyway.")

            # Only volumes that declare themselves safe for it get more than one writer
            # (ordinary drives, and FATX volumes from a fatx360 that locks its tables).
            if getattr(vol, "thread_safe", False):
                workers = cfg.worker_count()
            workers = max(1, min(workers, len(files)))
            suffix = f" | {workers} at once" if workers > 1 else ""

            made_dirs: set[str] = set()
            work_queue: "queue.Queue" = queue.Queue()
            for index, local_file in enumerate(files, 1):
                work_queue.put((index, local_file))

            def copy_worker():
                while not should_stop():
                    try:
                        index, local_file = work_queue.get_nowait()
                    except queue.Empty:
                        return

                    file_done = 0
                    try:
                        try:
                            relative = local_file.relative_to(local_root)
                        except ValueError:
                            relative = Path(local_file.name)

                        remote_parts = name_plan[local_file]
                        target_dir = hdd_join(remote_root, *remote_parts[:-1])
                        with dir_lock:
                            if target_dir not in made_dirs:
                                vol.ensure_dir(target_dir)
                                made_dirs.add(target_dir)

                        try:
                            file_size = local_file.stat().st_size
                        except Exception:
                            file_size = 0

                        def on_progress(done, _total, rel=relative, idx=index):
                            nonlocal file_done
                            now = time.time()
                            with lock:
                                shared["sent"] += done - file_done
                                file_done = done
                                if now - shared["last_emit"] < 0.15:
                                    return
                                shared["last_emit"] = now
                                moved = shared["sent"]
                            pct = int(min(99, moved / total_bytes * 100))
                            elapsed = max(0.001, now - started)
                            speed = (moved / (1024 ** 2)) / elapsed
                            emit(pct, f"Copying to HDD {pct}% | {speed:.2f} MB/s{suffix}",
                                 f"HDD {idx}/{len(files)}: {rel.as_posix()}")

                        with lock:
                            moved = shared["sent"]
                        emit(int(min(99, moved / total_bytes * 100)),
                             f"Copying to HDD ({index}/{len(files)})",
                             f"HDD {index}/{len(files)}: {relative.as_posix()} ({format_size(file_size)})")

                        vol.write_file(target_dir, remote_parts[-1], local_file,
                                       progress=on_progress, cancel=should_stop, overwrite=True)

                        with lock:
                            shared["sent"] += file_size - file_done
                            shared["copied"] += 1
                    except Exception as exc:
                        message = str(exc) or exc.__class__.__name__
                        with lock:
                            shared["sent"] -= file_done
                            if message != "cancelled" and shared["error"] is None:
                                shared["error"] = message
                        stop.set()
                        return

            threads = [threading.Thread(target=copy_worker, daemon=True, name=f"hdd-copy-{i}")
                       for i in range(2, workers + 1)]
            for thread in threads:
                thread.start()
            copy_worker()
            for thread in threads:
                thread.join()

            if shared["error"]:
                log_event(f"HDD copy failed after {shared['copied']} file(s): {shared['error']}")
                return False, f"HDD copy failed: {shared['error']}"
            if cancelled() or shared["copied"] < len(files):
                return False, "Upload cancelled."

            try:
                dev.flush()
            except Exception:
                pass

        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if message == "cancelled":
                return False, "Upload cancelled."
            log_event(f"HDD copy failed after {shared['copied']} file(s): {message}")
            return False, f"HDD copy failed: {message}"
        finally:
            stop.set()
            try:
                dev.close()
            except Exception:
                pass

    copied = shared["copied"]
    elapsed = max(0.001, time.time() - started)
    speed = (total_bytes / (1024 ** 2)) / elapsed
    summary = (f"HDD copy complete - {copied} file(s), {format_size(total_bytes)} "
               f"at {speed:.2f} MB/s")
    if workers > 1:
        summary += f" ({workers} files at once)"
    log_event(f"=== {summary} ===")
    emit(100, "HDD copy complete!", f"Written to {remote_root}")
    return True, summary


def hdd_upload_folder(kind: str, path: Path | None, remote_subfolder: str) -> tuple[Path | None, str]:
    """The folder a game upload makes on the drive: (where its files come from, its name).

    The name is "" when the upload makes no folder of its own (GOD and title ID content
    go into their title ID folders, loose files go straight into the chosen folder).
    """
    if kind == "xiso":
        return path, remote_subfolder
    if kind == "files" and path is not None:
        # An archive that unpacked to one folder with everything inside it.
        try:
            children = list(path.iterdir())
        except Exception:
            children = []
        if len(children) == 1 and children[0].is_dir():
            return children[0], children[0].name
    return path, ""


def upload_game_to_hdd(cfg: HddConfig, game_dir: Path, remote_dir: str,
                       progress_callback=None, cancel_event=None,
                       archive_hint: Path | None = None,
                       entry: "Files | None" = None,
                       rename_to: str | None = None) -> tuple[bool, str]:
    """rename_to: the name to give the game's folder on the drive instead of its own
    (too long for FATX). Nothing on this computer is renamed."""
    catalog = catalog_folder_kind(game_dir, entry)
    if catalog is not None:
        return upload_catalog_to_hdd(cfg, game_dir, remote_dir, catalog=catalog,
                                     progress_callback=progress_callback, cancel_event=cancel_event)
    content_hint = entry_is_content(entry)
    kind, path, remote_subfolder = analyze_upload_target(game_dir, content_hint=content_hint)

    rename_to = (rename_to or "").strip()
    if rename_to:
        source, folder_name = hdd_upload_folder(kind, path, remote_subfolder)
        if folder_name and folder_name != rename_to:
            log_event(f"HDD: writing '{folder_name}' to the drive as '{rename_to}'")
            path, remote_subfolder = source, rename_to

    if remote_subfolder:
        cleaned = clean_upload_name(remote_subfolder, console=True)
        if cleaned != remote_subfolder:
            log_event(f"HDD: folder '{remote_subfolder}' renamed to '{cleaned}' "
                      f"(removed characters the drive can't take)")
            remote_subfolder = cleaned

    if kind == "needs_convert":
        return False, NEEDS_CONVERT_MESSAGE
    if kind in ("missing", "empty") or path is None:
        try:
            if archive_hint is not None and archive_hint.suffix.lower() == ".iso" and archive_hint.exists():
                return False, NEEDS_CONVERT_MESSAGE
        except Exception:
            pass
        return False, f"Nothing to upload for {game_dir.name} - extract it first."

    destination = hdd_join(remote_dir, remote_subfolder) if remote_subfolder else hdd_normalize_path(remote_dir)
    if FORCE_TITLE_ID_TO_CONTENT and kind in FORCED_CONTENT_KINDS:
        forced = forced_content_destination(remote_dir, drive_prefixed=False)
        if forced != destination:
            log_event(f"HDD: {game_dir.name} is title ID content - forcing "
                      f"{destination} -> {forced}")
        destination = forced

    include_roots = None
    if kind == "content":
        _root, include_roots = find_content_upload_roots(
            game_dir, max_depth=content_scan_depth(content_hint))
        if not include_roots:
            include_roots = None

    detected = UPLOAD_KIND_LABELS.get(kind, kind)
    if include_roots:
        detected += " (" + ", ".join(p.name for p in include_roots) + ")"
    if content_hint:
        detected += " [known title ID source]"
    log_event(f"HDD: {game_dir.name} detected as {detected} -> {destination}")
    return hdd_upload_tree(cfg, path, destination,
                           progress_callback=progress_callback, cancel_event=cancel_event,
                           include_roots=include_roots)


# ---------------------------------------------------------------------------
# Catalogs: the Themes and Gamer Pictures tabs.
#
#   Themes          https://archive.org/details/xbox_360_themes
#   Gamer Pictures  https://archive.org/details/xbox_360_gamer_pictures
#
# Both download through the normal queue, like any other item. When they are
# uploaded (FTP, HDD, the row's Upload button or Upload All) every package is
# always put under Content/0000000000000000/FFFE07D1/ on the drive, no matter
# which folder was picked: themes in 00030000, gamer pictures in 00020000.
# ---------------------------------------------------------------------------

CATALOG_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60   # archive.org themes / gamer pictures: once a week
CATALOG_CACHE_VERSION = 1
CATALOG_ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")

THEME_TITLE_ID = "FFFE07D1"             # the dashboard's title ID
THEME_CONTENT_TYPE_DIR = "00030000"      # content type folder for themes
GAMERPIC_CONTENT_TYPE_DIR = "00020000"   # content type folder for gamer pictures

STFS_MAGICS = (b"CON ", b"LIVE", b"PIRS")
STFS_CONTENT_TYPE_OFFSET = 0x344
STFS_CONTENT_THEME = 0x00030000
STFS_CONTENT_GAMERPIC = 0x00020000
_STFS_DIR_BY_TYPE = {STFS_CONTENT_THEME: THEME_CONTENT_TYPE_DIR,
                     STFS_CONTENT_GAMERPIC: GAMERPIC_CONTENT_TYPE_DIR}

# Never pushed to the console when an archive holds no recognisable package.
CATALOG_JUNK_SUFFIXES = (".txt", ".nfo", ".diz", ".sfv", ".md5", ".url", ".htm", ".html", ".ini",
                         ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".db", ".md", ".pdf")

# "Name_4D4207D2.zip" or "Name_454108C6_2DCFAD9ABE10638C2480AA78D997D633E592607A.zip"
_CATALOG_NAME_RE = re.compile(r"^(.*?)[\s_]*_([0-9A-Fa-f]{8})(?:_([0-9A-Fa-f]{40}))?$")


@dataclass(frozen=True)
class Catalog:
    key: str             # tab name
    source: str          # archive.org item
    noun: str            # "theme"
    plural: str          # "themes"
    tab_label: str       # sidebar button / headings
    cache_file: str
    content_dir: str     # folder under FFFE07D1 its packages go to

    @property
    def page_url(self) -> str:
        return f"https://archive.org/details/{self.source}"

    @property
    def console_path(self) -> str:
        return "Content\\0000000000000000\\" + THEME_TITLE_ID + "\\" + self.content_dir + "\\"


THEME_CATALOG = Catalog("themes", "xbox_360_themes", "theme", "themes", "Themes",
                        "themes_cache.json", THEME_CONTENT_TYPE_DIR)
GAMERPIC_CATALOG = Catalog("gamerpics", "xbox_360_gamer_pictures", "gamer picture pack",
                           "gamer picture packs", "Gamer Pictures",
                           "gamerpics_cache.json", GAMERPIC_CONTENT_TYPE_DIR)
CATALOGS = (THEME_CATALOG, GAMERPIC_CATALOG)
_CATALOG_BY_SOURCE = {c.source: c for c in CATALOGS}
_CATALOG_BY_DIR = {c.content_dir: c for c in CATALOGS}

_CATALOG_LOCK = threading.Lock()
_CATALOG_STATE: dict = {c.key: {"entries": None, "lookup": None} for c in CATALOGS}


def catalog_for_entry(entry) -> Catalog | None:
    return _CATALOG_BY_SOURCE.get(getattr(entry, "archive_identifier", None) or "")


def is_catalog_entry(entry) -> bool:
    return catalog_for_entry(entry) is not None


def _split_catalog_name(name: str) -> tuple[str, str, str]:
    stem = Path(name or "").stem
    match = _CATALOG_NAME_RE.match(stem)
    if match and match.group(1).strip():
        return match.group(1).strip(), match.group(2).upper(), (match.group(3) or "").upper()
    return stem.strip(), "", ""


def catalog_display_name(name: str) -> str:
    """'! Aiko Dynamic HD Theme_4D4207D2.zip' -> '! Aiko Dynamic HD Theme'."""
    return _split_catalog_name(name)[0] or Path(name or "").stem


def catalog_title_id(name: str) -> str:
    """The game's title ID from the file name ('4D4207D2'), or '' when it has none."""
    return _split_catalog_name(name)[1]


def catalog_name_hash(name: str) -> str:
    """The 40 character ID some file names carry to tell same-named packs apart, or ''."""
    return _split_catalog_name(name)[2]


def _catalog_cache_path(cat: Catalog) -> Path:
    return CORE_DIR / cat.cache_file


def _read_catalog_cache(cat: Catalog, ignore_age: bool = False) -> list | None:
    try:
        blob = json.loads(_catalog_cache_path(cat).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(blob, dict) or blob.get("version") != CATALOG_CACHE_VERSION:
        return None
    if not ignore_age:
        try:
            age = time.time() - float(blob.get("fetched_at", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > CATALOG_CACHE_TTL_SECONDS:
            return None
    valid_keys = {f.name for f in dataclass_fields(Files)}
    entries = []
    try:
        for raw in blob.get("entries", []):
            entry = Files(**{k: v for k, v in raw.items() if k in valid_keys})
            if entry.archive_identifier == cat.source:
                entries.append(entry)
    except Exception:
        return None
    return entries or None


def _write_catalog_cache(cat: Catalog, entries: list) -> None:
    path = _catalog_cache_path(cat)
    tmp = path.with_suffix(".tmp")
    try:
        payload = {"version": CATALOG_CACHE_VERSION, "fetched_at": time.time(),
                   "entries": [asdict(e) for e in entries]}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _remember_catalog(cat: Catalog, entries: list) -> None:
    lookup = {}
    for entry in entries:
        base = Path(entry.name).name
        lookup.setdefault(base.lower(), entry)
        lookup.setdefault(Path(base).stem.lower(), entry)
    with _CATALOG_LOCK:
        _CATALOG_STATE[cat.key] = {"entries": list(entries), "lookup": lookup}


def _catalog_sort_key(entry) -> tuple:
    display = catalog_display_name(entry.name)
    return (display.lstrip("!'\"#(). ").lower() or display.lower(), entry.name.lower())


def load_catalog_index(cat: Catalog, force: bool = False) -> list:
    """Every pack in the catalog's archive item, sorted by name. Cached for a day.

    force=True always asks archive.org. If archive.org cannot be reached, an
    older cached list is used rather than nothing.
    """
    if not force:
        cached = _read_catalog_cache(cat)
        if cached is not None:
            _remember_catalog(cat, cached)
            return cached

    entries: list = []
    error = None
    session = curl_cffi_requests.Session(impersonate="chrome124")
    try:
        resp = session.get(f"{ARCHIVE_METADATA_URL}/{cat.source}", timeout=30)
        if resp.status_code != 200:
            error = f"archive.org answered HTTP {resp.status_code}"
        else:
            for f in resp.json().get("files", []) or []:
                fname = f.get("name") or ""
                if not fname or is_excluded_file(fname):
                    continue
                if not fname.lower().endswith(CATALOG_ARCHIVE_SUFFIXES):
                    continue                    # only the packs themselves, not thumbnails / metadata
                try:
                    size_val = int(f.get("size")) if f.get("size") else None
                except (TypeError, ValueError):
                    size_val = None
                entries.append(Files(
                    id=fname, name=fname, size=size_val, date=f.get("mtime"),
                    source_type="archive", archive_identifier=cat.source,
                    allow_iso_conversion=False, content_source=False,
                ))
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
    finally:
        try:
            session.close()
        except Exception:
            pass

    if entries:
        entries.sort(key=_catalog_sort_key)
        _write_catalog_cache(cat, entries)
        _remember_catalog(cat, entries)
        log_event(f"{cat.tab_label}: {len(entries):,} {cat.plural} loaded from {cat.source}.")
        return entries

    stale = _read_catalog_cache(cat, ignore_age=True)
    if stale is not None:
        log_event(f"{cat.tab_label}: could not refresh the list ({error or 'nothing found'}) - "
                  f"using the saved list.")
        _remember_catalog(cat, stale)
        return stale
    raise ArchiveError(f"Could not load the {cat.noun} list from archive.org: {error or 'nothing found'}")


def known_catalog_entries(cat: Catalog) -> list:
    """The packs already loaded or saved on this PC. Never goes online."""
    with _CATALOG_LOCK:
        entries = _CATALOG_STATE[cat.key]["entries"]
    if entries is None:
        cached = _read_catalog_cache(cat, ignore_age=True)
        if cached is None:
            return []
        _remember_catalog(cat, cached)
        entries = cached
    return list(entries)


def catalog_entry_for_filename(filename: str):
    """The theme / gamer picture pack a downloaded file came from, or None."""
    base = Path(filename or "").name
    keys = (base.lower(), Path(base).stem.lower())
    for cat in CATALOGS:
        with _CATALOG_LOCK:
            lookup = _CATALOG_STATE[cat.key]["lookup"]
        if lookup is None:
            known_catalog_entries(cat)
            with _CATALOG_LOCK:
                lookup = _CATALOG_STATE[cat.key]["lookup"] or {}
        for key in keys:
            if key in lookup:
                return lookup[key]
    return None


# Search ignores punctuation, so "assassin's creed" finds "Assassins Creed",
# "halo: reach" finds "Halo Reach" and "spiderman" finds "Spider-Man".
# Apostrophes are dropped outright (assassin's -> assassins); every other mark
# becomes a space. Accents are folded too (pokemon finds Pokémon).
_SEARCH_APOSTROPHES = re.compile(r"['‘’‛ʼ´`]")
_SEARCH_NON_WORD = re.compile(r"[^a-z0-9]+")


def _search_fold_uncached(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _SEARCH_APOSTROPHES.sub("", text.replace("&", " and "))
    return _SEARCH_NON_WORD.sub(" ", text).strip()


_search_fold_cached = functools.lru_cache(maxsize=200_000)(_search_fold_uncached)


def search_fold(text: str) -> str:
    """Lowercase, accents and punctuation removed, words split by single spaces."""
    return _search_fold_cached(text or "")


def search_tokens(term: str) -> list[str]:
    """The words of a search, folded the same way the names are."""
    return search_fold(term).split()


def search_text_matches(tokens: list[str], text: str) -> bool:
    """True if every word appears in the text, or in the text with its spaces
    removed (so "spiderman" matches "Spider-Man" and "spider man" matches "Spiderman")."""
    folded = search_fold(text)
    if all(t in folded for t in tokens):
        return True
    squashed = folded.replace(" ", "")
    return all(t in folded or t in squashed for t in tokens)


# ---------------------------------------------------------------------------
# xefu compatibility: Original Xbox games that work on the Xbox 360 with a
# given xefu emulator build. Only the wiki's green results are bundled
# ("Supported" and "Playable"); see xefu_compat_data.py.

XEFU_VERSIONS: tuple = tuple(xefu_compat_data.XEFU_VERSIONS)
XEFU_SOURCE_URL: str = xefu_compat_data.SOURCE_URL


@dataclass(frozen=True)
class XefuGame:
    name: str
    works: tuple            # ((version, status, has_cfg), ...) in XEFU_VERSIONS order
    notes: str = ""

    @property
    def versions(self) -> list[str]:
        return [v for v, _status, _cfg in self.works]

    def works_on(self, version: str) -> bool:
        return any(v == version for v, _status, _cfg in self.works)

    @property
    def has_cfg(self) -> bool:
        return any(cfg for _v, _status, cfg in self.works)


_XEFU_FILE_EXT_RE = re.compile(r"\.(zip|7z|rar|iso|xiso|cso|tar|gz)$", re.IGNORECASE)
_XEFU_TAGS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_XEFU_TRAILING_TAG_RE = re.compile(r"\s*(\([^)]*\)|\[[^\]]*\])\s*$")
_XEFU_SCENE_GROUP_RE = re.compile(r"-[A-Za-z0-9]+$")       # "...XBOX-ZTM" -> "...XBOX"
# Trailing words in archive file names that aren't part of the game's name
# ("Conker.Live.&.Reloaded.USA.XBOX-ZTM", "Grabbed.By.The.Ghoulies.USAXBOX-ZTM").
_XEFU_RELEASE_WORDS = {
    "usa", "us", "eur", "europe", "jap", "jpn", "japan", "aus", "kor", "rf", "pal",
    "ntsc", "ntscu", "ntscj", "xbox", "multi", "multi3", "multi5", "proper", "repack",
    "internal", "fixed", "dvd", "dvd5", "dvd9", "iso", "xiso", "ztm",
}
_XEFU_RELEASE_GLUED_RE = re.compile(r"^(usa|eur|jap|jpn|aus|kor|rf|pal|ntsc)xbox$")
# The wiki writes "Knights of the Old Republic II", the archive "...Republic.2";
# the archive adds "1" to first games ("Halo.1.Combat.Evolved", "Fable.1").
_XEFU_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
_XEFU_IGNORED_WORDS = {"the", "1"}
_XEFU_SYNONYMS = {"versus": "vs", "feat": "featuring", "wwii": "ww2"}
_XEFU_EXPANSIONS = {"tmnt": ("teenage", "mutant", "ninja", "turtles")}
_XEFU_TWO_DIGIT_YEAR_RE = re.compile(r"0\d")             # "Madden NFL 03" is "Madden NFL 2003"
# Left off by one side or the other: the wiki writes "James Bond 007: Agent Under
# Fire" and "Disney/Pixar Finding Nemo", the archive "007.Agent.Under.Fire".
_XEFU_OPTIONAL_PREFIXES = (("james", "bond"), ("disney", "pixar"), ("disneys",), ("disney",), ("mtv",))
# "All-Star Baseball 2003 Feat Derek Jeter", "American McGee Presents Scrapland".
_XEFU_CUT_AFTER = {"featuring"}
_XEFU_CUT_BEFORE = {"presents"}
# Games the two lists spell too differently for the rules above:
# wiki name -> how the archive names it.
_XEFU_EXTRA_NAMES = {
    "From Russia With Love : 007": ("007 From Russia With Love",),
    "Blood Omen 2": ("Blood Omen 2 The Legacy of Kain Series",),
    "Braveknight": ("Braveknight Leverant Eiyuuden",),
    "Close Combat: First to Fight": ("Close Combat First To Fight United States Marines",),
    "Club Football": ("Club Football Chelsea FC",),
    "Club Football 2005": ("Club Football 2005 Chelsea FC",),
    "Counter Terrorist Special Forces - Fire for Effect works": (
        "Counter Terrorist Special Forces Fire For Effect",),
    "Dinosaur Hunting (JAP)": ("Dinosaur Hunting Ushinawareta Daichi",),
    "Dragon’s Lair 3D": ("Dragons Lair 3D Return To The Lair",),
    "DroneZ": ("Dennou Taisen DroneZ",),
    "England International Football": ("England International Football 2004 Edition",),
    "Ford Bold Moves Street Racing": ("Ford Street Racing",),
    "Guilty Gear X2 #Reload": ("Guilty Gear X2 Reload The Midnight Carnival",),
    "Kikou Heidan J-Phoenis Plus": ("Kikou Heidan J-Phoenix Plus",),
    "Marvel vs. Capcom 2": ("Marvel vs Capcom 2 New Age of Heroes",),
    "Mashed": ("Mashed Drive To Survive",),
    "NBA Street V3": ("NBA Street Vol 3",),
    "Nightcaster": ("Night Caster Defeat The Darkness",),
    "Sea World: Shamu’s Deep Sea Adventures": ("SeaWorld Adventure Parks Shamus Deep Sea Adventures",),
    "Splat Renegade Paintball": ("Splat Magazine Renegade Paintball",),
    "Street Racing Syndicate": ("SRS Street Racing Syndicate",),
    "Yetisports Arctic Adventure": ("Yetisports Arctic Adventures",),
    "Colin McRae Rally 3": ("Colin McRae Rally 03",),
    "World Series Baseball": ("World Series Baseball 2K2",),
    # Not green, but matched so the app can say so:
    "AMF Xtreme Bowling 2006": ("AMF Xtreme Bowling",),
    "Bicycle Casino": ("Bicycle Casino Includes Texas Hold em",),
    "ESPN Winter X-Games Snowboarding 2002": ("Winter X Games Snowboarding 2002",),
    "NBA Jam 2004": ("NBA Jam",),
    "Samurai Shodown V": ("Samurai Shodown 5",),
    "The Hobbit": ("Hobbit The Prelude To The Lord of The Rings",),
    "Ultimate Fighting Championship: Tapout": ("UFC Tapout",),
}
# Dropped from the end, one at a time, only if nothing matched as written
# ("Call of Duty 2 Big Red One Collectors Edition", "Hitman 2 Silent Assassin Rev 1").
_XEFU_EDITION_WORDS = {
    "collectors", "collector", "limited", "edition", "goty", "directors", "cut", "special",
    "platinum", "hits", "classics", "rev", "revision", "black",
    "game", "of", "year", "video", "vol",
}
_XEFU_STATE: dict = {}


def _xefu_words(text: str, release_name: bool = False) -> list[str]:
    """The words that identify a game, normalised so the wiki's spelling and the
    archive's spelling come out the same."""
    words = search_fold(text).split()
    if release_name:
        while len(words) > 1 and (words[-1] in _XEFU_RELEASE_WORDS
                                  or _XEFU_RELEASE_GLUED_RE.match(words[-1])):
            words.pop()
    expanded = []
    i = 0
    while i < len(words):
        word = words[i]
        if (word == "20" and i + 1 < len(words) and len(words[i + 1]) == 2
                and words[i + 1].isdigit()):
            word = "20" + words[i + 1]                   # "NFL Blitz 20-03" is 2003
            i += 1
        elif _XEFU_TWO_DIGIT_YEAR_RE.fullmatch(word):
            word = "20" + word                           # "FIFA Soccer 03" is 2003
        expanded.extend(_XEFU_EXPANSIONS.get(word, (word,)))
        i += 1
    out = []
    for word in expanded:
        word = _XEFU_SYNONYMS.get(word, word)
        word = _XEFU_ROMAN.get(word, word)
        if word.isdigit():
            word = str(int(word))                      # "007" == "7"
        if word not in _XEFU_IGNORED_WORDS:
            out.append(word)
    return out


def _xefu_without_prefix(words: list[str]) -> list[str] | None:
    for prefix in _XEFU_OPTIONAL_PREFIXES:
        if tuple(words[:len(prefix)]) == prefix and len(words) > len(prefix):
            return words[len(prefix):]
    return None


def _xefu_key(name: str, keep_tags: bool = False) -> str:
    """A game name from the wiki boiled down for matching."""
    text = name or ""
    if not keep_tags:
        text = _XEFU_TAGS_RE.sub(" ", text)
    return "".join(_xefu_words(text))


def _xefu_file_base(name: str) -> str:
    """A file name without folders, extensions or a scene group ("-ZTM")."""
    text = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    while True:
        stripped = _XEFU_FILE_EXT_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"_?_x27_", "'", text)               # "I__x27_ll" -> "I'll"
    if "." in text and "(" not in text and "[" not in text:
        text = _XEFU_SCENE_GROUP_RE.sub("", text)        # dotted scene name
    return text


def _xefu_file_keys(name: str) -> tuple[list[str], list[str]]:
    """(keys to try against the wiki's full names, word list without any tags).
    "Tetris Worlds (Online Edition) (USA).zip" tries with both tags, with
    "(Online Edition)" only, then with none."""
    text = _xefu_file_base(name)
    keys = []
    while True:
        key = "".join(_xefu_words(text, release_name=True))
        if key and key not in keys:
            keys.append(key)
        trimmed = _XEFU_TRAILING_TAG_RE.sub("", text)
        if trimmed == text:
            break
        text = trimmed
    return keys, _xefu_words(_XEFU_TAGS_RE.sub(" ", text), release_name=True)


def xefu_games() -> list:
    """Every bundled game with a green result, A-Z, as XefuGame."""
    games = _XEFU_STATE.get("games")
    if games is None:
        order = {v: i for i, v in enumerate(XEFU_VERSIONS)}
        games = []
        for name, works, notes in xefu_compat_data.GAMES:
            ordered = tuple(sorted(((v, s, bool(c)) for v, (s, c) in works.items()),
                                   key=lambda w: order.get(w[0], 99)))
            games.append(XefuGame(name=name, works=ordered, notes=notes or ""))
        games.sort(key=lambda g: search_fold(g.name))

        # Every name on the wiki list, green or not: a file is matched to the game
        # it really is, and a game that is only In-game or worse says so instead of
        # borrowing another game's result. Value: XefuGame, or None (not green).
        entries = [(g.name, g) for g in games]
        entries += [(name, None) for name in getattr(xefu_compat_data, "NOT_GREEN", ())]
        exact: dict = {}
        loose: dict = {}
        heads: dict = {}
        for name, game in entries:
            variants = [name, *_XEFU_EXTRA_NAMES.get(name, ())]
            words = search_fold(_XEFU_TAGS_RE.sub(" ", name)).split()
            short = _xefu_without_prefix(words)               # "Disney/Pixar Finding Nemo"
            if short:
                variants.append(" ".join(short))
            for i, word in enumerate(words):
                if word in ("featuring", "feat") and i > 0:   # "Ski Racing 2005 Featuring Hermann Maier"
                    variants.append(" ".join(words[:i]))
                if word in _XEFU_CUT_BEFORE and i + 1 < len(words):
                    variants.append(" ".join(words[i + 1:]))  # "Motor Trend presents Lotus Challenge"
            for variant in variants:
                exact.setdefault(_xefu_key(variant, keep_tags=True), game)
                loose.setdefault(_xefu_key(variant), game)    # "Tetris Worlds" before "(Online Edition)"
            # "Mercenaries: Playground of Destruction" is what the archive calls just
            # "Mercenaries"; "King of Fighters: Maximum Impact: Maniax" is "...Maximum Impact".
            parts = name.split(":")
            for n in range(1, len(parts)):
                head = _xefu_key(":".join(parts[:n]))
                if head and game not in heads.setdefault(head, []):
                    heads[head].append(game)
        _XEFU_STATE["games"] = games
        _XEFU_STATE["exact"] = exact
        _XEFU_STATE["loose"] = loose
        _XEFU_STATE["heads"] = heads
    return games


_XEFU_MISSING = object()


def _xefu_find_words(words: list[str]):
    """XefuGame / None (on the list, not green) / _XEFU_MISSING for a word list,
    trying the full name first, then with edition words dropped from the end."""
    loose, heads = _XEFU_STATE["loose"], _XEFU_STATE["heads"]
    while words:
        key = "".join(words)
        if key in loose:
            return loose[key]
        if len(words) <= 1 or words[-1] not in _XEFU_EDITION_WORDS:
            break
        words = words[:-1]
    key = "".join(words)
    found = heads.get(key) if key else None
    if found and len(found) == 1:        # only if no other game shares that main title
        return found[0]
    return _XEFU_MISSING


def xefu_lookup(name: str) -> tuple:
    """(XefuGame or None, listed). listed is True when the file matched a game on
    the wiki list; the XefuGame is None when that game has no green result.
    Handles "Halo 2 (USA).zip" and scene names like
    "Conker.Live.&.Reloaded.USA.XBOX-ZTM.rar"."""
    xefu_games()
    exact = _XEFU_STATE["exact"]
    keys, words = _xefu_file_keys(name)
    for key in keys:
        if key in exact:
            return exact[key], True
    candidates = [words]
    short = _xefu_without_prefix(words)                 # "MTV Celebrity Deathmatch"
    if short:
        candidates.append(short)
    for i, word in enumerate(words):
        if word in _XEFU_CUT_AFTER and i > 0:
            candidates.append(words[:i])
        if word in _XEFU_CUT_BEFORE and i + 1 < len(words):
            candidates.append(words[i + 1:])
    for candidate in candidates:
        found = _xefu_find_words(candidate)
        if found is not _XEFU_MISSING:
            return found, True
    return None, False


def xefu_compat_for_name(name: str):
    """The XefuGame for an Original Xbox file name, or None if it has no green result."""
    return xefu_lookup(name)[0]


def xefu_versions_text(game, with_status: bool = False) -> str:
    """ "xefu7, xefu2021c" (with_status: "xefu7 (Playable, CFG), ...")."""
    parts = []
    for version, status, cfg in game.works:
        if with_status:
            extra = status + (", CFG" if cfg else "")
            parts.append(f"{version} ({extra})")
        else:
            parts.append(version + (" (CFG)" if cfg else ""))
    return ", ".join(parts)


def search_catalog(entries: list, term: str, extra_text=None) -> list:
    """Every entry whose file name (plus extra_text(entry), if given) holds all the words."""
    tokens = search_tokens(term)
    if not tokens:
        return list(entries)
    found = []
    for entry in entries:
        text = entry.name
        if extra_text is not None:
            try:
                text += " " + (extra_text(entry) or "")
            except Exception:
                pass
        if search_text_matches(tokens, text):
            found.append(entry)
    return found


def stfs_content_type(path: Path) -> int | None:
    """The content type in an Xbox 360 package's header (0x00030000 theme, 0x00020000
    gamer picture), or None if it is not a package."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(STFS_CONTENT_TYPE_OFFSET + 4)
    except OSError:
        return None
    if len(head) < STFS_CONTENT_TYPE_OFFSET + 4 or head[:4] not in STFS_MAGICS:
        return None
    return int.from_bytes(head[STFS_CONTENT_TYPE_OFFSET:STFS_CONTENT_TYPE_OFFSET + 4], "big")


def _catalog_files(game_dir: Path, limit: int = 5000) -> list[Path]:
    found: list[Path] = []
    try:
        for path in sorted(game_dir.rglob("*")):
            if len(found) >= limit:
                break
            try:
                if path.is_file() and path.name != PROCESSING_MARKER_NAME:
                    found.append(path)
            except OSError:
                continue
    except Exception:
        pass
    return found


def find_catalog_packages(game_dir: Path, fallback_dir: str = THEME_CONTENT_TYPE_DIR
                          ) -> list[tuple[Path, str]]:
    """(file, content type folder) for every package in an extracted theme / gamer picture download.

    Each package goes where its own header says: themes to 00030000, gamer
    pictures to 00020000 (theme packs often carry a gamer picture pack too).
    Any other package goes to fallback_dir. If nothing in there has a package
    header, every file except readmes and pictures is sent to fallback_dir.
    """
    files = _catalog_files(game_dir)
    packages = []
    for path in files:
        ctype = stfs_content_type(path)
        if ctype is None:
            continue
        packages.append((path, _STFS_DIR_BY_TYPE.get(ctype, fallback_dir)))
    if packages:
        return packages
    return [(p, fallback_dir) for p in files if p.suffix.lower() not in CATALOG_JUNK_SUFFIXES]


def _sniff_catalog_folder(game_dir: Path) -> Catalog | None:
    """For folders whose download is not known: only theme / gamer picture packages, nothing else."""
    try:
        if not game_dir.is_dir() or (game_dir / "GOD_Output").exists():
            return None
        _root, title_dirs = find_content_upload_roots(game_dir)
        if title_dirs:
            return None                         # laid out as title ID folders: the normal content upload keeps that
    except Exception:
        return None
    seen = set()
    for path in _catalog_files(game_dir, limit=400):
        if path.suffix.lower() in (".iso", ".xex", ".xbe"):
            return None
        ctype = stfs_content_type(path)
        if ctype is None:
            continue
        if ctype not in _STFS_DIR_BY_TYPE:
            return None
        seen.add(ctype)
    if STFS_CONTENT_THEME in seen:
        return THEME_CATALOG
    if STFS_CONTENT_GAMERPIC in seen:
        return GAMERPIC_CATALOG
    return None


def catalog_folder_kind(game_dir: Path, entry=None) -> Catalog | None:
    """The catalog an extracted download belongs to (so it goes under FFFE07D1), or None."""
    cat = catalog_for_entry(entry)
    if cat is not None or entry is not None:
        return cat
    try:
        known = catalog_entry_for_filename(Path(game_dir).name)
        if known is not None:
            return catalog_for_entry(known)
        return _sniff_catalog_folder(Path(game_dir))
    except Exception:
        return None


def is_catalog_folder(game_dir: Path, entry=None) -> bool:
    return catalog_folder_kind(game_dir, entry) is not None


def catalog_remote_root(kind: str, remote_dir: str = "", fallback_path: str = "") -> str:
    """Content/0000000000000000/FFFE07D1 on the drive the upload goes to.

    FTP paths start with the drive (/Hdd1/...): it is taken from the folder that
    was picked, then from the FTP default folder, and is Hdd1 otherwise. A drive
    opened directly has no drive part (/Content/...).
    """
    if kind == "ftp":
        base = forced_content_destination(remote_dir or "/", drive_prefixed=True,
                                          fallback_path=fallback_path)
    else:
        base = forced_content_destination(remote_dir or "/", drive_prefixed=False)
    return ftp_join(base, THEME_TITLE_ID)


def catalog_upload_destination(kind: str, remote_dir: str = "", fallback_path: str = "",
                               content_dir: str = THEME_CONTENT_TYPE_DIR) -> str:
    """Where a catalog's packages end up: .../Content/0000000000000000/FFFE07D1/<content_dir>."""
    return ftp_join(catalog_remote_root(kind, remote_dir, fallback_path), content_dir)


def _stage_catalog_files(packages: list, staging: Path, max_name: int = HDD_MAX_NAME) -> None:
    """Copy the packages into staging/<content type>/, flat, with names the console accepts."""
    taken: dict = {}
    for source, sub in packages:
        folder = staging / sub
        folder.mkdir(parents=True, exist_ok=True)
        name = clean_upload_name(source.name, console=True)
        if len(name) > max_name:
            stem, ext = os.path.splitext(name)
            name = (stem[:max_name - len(ext)] + ext) if 0 < len(ext) < max_name // 2 else name[:max_name]
        used = taken.setdefault(sub, set())
        candidate, number = name, 2
        while candidate.lower() in used:
            tag = f"_{number}"
            candidate = name[:max_name - len(tag)] + tag
            number += 1
        used.add(candidate.lower())
        shutil.copyfile(source, folder / candidate)


def _upload_catalog(kind: str, cat: Catalog, cfg, game_dir: Path, remote_dir: str,
                    progress_callback=None, cancel_event=None,
                    max_parallel: int | None = None) -> tuple[bool, str]:
    where = "FTP" if kind == "ftp" else "HDD"
    try:
        if not game_dir.is_dir() or not any(game_dir.iterdir()):
            return False, f"Nothing to upload for {game_dir.name} - extract it first."
    except Exception:
        return False, f"Nothing to upload for {game_dir.name} - extract it first."

    packages = find_catalog_packages(game_dir, fallback_dir=cat.content_dir)
    if not packages:
        return False, f"No {cat.noun} files were found in {game_dir.name}."

    fallback = getattr(cfg, "default_path", "") if kind == "ftp" else ""
    root = catalog_remote_root(kind, remote_dir, fallback_path=fallback)
    subs = sorted({sub for _path, sub in packages})
    log_event(f"{where}: {game_dir.name} is an Xbox 360 {cat.noun} - forcing {len(packages)} file(s) into "
              + ", ".join(ftp_join(root, sub) for sub in subs))

    staging = Path(tempfile.mkdtemp(prefix=f"bman_{cat.key}_"))
    try:
        try:
            _stage_catalog_files(packages, staging)
        except OSError as exc:
            return False, f"Could not prepare the {cat.noun} files: {exc}"
        if kind == "ftp":
            return ftp_upload_tree(cfg, staging, root, progress_callback=progress_callback,
                                   cancel_event=cancel_event, max_parallel=max_parallel)
        return hdd_upload_tree(cfg, staging, root, progress_callback=progress_callback,
                               cancel_event=cancel_event)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def upload_catalog_via_ftp(cfg: FtpConfig, game_dir: Path, remote_dir: str = "",
                           catalog: Catalog = THEME_CATALOG, progress_callback=None,
                           cancel_event=None, max_parallel: int | None = None) -> tuple[bool, str]:
    return _upload_catalog("ftp", catalog, cfg, game_dir, remote_dir,
                           progress_callback, cancel_event, max_parallel)


def upload_catalog_to_hdd(cfg: HddConfig, game_dir: Path, remote_dir: str = "",
                          catalog: Catalog = THEME_CATALOG, progress_callback=None,
                          cancel_event=None) -> tuple[bool, str]:
    return _upload_catalog("hdd", catalog, cfg, game_dir, remote_dir, progress_callback, cancel_event)


# ---------------------------------------------------------------------------
# xboxgamer.pics: the actual pictures for the Gamer Pictures tab.
#
# Every archive pack name ends in a title ID (..._373907D2.zip) and the site
# files its pictures as titles/<title id>/<picture id>.png, so packs are
# matched to the site by title ID. /api/titles.json lists every title in one
# go; a title's own page lists its picture IDs.
# ---------------------------------------------------------------------------

GAMERPICS_SITE = "https://xboxgamer.pics"
GAMERPICS_ASSETS = "https://assets.xboxgamer.pics/titles"
GAMERPICS_TITLES_API = f"{GAMERPICS_SITE}/api/titles.json"
GAMERPICS_SITE_CACHE_FILE = "gamerpics_site_cache.json"
GAMERPICS_SITE_TTL_SECONDS = 7 * 24 * 60 * 60
GAMERPICS_TILE_ID = "8000"               # the title's tile, not a gamer picture
GAMERPICS_IMAGE_CACHE_MAX = 800

_GP_LOCK = threading.Lock()
_GP_STATE: dict = {"titles": None, "titles_at": 0.0, "pics": {}, "loaded": False}
_GP_IMAGES: "OrderedDict[str, bytes]" = OrderedDict()
_GP_LOCAL = threading.local()


def _gp_session():
    session = getattr(_GP_LOCAL, "session", None)
    if session is None:
        session = curl_cffi_requests.Session(impersonate="chrome124")
        _GP_LOCAL.session = session
    return session


def _gp_cache_path() -> Path:
    return CORE_DIR / GAMERPICS_SITE_CACHE_FILE


def _gp_load_disk() -> None:
    with _GP_LOCK:
        if _GP_STATE["loaded"]:
            return
        _GP_STATE["loaded"] = True
    try:
        blob = json.loads(_gp_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(blob, dict):
        return
    titles = blob.get("titles")
    pics = blob.get("pics")
    with _GP_LOCK:
        if isinstance(titles, dict) and _GP_STATE["titles"] is None:
            _GP_STATE["titles"] = titles
            try:
                _GP_STATE["titles_at"] = float(blob.get("titles_at", 0))
            except (TypeError, ValueError):
                _GP_STATE["titles_at"] = 0.0
        if isinstance(pics, dict):
            for key, value in pics.items():
                if isinstance(value, dict) and isinstance(value.get("ids"), list):
                    _GP_STATE["pics"].setdefault(key, value)


def _gp_save_disk() -> None:
    with _GP_LOCK:
        payload = {"titles": _GP_STATE["titles"], "titles_at": _GP_STATE["titles_at"],
                   "pics": dict(_GP_STATE["pics"])}
    path = _gp_cache_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def load_gamerpic_site_titles(force: bool = False) -> dict:
    """{title id (lower case): {"name", "type", "url", "count"}} for every title on xboxgamer.pics.

    Cached for a week; an older saved copy is used if the site cannot be reached.
    """
    _gp_load_disk()
    with _GP_LOCK:
        titles, fetched_at = _GP_STATE["titles"], _GP_STATE["titles_at"]
    fresh = titles is not None and 0 <= time.time() - fetched_at <= GAMERPICS_SITE_TTL_SECONDS
    if fresh and not force:
        return titles

    error = None
    try:
        resp = _gp_session().get(GAMERPICS_TITLES_API, timeout=30)
        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}"
        else:
            data = resp.json()
            rows = data if isinstance(data, list) else (data.get("titles") or [])
            parsed = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tid = str(row.get("id") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{8}", tid):
                    continue
                try:
                    count = int(row.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0
                parsed[tid] = {"name": str(row.get("name") or "").strip(),
                               "type": str(row.get("type") or ""),
                               "url": str(row.get("url") or ""), "count": count}
            if parsed:
                with _GP_LOCK:
                    _GP_STATE["titles"], _GP_STATE["titles_at"] = parsed, time.time()
                _gp_save_disk()
                log_event(f"Gamer Pictures: {len(parsed):,} titles loaded from xboxgamer.pics.")
                return parsed
            error = "no titles in the answer"
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__

    if titles is not None:
        log_event(f"Gamer Pictures: could not refresh xboxgamer.pics ({error}) - using the saved copy.")
        return titles
    log_event(f"Gamer Pictures: xboxgamer.pics could not be reached ({error}) - no previews.")
    return {}


def gamerpic_site_title(title_id: str) -> dict | None:
    """The xboxgamer.pics title for a title ID, from what is already loaded (never goes online)."""
    tid = (title_id or "").strip().lower()
    if not tid:
        return None
    _gp_load_disk()
    with _GP_LOCK:
        titles = _GP_STATE["titles"] or {}
    return titles.get(tid)


def gamerpic_site_url(title_id: str) -> str:
    """The title's page on xboxgamer.pics (the site's home page when it has none)."""
    title = gamerpic_site_title(title_id)
    url = (title or {}).get("url") or ""
    return GAMERPICS_SITE + url if url.startswith("/") else GAMERPICS_SITE


def gamerpic_image_url(title_id: str, picture_id: str) -> str:
    return f"{GAMERPICS_ASSETS}/{(title_id or '').lower()}/{(picture_id or '').lower()}.png"


def fetch_gamerpic_ids(title_id: str, force: bool = False) -> list[str]:
    """The picture IDs ('20001', '2800a', ...) xboxgamer.pics has for a title, in the site's order.

    Read from the title's page (cached for a week). Empty when the site does not have the title.
    """
    tid = (title_id or "").strip().lower()
    title = gamerpic_site_title(tid)
    if not title or not title.get("url"):
        return []
    with _GP_LOCK:
        cached = _GP_STATE["pics"].get(tid)
    if cached and not force:
        try:
            if 0 <= time.time() - float(cached.get("at", 0)) <= GAMERPICS_SITE_TTL_SECONDS:
                return list(cached["ids"])
        except (TypeError, ValueError):
            pass

    ids: list[str] = []
    try:
        resp = _gp_session().get(GAMERPICS_SITE + title["url"], timeout=20)
        if resp.status_code == 200:
            # The page also shows pictures from other titles (a "random" strip):
            # only this title's own folder counts.
            pattern = re.compile(r"assets\.xboxgamer\.pics/titles/" + re.escape(tid)
                                 + r"/([0-9a-fA-F]+)\.png")
            seen = set()
            for match in pattern.finditer(resp.text):
                pid = match.group(1).lower()
                if pid != GAMERPICS_TILE_ID and pid not in seen:
                    seen.add(pid)
                    ids.append(pid)
        else:
            log_event(f"Gamer Pictures: xboxgamer.pics answered HTTP {resp.status_code} for {tid}.")
    except Exception as exc:
        log_event(f"Gamer Pictures: could not read the pictures for {tid} - {exc}")
        if cached:
            return list(cached.get("ids") or [])
        return []

    if ids or not cached:
        with _GP_LOCK:
            _GP_STATE["pics"][tid] = {"ids": ids, "at": time.time()}
        _gp_save_disk()
    return ids or list((cached or {}).get("ids") or [])


def fetch_gamerpic_image(url: str) -> bytes | None:
    """One picture's PNG bytes (kept in memory for the session), or None."""
    with _GP_LOCK:
        data = _GP_IMAGES.get(url)
        if data is not None:
            _GP_IMAGES.move_to_end(url)
            return data
    data = load_saved_image(url, GAMERPIC_IMAGE_DIR)
    if data is None:
        try:
            resp = _gp_session().get(url, timeout=15)
            if resp.status_code != 200 or not resp.content:
                return None
            data = resp.content
        except Exception:
            return None
        save_image(url, data, GAMERPIC_IMAGE_DIR)
    with _GP_LOCK:
        _GP_IMAGES[url] = data
        while len(_GP_IMAGES) > GAMERPICS_IMAGE_CACHE_MAX:
            _GP_IMAGES.popitem(last=False)
    return data


# ---------------------------------------------------------------------------
# Theme wallpapers: the actual pictures for the Themes tab.
#
#   https://archive.org/details/xbox360-theme-wallpapers-zip
#
# The item is a few big zips ("Xbox 360 themes images - part N of 4.zip").
# Inside, every theme has its own folder named exactly like its pack in
# xbox_360_themes, minus the .zip:
#
#   <pack name>/<package hash>_images/Wallpaper1.png  (... Wallpaper4, .png or .jpg)
#
# so packs are matched by file name. archive.org lists a zip's contents at
# /download/<item>/<zip>/ and hands out any single file in it at
# /download/<item>/<zip>/<path>, so a preview never downloads a whole zip.
#
# archive.org shows the names of these zips decoded as code page 866 (the
# zips store UTF-8 names without saying so): 'Thème' is listed as 'Th├иme'.
# For matching, names are turned back into UTF-8; for downloading, the name
# exactly as archive.org lists it is kept, because that is the one it serves.
# ---------------------------------------------------------------------------

WALLPAPER_SOURCE = "xbox360-theme-wallpapers-zip"
WALLPAPER_CACHE_FILE = "theme_wallpapers_cache.json"
WALLPAPER_CACHE_VERSION = 1
WALLPAPER_TTL_SECONDS = 7 * 24 * 60 * 60
WALLPAPER_THUMB_DIR = "theme_wallpaper_thumbs"
WALLPAPER_THUMB_SIZE = (480, 270)        # kept on disk; the strip shows them smaller
WALLPAPER_THUMB_DISK_MAX = 3000          # thumbnails kept on disk before the oldest go
WALLPAPER_THUMB_MEMORY_MAX = 200

# One row of archive.org's zip listing: <tr><td><a href="//archive.org/download/...">path</a><td>...
_WALLPAPER_ROW_RE = re.compile(r'<tr><td><a href="([^"]+)">([^<]+)</a>', re.IGNORECASE)
_WALLPAPER_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
_WALLPAPER_NUMBER_RE = re.compile(r"(\d+)")

_WP_LOCK = threading.Lock()
_WP_STATE: dict = {"index": None, "at": 0.0, "loaded": False}
_WP_THUMBS: "OrderedDict[str, bytes]" = OrderedDict()
_WP_LOCAL = threading.local()
_WP_WRITES = itertools.count(1)


def _wp_session():
    session = getattr(_WP_LOCAL, "session", None)
    if session is None:
        session = curl_cffi_requests.Session(impersonate="chrome124")
        _WP_LOCAL.session = session
    return session


def _wallpaper_fix_name(name: str) -> str:
    """'Th├иme_585708B8' -> 'Thème_585708B8'. Names that are fine already come back unchanged."""
    try:
        return name.encode("cp866").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _wallpaper_key(name: str) -> str:
    """The matching key for a pack: its file name without .zip/.rar/.7z, lower case."""
    stem = Path(name or "").name
    lower = stem.lower()
    for suffix in CATALOG_ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    return stem.strip().lower()


def _wallpaper_sort_key(path: str) -> tuple:
    base = path.rsplit("/", 1)[-1]
    match = _WALLPAPER_NUMBER_RE.search(base)
    return (int(match.group(1)) if match else 10 ** 6, base.lower())


def _wp_cache_path() -> Path:
    return CORE_DIR / WALLPAPER_CACHE_FILE


def _wp_load_disk() -> None:
    with _WP_LOCK:
        if _WP_STATE["loaded"]:
            return
        _WP_STATE["loaded"] = True
    try:
        blob = json.loads(_wp_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(blob, dict) or blob.get("version") != WALLPAPER_CACHE_VERSION:
        return
    index = blob.get("index")
    if not isinstance(index, dict) or not index:
        return
    with _WP_LOCK:
        if _WP_STATE["index"] is None:
            _WP_STATE["index"] = index
            try:
                _WP_STATE["at"] = float(blob.get("at", 0))
            except (TypeError, ValueError):
                _WP_STATE["at"] = 0.0


def _wp_save_disk() -> None:
    with _WP_LOCK:
        payload = {"version": WALLPAPER_CACHE_VERSION, "at": _WP_STATE["at"],
                   "index": _WP_STATE["index"]}
    path = _wp_cache_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _wallpaper_zip_names(session) -> list[str]:
    """The zips in the wallpaper item, in name order (part 1, part 2, ...)."""
    resp = session.get(f"{ARCHIVE_METADATA_URL}/{WALLPAPER_SOURCE}", timeout=30)
    if resp.status_code != 200:
        raise ArchiveError(f"archive.org answered HTTP {resp.status_code}")
    names = [f.get("name") or "" for f in resp.json().get("files", []) or []]
    return sorted((n for n in names if n.lower().endswith(".zip")), key=str.lower)


def _wallpaper_listing(session, zip_name: str) -> list[tuple[str, str]]:
    """(download URL, path inside the zip) for every picture in one zip, from archive.org's listing."""
    url = f"{ARCHIVE_BASE_URL}/download/{WALLPAPER_SOURCE}/{quote(zip_name)}/"
    resp = session.get(url, timeout=120)
    if resp.status_code != 200:
        raise ArchiveError(f"archive.org answered HTTP {resp.status_code} for {zip_name}")
    import html as _html
    rows = []
    for href, text in _WALLPAPER_ROW_RE.findall(resp.text):
        path = _html.unescape(text).strip()
        if not path.lower().endswith(_WALLPAPER_IMAGE_SUFFIXES):
            continue
        href = _html.unescape(href)
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = ARCHIVE_BASE_URL + href
        if not is_archive_host(href):
            continue                            # only ever follow links back to archive.org
        rows.append((href, path))
    return rows


def _build_wallpaper_index(listings: list[list[tuple[str, str]]]) -> dict:
    """{pack key: [download URL, ...]} with each pack's wallpapers in order (1, 2, 3, 4)."""
    grouped: dict = {}
    for rows in listings:
        for href, path in rows:
            top = path.split("/", 1)[0]
            key = _wallpaper_key(_wallpaper_fix_name(top))
            if key:
                grouped.setdefault(key, []).append((path, href))
    return {key: [href for _p, href in sorted(items, key=lambda item: _wallpaper_sort_key(item[0]))]
            for key, items in grouped.items()}


def load_theme_wallpapers(force: bool = False) -> dict:
    """{pack key: [wallpaper URL, ...]} for every theme in the wallpaper item.

    Cached for a week; an older saved copy is used if archive.org cannot be
    reached. Empty when there is nothing at all.
    """
    _wp_load_disk()
    with _WP_LOCK:
        index, fetched_at = _WP_STATE["index"], _WP_STATE["at"]
    fresh = index is not None and 0 <= time.time() - fetched_at <= WALLPAPER_TTL_SECONDS
    if fresh and not force:
        return index

    error = None
    try:
        session = _wp_session()
        zips = _wallpaper_zip_names(session)
        if not zips:
            raise ArchiveError("no zips in the item")

        def one(name):
            last = None
            for attempt in range(2):                # archive.org often answers a busy node with 503
                part = curl_cffi_requests.Session(impersonate="chrome124")
                try:
                    return _wallpaper_listing(part, name)
                except Exception as exc:
                    last = exc
                finally:
                    try:
                        part.close()
                    except Exception:
                        pass
                if attempt == 0:
                    time.sleep(3)
            log_event(f"Themes: could not list {name} - {last}")
            return None

        with ThreadPoolExecutor(max_workers=min(4, len(zips))) as pool:
            listings = list(pool.map(one, zips))
        failed = [name for name, rows in zip(zips, listings) if rows is None]
        built = _build_wallpaper_index([rows for rows in listings if rows])
        if built:
            if failed and index:
                # Keep what the saved copy had for the zips that could not be read this time.
                built = {**index, **built}
            with _WP_LOCK:
                # A partial list is used now but is not saved as fresh, so it is tried again next time.
                _WP_STATE["index"], _WP_STATE["at"] = built, (0.0 if failed else time.time())
            _wp_save_disk()
            pictures = sum(len(v) for v in built.values())
            log_event(f"Themes: wallpapers for {len(built):,} themes ({pictures:,} pictures) "
                      f"loaded from {WALLPAPER_SOURCE}"
                      + (f" - {len(failed)} of {len(zips)} zips could not be read." if failed else "."))
            return built
        error = "no wallpapers in the listings"
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__

    if index is not None:
        log_event(f"Themes: could not refresh the wallpaper list ({error}) - using the saved copy.")
        return index
    log_event(f"Themes: the wallpaper list could not be loaded ({error}) - no previews.")
    return {}


def theme_wallpaper_urls(name: str) -> list[str]:
    """The wallpaper URLs for a theme pack's file name, from what is already loaded (never goes online)."""
    _wp_load_disk()
    with _WP_LOCK:
        index = _WP_STATE["index"] or {}
    return list(index.get(_wallpaper_key(name)) or [])


def theme_wallpaper_page_url() -> str:
    return f"https://archive.org/details/{WALLPAPER_SOURCE}"


def _wp_thumb_dir() -> Path:
    return CORE_DIR / WALLPAPER_THUMB_DIR


def _wp_thumb_path(url: str) -> Path:
    return _wp_thumb_dir() / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg")


def _wp_prune_thumbs() -> None:
    try:
        files = [p for p in _wp_thumb_dir().glob("*.jpg") if p.is_file()]
        if len(files) <= WALLPAPER_THUMB_DISK_MAX:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for path in files[:len(files) - WALLPAPER_THUMB_DISK_MAX]:
            try:
                path.unlink()
            except OSError:
                pass
    except Exception:
        pass


def fetch_theme_wallpaper(url: str) -> bytes | None:
    """A small JPEG of one wallpaper (memory, then disk, then archive.org), or None.

    The full picture (often a 1 MB+ PNG) is only downloaded once; what is kept
    is a 480x270 copy.
    """
    with _WP_LOCK:
        data = _WP_THUMBS.get(url)
        if data is not None:
            _WP_THUMBS.move_to_end(url)
            return data

    path = _wp_thumb_path(url)
    data = None
    try:
        data = path.read_bytes() or None
    except OSError:
        data = None

    if data is None:
        try:
            resp = _wp_session().get(url, timeout=60)
            if resp.status_code != 200 or not resp.content:
                return None
            from PIL import Image as _Image
            import io as _io
            picture = _Image.open(_io.BytesIO(resp.content))
            picture.draft("RGB", WALLPAPER_THUMB_SIZE)     # quicker for big JPEGs
            picture = picture.convert("RGB")
            picture.thumbnail(WALLPAPER_THUMB_SIZE)
            if picture.size != WALLPAPER_THUMB_SIZE:
                # not 16:9: letterbox it so the strip never stretches it
                canvas = _Image.new("RGB", WALLPAPER_THUMB_SIZE, (0, 0, 0))
                canvas.paste(picture, ((WALLPAPER_THUMB_SIZE[0] - picture.width) // 2,
                                       (WALLPAPER_THUMB_SIZE[1] - picture.height) // 2))
                picture = canvas
            out = _io.BytesIO()
            picture.save(out, "JPEG", quality=85)
            data = out.getvalue()
        except Exception:
            return None
        # Unique per thread: the same picture can be fetched twice at once (a theme clicked twice).
        tmp = path.with_name(f"{path.stem}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)
            if next(_WP_WRITES) % 50 == 0:
                _wp_prune_thumbs()
        except OSError:
            try:
                tmp.unlink()            # Windows refuses the replace while another thread reads the file
            except OSError:
                pass

    with _WP_LOCK:
        _WP_THUMBS[url] = data
        while len(_WP_THUMBS) > WALLPAPER_THUMB_MEMORY_MAX:
            _WP_THUMBS.popitem(last=False)
    return data


DEFAULT_ACCENT = "#1F6AA5"
ACCENT_SETTING_KEY = "accent_color"

ACCENT_PRESETS = (
    ("Default blue", "#1F6AA5"),
    ("Sky", "#0284C7"),
    ("Cyan", "#0891B2"),
    ("Teal", "#0D9488"),
    ("Emerald", "#15803D"),
    ("Olive", "#4D7C0F"),
    ("Amber", "#B45309"),
    ("Orange", "#EA580C"),
    ("Red", "#B91C1C"),
    ("Rose", "#BE185D"),
    ("Pink", "#DB2777"),
    ("Purple", "#7C3AED"),
    ("Violet", "#6D28D9"),
    ("Indigo", "#4F46E5"),
    ("Steel", "#475569"),
    ("Graphite", "#52525B"),
)

def normalize_hex(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return "#" + text.upper()

def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    text = (normalize_hex(value) or DEFAULT_ACCENT).lstrip("#")
    return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))

def _rgb_to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(round(c * 255)))) for c in rgb))

def _lightness(value: str) -> float:
    return colorsys.rgb_to_hls(*_hex_to_rgb(value))[1]

def _luminance(value: str) -> float:
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in _hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def contrast_text_for(value: str) -> str:
    return "#141414" if _luminance(value) > 0.45 else "#DCE4EE"

def _is_theme_blue(value) -> bool:
    if not isinstance(value, str) or not value.startswith("#"):
        return False
    normalized = normalize_hex(value)
    if normalized is None:
        return False
    hue, light, sat = colorsys.rgb_to_hls(*_hex_to_rgb(normalized))
    return 185 <= hue * 360 <= 255 and sat >= 0.15 and 0.12 <= light <= 0.75

_BASE_THEME = copy.deepcopy(ctk.ThemeManager.theme)

def _collect_blues(node, found: set) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _collect_blues(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect_blues(value, found)
    elif _is_theme_blue(node):
        found.add(normalize_hex(node))

_THEME_BLUES: set[str] = set()
_collect_blues(_BASE_THEME, _THEME_BLUES)
_THEME_BLUES.update({"#1F6AA5", "#144870", "#3B8ED0", "#36719F"})

_REFERENCE_HUE, _REFERENCE_LIGHTNESS, _REFERENCE_SATURATION = colorsys.rgb_to_hls(*_hex_to_rgb(DEFAULT_ACCENT))

def _shade(accent: str, light_factor: float, sat_factor: float = 1.0) -> str:
    hue, light, sat = colorsys.rgb_to_hls(*_hex_to_rgb(accent))
    return _rgb_to_hex(colorsys.hls_to_rgb(
        hue,
        max(0.05, min(0.93, light * light_factor)),
        max(0.0, min(1.0, sat * sat_factor)),
    ))

_BLUE_FACTORS = {}
for _blue in _THEME_BLUES:
    _blue_light, _blue_sat = colorsys.rgb_to_hls(*_hex_to_rgb(_blue))[1:]
    _BLUE_FACTORS[_blue] = (
        _blue_light / _REFERENCE_LIGHTNESS,
        (_blue_sat / _REFERENCE_SATURATION) if _REFERENCE_SATURATION else 1.0,
    )

_HOVER_FACTOR = _BLUE_FACTORS["#144870"][0]

def accent_palette(accent: str) -> dict:
    accent = normalize_hex(accent) or DEFAULT_ACCENT
    if accent == DEFAULT_ACCENT:
        return {blue: blue for blue in _THEME_BLUES}
    return {blue: _shade(accent, *_BLUE_FACTORS[blue]) for blue in _THEME_BLUES}

_ACCENT_TEXT_KEYS = {
    "CTkButton": ("text_color",),
    "CTkSegmentedButton": ("text_color",),
    "CTkOptionMenu": ("text_color",),
    "CTkCheckBox": ("checkmark_color",),
}

CURRENT_ACCENT = DEFAULT_ACCENT

def current_accent() -> str:
    return CURRENT_ACCENT

def accent_fg() -> str:
    return _shade(CURRENT_ACCENT, 1.0)

def accent_hover() -> str:
    return _shade(CURRENT_ACCENT, _HOVER_FACTOR)

def _substitute_colors(node, mapping):
    if isinstance(node, dict):
        return {key: _substitute_colors(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute_colors(value, mapping) for value in node]
    if isinstance(node, str):
        replacement = mapping.get(normalize_hex(node))
        if replacement is not None:
            return replacement
    return node

def set_accent(accent: str) -> str:
    global CURRENT_ACCENT
    accent = normalize_hex(accent) or DEFAULT_ACCENT
    CURRENT_ACCENT = accent

    theme = _substitute_colors(_BASE_THEME, accent_palette(accent))

    ink = contrast_text_for(_shade(accent, 1.0))
    if ink != "#DCE4EE":
        for section_name, keys in _ACCENT_TEXT_KEYS.items():
            section = theme.get(section_name)
            if not isinstance(section, dict):
                continue
            for key in keys:
                if key in section:
                    section[key] = [ink, ink]

    ctk.ThemeManager.theme = theme
    return accent

_LIVE_COLOR_OPTIONS = (
    "fg_color", "hover_color", "progress_color", "button_color",
    "button_hover_color", "border_color", "selected_color",
    "selected_hover_color", "unselected_color", "unselected_hover_color",
    "label_fg_color", "scrollbar_button_color", "scrollbar_button_hover_color",
)

def _swapped(value, mapping):
    if isinstance(value, str):
        return mapping.get(normalize_hex(value))
    if isinstance(value, (list, tuple)):
        result = [mapping.get(normalize_hex(item), item) if isinstance(item, str) else item
                  for item in value]
        return result if result != list(value) else None
    return None

def _retheme_widget(widget, mapping, text_mapping) -> None:
    if not getattr(widget, "_accent_locked", False):
        recoloured = False
        for option in _LIVE_COLOR_OPTIONS:
            try:
                value = widget.cget(option)
            except Exception:
                continue
            replacement = _swapped(value, mapping)
            if replacement is None:
                continue
            try:
                widget.configure(**{option: replacement})
                recoloured = True
            except Exception:
                pass

        if recoloured:
            for option in ("text_color", "checkmark_color"):
                try:
                    value = widget.cget(option)
                except Exception:
                    continue
                replacement = _swapped(value, text_mapping)
                if replacement is None:
                    continue
                try:
                    widget.configure(**{option: replacement})
                except Exception:
                    pass

    for child in widget.winfo_children():
        _retheme_widget(child, mapping, text_mapping)

def retheme_widgets(root, old_accent: str, new_accent: str) -> None:
    old_palette = accent_palette(old_accent)
    new_palette = accent_palette(new_accent)

    mapping = {}
    for stock_blue, target in new_palette.items():
        mapping[stock_blue] = target
        mapping[normalize_hex(old_palette[stock_blue])] = target

    old_ink = contrast_text_for(_shade(old_accent, 1.0))
    new_ink = contrast_text_for(_shade(new_accent, 1.0))
    text_mapping = {normalize_hex(old_ink): new_ink, "#DCE4EE": new_ink}

    _retheme_widget(root, mapping, text_mapping)

def load_accent() -> str:
    return normalize_hex(load_settings().get(ACCENT_SETTING_KEY)) or DEFAULT_ACCENT

def save_accent(accent: str) -> None:
    data = load_settings()
    data[ACCENT_SETTING_KEY] = accent
    save_settings(data)

try:
    set_accent(load_accent())
except Exception as exc:
    print(f"Could not apply saved accent colour: {exc}")
    set_accent(DEFAULT_ACCENT)

def normalize_setting(spec: DownloadSetting, value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return spec.default
    steps = round((value - spec.minimum) / spec.step)
    value = spec.minimum + steps * spec.step
    value = max(spec.minimum, min(spec.maximum, value))
    return round(value, spec.decimals) if spec.decimals else int(round(value))

def get_download_settings() -> dict:
    stored = load_settings()
    return {spec.key: normalize_setting(spec, stored.get(spec.key, spec.default))
            for spec in DOWNLOAD_SETTINGS}

def apply_download_settings(downloader) -> None:
    values = get_download_settings()
    for spec in DOWNLOAD_SETTINGS:
        applied = values[spec.key] * spec.scale
        setattr(downloader, spec.attr, applied if spec.decimals else int(applied))

def _write_archive_credentials(creds: dict) -> None:
    (CORE_DIR / CREDS_FILE).write_text(json.dumps(creds, indent=2), encoding="utf-8")

def save_archive_credentials(user_val: str, sig_val: str) -> None:
    existing = grab_archive_credentials()
    _write_archive_credentials({
        "cookies": [
            {"name": "logged-in-user", "value": user_val, "domain": ".archive.org"},
            {"name": "logged-in-sig", "value": sig_val, "domain": ".archive.org"}
        ],
        "s3": existing.get("s3", {}),
        "screenname": existing.get("screenname", ""),
    })

def save_archive_s3_keys(access: str, secret: str) -> None:
    existing = grab_archive_credentials()
    _write_archive_credentials({
        "cookies": existing.get("cookies", []),
        "s3": {"access": access, "secret": secret},
        "screenname": existing.get("screenname", ""),
    })


# IS_FROZEN / EXE_PATH are set near the top of this file (they also handle Nuitka).

def parse_version(text: str) -> tuple:
    parts = []
    for chunk in str(text).strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)

def check_for_update(timeout: int = 10) -> dict | None:
    session = None
    try:
        session = curl_cffi_requests.Session(impersonate="chrome124")
        resp = session.get(UPDATE_MANIFEST_URL, timeout=timeout)
        if resp.status_code != 200:
            log_event(f"Update check: HTTP {resp.status_code}")
            return None
        data = resp.json()
        remote = str(data.get("version", "")).strip()
        if not remote or not data.get("url"):
            return None
        if parse_version(remote) <= parse_version(APP_VERSION):
            log_event(f"Update check: running {APP_VERSION}, latest is {remote} - up to date.")
            return None
        log_event(f"Update check: {remote} available (running {APP_VERSION}).")
        return data
    except Exception as exc:
        log_event(f"Update check failed: {exc}")
        return None
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

UPDATE_DOWNLOAD_ATTEMPTS = 3


class _UpdateRejected(Exception):
    """The file that arrived is wrong, so downloading it again will not help."""


class _UpdateRetry(Exception):
    """The download failed in a way that is worth another attempt."""


def _update_sibling(tag: str) -> Path:
    return EXE_PATH.with_name(EXE_PATH.name + tag)


def _can_self_update() -> bool:
    return os.name == "nt" and IS_FROZEN


def _retry_os(action, tries: int = 12, delay: float = 0.25):
    # Antivirus and the search indexer hold a freshly written exe open for a
    # moment, which makes the first rename fail with "access denied".
    last = None
    for _ in range(tries):
        try:
            return action()
        except OSError as exc:
            last = exc
            time.sleep(delay)
    raise last


def _download_update_once(url: str, part: Path, want_sha: str, progress_callback) -> None:
    session = curl_cffi_requests.Session(impersonate="chrome124")
    try:
        resp = session.get(url, stream=True, timeout=60, allow_redirects=True)
        if resp.status_code != 200:
            message = f"the server answered HTTP {resp.status_code}"
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _UpdateRetry(message)
            raise _UpdateRejected(message)

        total = 0
        if not resp.headers.get("Content-Encoding"):
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0

        digest = hashlib.sha256()
        received = 0
        head = b""
        with open(part, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if len(head) < 2:
                    head = (head + chunk[:2])[:2]
                handle.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress_callback:
                    progress_callback(int(received / total * 100) if total else 0, received, total)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        try:
            session.close()
        except Exception:
            pass

    if total and received != total:
        raise _UpdateRetry(f"the download was cut short at {format_size(received)} of {format_size(total)}")
    if received < MIN_UPDATE_BYTES:
        raise _UpdateRejected(f"only {format_size(received)} arrived, which looks like an error "
                              f"page rather than the app")
    if head != b"MZ":
        raise _UpdateRejected("the file that arrived is not a Windows program")
    if want_sha and digest.hexdigest() != want_sha:
        raise _UpdateRejected("the file does not match the checksum in the update manifest")


def download_update(manifest: dict, progress_callback=None) -> tuple[Path | None, str]:
    """Download and verify the new exe. Returns (path, "") or (None, reason)."""
    part = _update_sibling(".part")
    ready = _update_sibling(".new")
    url = str(manifest.get("url") or "")
    want_sha = str(manifest.get("sha256") or "").strip().lower()

    try:
        with open(part, "wb"):
            pass
    except OSError as exc:
        log_event(f"Update download: cannot write next to the app: {exc}")
        return None, (f"The app's folder can't be written to ({exc.strerror or exc}): "
                      f"{EXE_PATH.parent}\n"
                      f"Move the app to a folder you own (not Program Files) and try again.")

    error = ""
    for attempt in range(1, UPDATE_DOWNLOAD_ATTEMPTS + 1):
        try:
            _download_update_once(url, part, want_sha, progress_callback)
            error = ""
            break
        except _UpdateRejected as exc:
            error = str(exc)
            log_event(f"Update download rejected: {error}")
            break
        except Exception as exc:
            if isinstance(exc, _UpdateRetry):
                error = str(exc)
            else:
                error = "the connection failed or was cut off (details are in the Logs tab)"
            log_event(f"Update download attempt {attempt}/{UPDATE_DOWNLOAD_ATTEMPTS} failed: {exc}")
            if attempt < UPDATE_DOWNLOAD_ATTEMPTS:
                time.sleep(1.5 * attempt)

    if not error:
        try:
            _retry_os(lambda: os.replace(part, ready))
            log_event(f"Update downloaded and verified: {format_size(ready.stat().st_size)}")
            return ready, ""
        except OSError as exc:
            error = f"the downloaded file could not be saved ({exc})"
            log_event(f"Update download: {error}")

    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass
    return None, f"The update could not be downloaded - {error}. Your current version is untouched."


def apply_update(new_exe: Path) -> tuple[bool, str]:
    """Swap the verified exe into place. Returns (ok, reason).

    Windows will not let a running exe be deleted or overwritten, but it does
    let it be renamed. So the running exe is renamed aside and the new one takes
    its name. There is never a moment with no exe at the app's path, and if the
    second step fails the first one is undone.
    """
    if not _can_self_update():
        return False, "Updates only install into the packaged Windows .exe."

    old = _update_sibling(".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        old = _update_sibling(f".old{int(time.time())}")

    try:
        _retry_os(lambda: os.replace(EXE_PATH, old))
    except OSError as exc:
        log_event(f"Update swap: could not move the running exe aside: {exc}")
        return False, (f"Windows would not let the running app be moved aside ({exc}). "
                       f"Nothing was changed. The new version is saved next to the app "
                       f"as {new_exe.name}.")

    try:
        _retry_os(lambda: os.replace(new_exe, EXE_PATH))
    except OSError as exc:
        try:
            _retry_os(lambda: os.replace(old, EXE_PATH))
        except OSError as undo_exc:
            log_event(f"Update swap failed ({exc}) and could not be undone ({undo_exc}).")
            return False, (f"The update could not be put in place ({exc}). Your current version "
                           f"is now named {old.name} - rename it back to {EXE_PATH.name}.")
        log_event(f"Update swap failed and was undone: {exc}")
        return False, f"The update could not be put in place ({exc}). Your current version is untouched."

    log_event(f"Update swapped in. The previous version is parked as {old.name} until the next start.")
    return True, ""


def relaunch_app() -> bool:
    # A one-file PyInstaller exe tells its child where the unpacked files are
    # through the environment. Started with those variables, the new exe would
    # try to run out of this process's temp folder, which is deleted on exit.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("_PYI_", "_MEIPASS", "NUITKA_ONEFILE"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        subprocess.Popen(
            [str(EXE_PATH)], cwd=str(EXE_PATH.parent), env=env, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return True
    except Exception as exc:
        log_event(f"Could not restart after the update: {exc}")
        return False


def cleanup_update_leftovers(tries: int = 20, delay: float = 1.0) -> None:
    """Run once at startup: remove the parked old exe and any stale download."""
    if not IS_FROZEN:
        return
    stale_names = {EXE_PATH.name + ".part", EXE_PATH.name + ".new",
                   EXE_PATH.stem + ".new" + EXE_PATH.suffix}   # last one: the older updater's name
    old_prefix = EXE_PATH.name + ".old"

    parked = []
    try:
        for child in EXE_PATH.parent.iterdir():
            if child.name in stale_names:
                try:
                    child.unlink()
                except OSError:
                    pass
            elif child.name.startswith(old_prefix):
                parked.append(child)
    except OSError:
        return

    if not parked:
        return
    log_event(f"Update installed - now running version {APP_VERSION}.")

    # The previous version may still be shutting down, so its exe can stay
    # locked for a few seconds after this one starts.
    for _ in range(tries):
        remaining = []
        for path in parked:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                remaining.append(path)
        parked = remaining
        if not parked:
            return
        time.sleep(delay)
    log_event("Could not remove the previous version yet (" + ", ".join(p.name for p in parked)
              + ") - it will be removed on a later start.")

def clean_pyinstaller_temp() -> None:
    """Delete leftover PyInstaller unpack folders (%TEMP%\\_MEI*), run once at start.

    One-file PyInstaller exes (older Bman Downloader builds and other apps)
    unpack into _MEIxxxxx and leave it behind if they crash or are killed.
    A folder that can't be removed is skipped. Folders still in use by a
    running program are left alone: Windows won't rename a folder while files
    inside it are open, so each one is renamed first and only deleted if that
    works - deleting half of a running app's files would break it."""
    temp_dir = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    own = getattr(sys, "_MEIPASS", None)
    own = os.path.normcase(os.path.abspath(own)) if own else None
    removed = skipped = 0
    try:
        candidates = [p for p in temp_dir.iterdir() if p.name.upper().startswith("_MEI")]
    except OSError as exc:
        log_event(f"_MEI cleanup: could not read {temp_dir}: {exc}")
        return

    for folder in candidates:
        try:
            if os.path.normcase(os.path.abspath(folder)) == own:
                continue                                    # this app's own folder
            if not folder.is_dir():
                folder.unlink()                             # stray _MEI file
                removed += 1
                continue
            target = folder
            if not folder.name.endswith(".bmandel"):
                target = folder.with_name(folder.name + ".bmandel")
                os.rename(folder, target)                   # fails if it's in use
            shutil.rmtree(target)
            removed += 1
        except Exception:
            skipped += 1                                    # in use / no permission: next one
            continue

    if removed or skipped:
        log_event(f"_MEI cleanup: removed {removed} leftover folder(s) from {temp_dir}"
                  + (f", skipped {skipped} in use or locked." if skipped else "."))


class Index:
    """The searchable list of files from every archive.org source.

    status:
      "fresh"   - downloaded just now, or a saved copy that is still in date
      "stale"   - an out-of-date saved copy; the app opens with it and refreshes in the background
      "partial" - some archive.org sources failed; their entries come from the last saved copy
      "offline" - archive.org could not be reached and there is no saved copy
    """

    def __init__(self, entries: list[Files], cookies: list[dict], user_agent: str,
                 s3: dict | None = None, status: str = "fresh",
                 failed_sources: list[str] | None = None, fetched_at: float | None = None):
        self.entries = entries
        self.cookies = cookies
        self.user_agent = user_agent
        self.s3 = s3 or {}
        self.status = status
        self.failed_sources = list(failed_sources or [])
        self.fetched_at = fetched_at
        self._refresh_lock = threading.Lock()

    @property
    def needs_refresh(self) -> bool:
        return self.status != "fresh"

    @staticmethod
    def _sources_signature() -> str:
        return json.dumps(
            {"sources": sorted(ARCHIVE_SOURCES.items()), "content": sorted(CONTENT_SOURCES)},
            separators=(",", ":"),
        )

    @staticmethod
    def _cache_path() -> Path:
        return CORE_DIR / INDEX_CACHE_FILE

    @classmethod
    def _read_cache_blob(cls, ignore_age: bool = False):
        """(entries, fetched_at) from index_cache.json, or None.

        ignore_age=True also accepts an out-of-date copy, or one saved with a
        different source list (entries from sources no longer used are dropped)."""
        cache_path = cls._cache_path()
        if not cache_path.exists():
            return None
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(blob, dict) or blob.get("version") != INDEX_CACHE_VERSION:
            return None

        try:
            fetched_at = float(blob.get("fetched_at", 0))
        except (TypeError, ValueError):
            fetched_at = 0.0

        if not ignore_age:
            if blob.get("sources") != cls._sources_signature():
                return None
            age = time.time() - fetched_at
            ttl = INDEX_CACHE_PARTIAL_TTL_SECONDS if blob.get("partial") else INDEX_CACHE_TTL_SECONDS
            if age < 0 or age > ttl:
                return None

        valid_keys = {f.name for f in dataclass_fields(Files)}
        entries: list[Files] = []
        for raw in blob.get("entries", []) or []:
            try:
                entry = Files(**{k: v for k, v in raw.items() if k in valid_keys})
            except Exception:
                continue
            ident = entry.archive_identifier
            if ident not in ARCHIVE_SOURCES:
                continue
            # Follow the current source settings, not the ones the copy was saved with.
            entries.append(dataclass_replace(entry, allow_iso_conversion=ARCHIVE_SOURCES[ident],
                                             content_source=is_content_source(ident)))
        if not entries:
            return None
        return entries, fetched_at

    @classmethod
    def _read_cache(cls) -> list[Files] | None:
        hit = cls._read_cache_blob()
        return hit[0] if hit else None

    @classmethod
    def _write_cache(cls, entries: list[Files], partial: bool = False) -> None:
        cache_path = cls._cache_path()
        tmp_path = cache_path.with_suffix(".tmp")
        try:
            payload = {
                "version": INDEX_CACHE_VERSION,
                "fetched_at": time.time(),
                "sources": cls._sources_signature(),
                "partial": bool(partial),
                "entries": [asdict(e) for e in entries],
            }
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp_path, cache_path)
        except Exception:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    @staticmethod
    def _fetch_source(identifier: str, allow_iso: bool) -> list[Files]:
        """One source's file list. Raises if archive.org didn't answer properly."""
        session = curl_cffi_requests.Session(impersonate="chrome124")
        try:
            resp = session.get(f"https://archive.org/metadata/{identifier}",
                               timeout=INDEX_FETCH_TIMEOUT)
            if resp.status_code != 200:
                raise ArchiveError(f"HTTP {resp.status_code}")
            data = resp.json()
        finally:
            session.close()
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            # archive.org answers {} or {"error": ...} when it's struggling
            raise ArchiveError((data or {}).get("error") or "no file list in the reply")

        entries = []
        for f in files:
            fname = f.get("name")
            if not fname or is_excluded_file(fname):
                continue
            try:
                size_val = int(f.get("size")) if f.get("size") else None
            except (ValueError, TypeError):
                size_val = None
            entries.append(Files(
                id=fname,
                name=fname,
                size=size_val,
                date=f.get("mtime"),
                source_type="archive",
                archive_identifier=identifier,
                allow_iso_conversion=allow_iso,
                content_source=is_content_source(identifier),
            ))
        return entries

    @classmethod
    def _fetch_all(cls) -> tuple[dict[str, list[Files]], list[str]]:
        """({source: entries} for every source that loaded, [sources that failed]).

        The first source is tried on its own: if archive.org is down, that
        answers it in a few seconds instead of waiting on every source."""
        sources = list(ARCHIVE_SOURCES.items())
        loaded: dict[str, list[Files]] = {}
        failed: list[str] = []
        if not sources:
            return loaded, failed

        first_id, first_iso = sources[0]
        try:
            loaded[first_id] = cls._fetch_source(first_id, first_iso)
        except Exception as exc:
            log_event(f"archive.org did not answer ({exc}) - using the saved game list.")
            return loaded, [ident for ident, _ in sources]

        with ThreadPoolExecutor(max_workers=INDEX_FETCH_WORKERS) as pool:
            futures = {pool.submit(cls._fetch_source, ident, iso): ident for ident, iso in sources[1:]}
            for future in as_completed(futures):
                ident = futures[future]
                try:
                    loaded[ident] = future.result()
                except Exception as exc:
                    failed.append(ident)
                    log_event(f"Could not load {ident} from archive.org: {exc}")
        return loaded, failed

    @classmethod
    def offline(cls) -> "Index":
        """An empty index for when nothing could be loaded; the app still opens."""
        try:
            creds = grab_archive_credentials()
        except Exception:
            creds = {}
        return cls(entries=[], cookies=creds.get("cookies", []), user_agent=SPOOF,
                   s3=creds.get("s3", {}), status="offline",
                   failed_sources=list(ARCHIVE_SOURCES))

    @classmethod
    def load_combined(cls, use_cache: bool = True) -> Index:
        """Never raises because archive.org is down: the app always gets an Index.

        An out-of-date saved copy is returned straight away (status "stale") so
        the window opens instantly; call refresh() on it in the background."""
        creds = grab_archive_credentials()

        def build(entries, status, failed=(), fetched_at=None):
            return cls(entries=entries, cookies=creds.get("cookies", []), user_agent=SPOOF,
                       s3=creds.get("s3", {}), status=status,
                       failed_sources=list(failed), fetched_at=fetched_at)

        if use_cache:
            fresh = cls._read_cache_blob()
            if fresh is not None:
                return build(fresh[0], "fresh", fetched_at=fresh[1])
            stale = cls._read_cache_blob(ignore_age=True)
            if stale is not None:
                return build(stale[0], "stale", fetched_at=stale[1])

        index = build([], "offline", failed=list(ARCHIVE_SOURCES))
        if not use_cache:
            stale = cls._read_cache_blob(ignore_age=True)
            if stale is not None:
                index.entries, index.fetched_at, index.status = stale[0], stale[1], "stale"
        index.refresh()
        return index

    def refresh(self) -> bool:
        """Re-download the lists from archive.org. Sources that fail keep the
        entries already loaded for them. True if anything new was loaded."""
        if not self._refresh_lock.acquire(blocking=False):
            return False                        # a refresh is already running
        try:
            loaded, failed = self._fetch_all()
            if not loaded:
                self.failed_sources = list(ARCHIVE_SOURCES)
                if not self.entries:
                    self.status = "offline"
                elif self.status == "fresh":
                    self.status = "stale"
                return False

            previous: dict[str, list[Files]] = {}
            for entry in self.entries:
                previous.setdefault(entry.archive_identifier, []).append(entry)
            merged: list[Files] = []
            for ident in ARCHIVE_SOURCES:
                merged.extend(loaded[ident] if ident in loaded else previous.get(ident, []))

            self.entries = merged                # one assignment: searches never see half a list
            self.failed_sources = sorted(failed)
            self.status = "partial" if failed else "fresh"
            self.fetched_at = time.time()
            self._write_cache(merged, partial=bool(failed))
            return True
        except Exception as exc:
            log_event(f"Refreshing the game list failed: {exc}")
            return False
        finally:
            self._refresh_lock.release()

    def search(self, term: str) -> list[Files]:
        tokens = search_tokens(term)
        if not tokens:
            return []
        return [
            entry for entry in self.entries
            if not is_excluded_file(entry.name)
            and search_text_matches(tokens, entry.name)
        ]
        
ARCHIVE_METADATA_URL = "https://archive.org/metadata"

_NODE_CACHE: dict = {}
_NODE_CACHE_LOCK = threading.Lock()


def _collect_nodes(meta: dict) -> list:
    nodes = []
    seen = set()

    def add(host, directory):
        if not host or not directory:
            return
        host = str(host).strip().strip("/")
        directory = "/" + str(directory).strip().strip("/")
        if host and host not in seen:
            seen.add(host)
            nodes.append((host, directory))

    primary_dir = meta.get("dir") or ""
    for key in ("server", "d1", "d2"):
        add(meta.get(key), primary_dir)
    for host in (meta.get("workable_servers") or []):
        add(host, primary_dir)
    for loc in (meta.get("workable") or []):
        if isinstance(loc, dict):
            add(loc.get("server"), loc.get("dir") or primary_dir)
    alt = meta.get("alternate_locations") or {}
    if isinstance(alt, dict):
        for bucket in ("servers", "workable", "locations"):
            for loc in (alt.get(bucket) or []):
                if isinstance(loc, dict):
                    add(loc.get("server"), loc.get("dir") or primary_dir)
    return nodes


def _mark_sparse(handle) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        FSCTL_SET_SPARSE = 0x000900C4
        raw = msvcrt.get_osfhandle(handle.fileno())
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(raw), wintypes.DWORD(FSCTL_SET_SPARSE),
            None, 0, None, 0, ctypes.byref(returned), None,
        )
        return bool(ok)
    except Exception:
        return False


PARTIAL_DIR_NAME = "incomplete"   # downloads/incomplete/: files that are still downloading


def partial_download_path(name: str, destination_dir: Path | None = None) -> Path:
    """Where a file is written while it downloads. It only moves to its real name once complete."""
    return (destination_dir or DOWNLOAD_DIR) / PARTIAL_DIR_NAME / (Path(name).name + ".part")


def _remove_partial_dir_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()          # only succeeds when nothing is left in it
    except OSError:
        pass


PROCESSING_MARKER_NAME = ".bman_processing.json"

# Xbox 360 indie games (xbox_360_indie_games): each zip holds just the game's package
# file. It is put in its title ID / content type folders as soon as it is extracted:
#   extracted\<game>\584E07D2\00000002\<package>
# Uploads then put the title ID folder in Content\0000000000000000\ on the console.
INDIE_SOURCE = "xbox_360_indie_games"
INDIE_TITLE_ID = "584E07D2"              # every XNA indie game; used when the zip name has none
INDIE_CONTENT_TYPE = "00000002"
_ZIP_TITLE_ID_RE = re.compile(r"_([0-9A-Fa-f]{8})_v\d+(?:\.\d+)*(?:_[0-9A-Fa-f]{40})?\.(?:zip|rar|7z)$",
                              re.IGNORECASE)


def is_indie_entry(entry) -> bool:
    return getattr(entry, "archive_identifier", None) == INDIE_SOURCE


def indie_title_id(archive_name: str) -> str:
    """'Tapper_584E07D2_v0.0.0.0.zip' -> '584E07D2' (the title ID in the file name)."""
    match = _ZIP_TITLE_ID_RE.search(archive_name or "")
    return match.group(1).upper() if match else INDIE_TITLE_ID


def arrange_indie_content(game_extract_dir: Path, archive_name: str) -> bool:
    """Move an extracted indie game into <title ID>/00000002/ inside its folder.

    Left alone if it already has a title ID folder. Returns False (after logging
    why) only if the files could not be moved."""
    try:
        _root, existing = find_content_upload_roots(game_extract_dir, max_depth=CONTENT_SCAN_DEPTH_HINTED)
        if existing:
            return True                  # the zip already had the folder structure
        items = [p for p in game_extract_dir.iterdir() if p.name != PROCESSING_MARKER_NAME]
    except OSError as exc:
        log_event(f"Indie layout for {archive_name}: could not read {game_extract_dir}: {exc}")
        return False
    if not items:
        return True

    title_id = indie_title_id(archive_name)
    target = game_extract_dir / title_id / INDIE_CONTENT_TYPE
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in items:
            shutil.move(str(item), str(target / item.name))
    except OSError as exc:
        log_event(f"Indie layout for {archive_name}: could not move the files into "
                  f"{title_id}\\{INDIE_CONTENT_TYPE}: {exc}")
        return False
    log_event(f"Indie game: placed in {title_id}\\{INDIE_CONTENT_TYPE}\\")
    return True


def _mark_processing(game_extract_dir: Path, stage: str, cleanup: list[Path]) -> None:
    """Note what to delete if the app dies during this extraction / conversion."""
    try:
        (game_extract_dir / PROCESSING_MARKER_NAME).write_text(
            json.dumps({"stage": stage, "cleanup": [str(p) for p in cleanup]}), encoding="utf-8")
    except OSError as exc:
        log_event(f"Could not write the processing marker in {game_extract_dir.name}: {exc}")


def _clear_processing(game_extract_dir: Path) -> None:
    try:
        (game_extract_dir / PROCESSING_MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        pass


def _undo_processing(game_extract_dir: Path, reason: str) -> None:
    """Delete the half-made output named in the marker, then the marker itself."""
    marker = game_extract_dir / PROCESSING_MARKER_NAME
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    root = EXTRACT_DIR.resolve()
    for raw in info.get("cleanup", []):
        target = Path(raw)
        try:
            resolved = target.resolve()
            if resolved != root and root not in resolved.parents:
                continue                    # never touch anything outside extracted/
            if resolved.is_dir():
                shutil.rmtree(resolved)
            elif resolved.exists():
                resolved.unlink()
            else:
                continue                    # already gone
            log_event(f"Removed {reason} {info.get('stage', 'processing')} output: {target}")
        except OSError as exc:
            log_event(f"Could not remove {target}: {exc}")
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    _remove_partial_dir_if_empty(game_extract_dir)


def clean_interrupted_processing() -> int:
    """Undo extractions / conversions that were cut off when the app last closed or crashed."""
    count = 0
    try:
        markers = list(EXTRACT_DIR.glob(f"*/{PROCESSING_MARKER_NAME}"))
        if (EXTRACT_DIR / PROCESSING_MARKER_NAME).exists():
            markers.append(EXTRACT_DIR / PROCESSING_MARKER_NAME)
    except OSError:
        return 0
    for marker in markers:
        _undo_processing(marker.parent, "unfinished")
        count += 1
    return count


def clean_incomplete_downloads() -> int:
    """Delete downloads that were cut off when the app closed or crashed, and their folder.

    Also deletes stray .part files anywhere under downloads/ (older versions wrote
    them next to the finished file), and undoes extractions / conversions that were
    cut off part-way, so none of them show up as finished.

    Call once at start-up, before anything starts downloading.
    """
    folder = DOWNLOAD_DIR / PARTIAL_DIR_NAME
    removed = clean_interrupted_processing()

    try:
        stray = [p for p in DOWNLOAD_DIR.rglob("*.part")
                 if p.is_file() and PARTIAL_DIR_NAME not in p.relative_to(DOWNLOAD_DIR).parts]
    except OSError:
        stray = []
    for path in stray:
        try:
            _retry_os(path.unlink, tries=8)
            removed += 1
            log_event(f"Deleted an unfinished download from last time: {path.name}")
        except OSError as exc:
            log_event(f"Could not delete the unfinished download {path.name}: {exc}")

    try:
        leftovers = list(folder.iterdir())
    except OSError:
        return removed
    for path in leftovers:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                _retry_os(path.unlink, tries=8)
            removed += 1
            log_event(f"Deleted an unfinished download from last time: {path.name}")
        except OSError as exc:
            log_event(f"Could not delete the unfinished download {path.name}: {exc}")
    _remove_partial_dir_if_empty(folder)
    return removed


class Downloader:
    chunk_size = 4 * 1024 * 1024

    max_segments = DEFAULT_MAX_SEGMENTS
    min_bytes_per_segment = DEFAULT_MIN_SEGMENT_MB * 1024 * 1024
    segment_min_file_size = DEFAULT_SEGMENT_MIN_FILE_MB * 1024 * 1024
    max_retries = DEFAULT_MAX_RETRIES
    retry_backoff_base = DEFAULT_RETRY_BACKOFF

    def __init__(self, index: Index):
        self.index = index
        apply_download_settings(self)
        self._session_pools: dict = {}
        self._pools_lock = threading.Lock()

    def _pool_for(self, host: str) -> "queue.Queue":
        with self._pools_lock:
            pool = self._session_pools.get(host)
            if pool is None:
                pool = queue.Queue()
                self._session_pools[host] = pool
            return pool

    def _create_session(self, host: str = ""):
        try:
            return self._pool_for(host).get_nowait()
        except queue.Empty:
            pass
        archive_session = curl_cffi_requests.Session(impersonate="chrome124")
        archive_session.headers.update({"User-Agent": self.index.user_agent, "Referer": ARCHIVE_BASE_URL})
        for cookie in self.index.cookies:
            archive_session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))
        try:
            archive_session._ia_pool_host = host
        except Exception:
            pass
        return archive_session

    def _release_session(self, session, healthy: bool = True, host: str | None = None):
        if host is None:
            host = getattr(session, "_ia_pool_host", "")
        pool = self._pool_for(host)
        if healthy and pool.qsize() < self.max_segments + 4:
            pool.put(session)
        else:
            try:
                session.close()
            except Exception:
                pass

    def _auth_header(self) -> dict:
        s3 = getattr(self.index, "s3", None) or {}
        if s3.get("access") and s3.get("secret"):
            return {"Authorization": f"LOW {s3['access']}:{s3['secret']}"}
        return {}

    def _authed_get(self, session, url, headers=None, max_redirects: int = 10, **kwargs):
        kwargs.pop("allow_redirects", None)
        base_headers = dict(headers or {})
        current = url
        response = None

        for _ in range(max_redirects):
            hop_headers = dict(base_headers)
            if is_archive_host(current):
                hop_headers.update(self._auth_header())
            response = session.get(current, headers=hop_headers, allow_redirects=False, **kwargs)

            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            if not location:
                return response

            try:
                response.close()
            except Exception:
                pass
            current = urljoin(current, location)

        log_event(f"  too many redirects for {url}")
        return response

    def _item_nodes(self, identifier: str) -> list:
        with _NODE_CACHE_LOCK:
            cached = _NODE_CACHE.get(identifier)
        if cached is not None:
            return cached

        nodes = []
        session = self._create_session()
        ok = False
        try:
            resp = self._authed_get(
                session, f"{ARCHIVE_METADATA_URL}/{quote(identifier)}", timeout=30
            )
            if resp.status_code == 200:
                nodes = _collect_nodes(resp.json())
                ok = True
            else:
                log_event(f"  metadata: HTTP {resp.status_code} for {identifier}")
            try:
                resp.close()
            except Exception:
                pass
        except Exception as exc:
            log_event(f"  metadata lookup failed for {identifier} - {exc}")
        finally:
            self._release_session(session, healthy=ok)

        with _NODE_CACHE_LOCK:
            _NODE_CACHE[identifier] = nodes
        if nodes:
            log_event(f"  nodes for {identifier}: {', '.join(h for h, _ in nodes)}")
        return nodes

    def _candidate_urls(self, entry: Files) -> list:
        identifier = entry.archive_identifier or list(ARCHIVE_SOURCES.keys())[0]
        name = quote(entry.name)
        urls = [f"https://{host}{directory}/{name}" for host, directory in self._item_nodes(identifier)]
        urls.append(f"{ARCHIVE_BASE_URL}/download/{quote(identifier)}/{name}")
        urls.append(f"{ARCHIVE_S3_BASE_URL}/{quote(identifier)}/{name}")
        return urls

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    def _probe(self, candidates: list, attempts: int = 3):
        last_err = None
        for attempt in range(attempts):
            for candidate in candidates:
                session = self._create_session()
                ok = False
                try:
                    resp = self._authed_get(
                        session,
                        candidate,
                        headers={"Range": "bytes=0-0"},
                        stream=True,
                        timeout=30,
                    )
                    if resp.status_code == 206:
                        content_range = resp.headers.get("Content-Range", "")
                        total = 0
                        if "/" in content_range:
                            try:
                                total = int(content_range.rsplit("/", 1)[-1])
                            except ValueError:
                                total = 0
                        resp.close()
                        ok = True
                        return candidate, total, True

                    if resp.status_code == 200:
                        total = int(resp.headers.get("Content-Length", 0))
                        resp.close()
                        ok = True
                        return candidate, total, False

                    log_event(f"  probe: HTTP {resp.status_code} from {candidate}")
                    if resp.status_code in (401, 403):
                        log_event("  -> archive.org refused this as unauthorised. "
                                  + ("Check the API keys in Settings are correct."
                                     if self._auth_header()
                                     else "No API keys are saved -- add them in Settings; "
                                          "this item needs an account."))
                    if not self._is_retryable_status(resp.status_code):
                        last_err = DownloadError(f"HTTP {resp.status_code} for {candidate}")
                    resp.close()
                except Exception as exc:
                    last_err = exc
                    log_event(f"  probe: {candidate} errored - {exc}")
                finally:
                    self._release_session(session, healthy=ok)

            if attempt < attempts - 1:
                time.sleep(self.retry_backoff_base * (attempt + 1))

        return None, 0, False

    def _live_nodes(self, urls: list, timeout: int = 20) -> list:
        if len(urls) <= 1:
            return list(urls)

        good = {}
        lock = threading.Lock()

        def check(pair):
            position, url = pair
            host = urlparse(url).netloc
            session = self._create_session(host)
            ok = False
            try:
                resp = self._authed_get(
                    session, url, headers={"Range": "bytes=0-0"}, stream=True, timeout=timeout
                )
                if resp.status_code == 206:
                    ok = True
                    with lock:
                        good[position] = url
                resp.close()
            except Exception:
                pass
            finally:
                self._release_session(session, healthy=ok)

        with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as pool:
            list(pool.map(check, list(enumerate(urls))))

        return [good[k] for k in sorted(good)]

    def _fetch_parallel(self, urls, destination, total, progress_callback, cancel_event):
        urls = list(urls)
        if not urls:
            raise DownloadError("No usable data node for this file.")

        prealloc_started = time.time()
        with open(destination, "wb") as handle:
            sparse = _mark_sparse(handle)
            handle.truncate(total)
        prealloc_elapsed = time.time() - prealloc_started
        if prealloc_elapsed > 1.0 or not sparse:
            log_event(f"  preallocated {format_size(total)} in {prealloc_elapsed:.1f}s "
                      f"(sparse: {'yes' if sparse else 'NO - disk had to zero-fill'})")

        chunk_bytes = max(int(self.min_bytes_per_segment), 1024 * 1024)
        planned = max(1, min(int(self.max_segments), max(1, total // chunk_bytes)))
        if total // chunk_bytes < planned * 6:
            chunk_bytes = max(2 * 1024 * 1024, total // max(planned * 6, 1))

        work: "queue.Queue" = queue.Queue()
        pos = 0
        while pos < total:
            end = min(pos + chunk_bytes, total) - 1
            work.put((pos, end))
            pos = end + 1

        n_chunks = work.qsize()
        workers = max(1, min(int(self.max_segments), n_chunks))
        log_event(f"  MODE: parallel - {workers} connections over {len(urls)} node(s), "
                  f"{n_chunks} x {format_size(chunk_bytes)} work chunks")
        for host in dict.fromkeys(urlparse(u).netloc for u in urls):
            log_event(f"    node: {host}")

        started = time.time()
        state = {"received": 0, "last_update": 0.0,
                 "mark_t": started, "mark_b": 0, "speed": 0.0}
        per_node: dict = {}
        lock = threading.Lock()

        def report(n_bytes, host=None):
            payload = None
            with lock:
                state["received"] += n_bytes
                if host:
                    per_node[host] = per_node.get(host, 0) + n_bytes
                now = time.time()
                if now - state["last_update"] < 0.15:
                    return
                window = now - state["mark_t"]
                if window >= 0.5:
                    inst = ((state["received"] - state["mark_b"]) / (1024 ** 2)) / window
                    state["speed"] = inst if state["speed"] <= 0 else (state["speed"] * 0.75 + inst * 0.25)
                    state["mark_t"] = now
                    state["mark_b"] = state["received"]
                state["last_update"] = now
                if progress_callback:
                    speed = state["speed"] or (state["received"] / (1024 ** 2)) / max(now - started, 1e-3)
                    payload = (state["received"], total, speed)

            if payload is not None:
                progress_callback(*payload)

        def worker(worker_id: int):
            url_idx = worker_id
            handle = open(destination, "r+b", buffering=0)
            try:
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise DownloadError("Download cancelled.")
                    try:
                        start, end = work.get_nowait()
                    except queue.Empty:
                        return

                    offset = start
                    attempt = 0
                    last_err = None
                    while offset <= end:
                        if attempt >= self.max_retries:
                            raise DownloadError(
                                f"range {start}-{end} failed after {attempt} attempts: {last_err}"
                            )
                        url = urls[url_idx % len(urls)]
                        host = urlparse(url).netloc
                        session = self._create_session(host)
                        ok = False
                        try:
                            resp = self._authed_get(
                                session, url,
                                headers={"Range": f"bytes={offset}-{end}"},
                                stream=True, timeout=60,
                            )
                            if resp.status_code != 206:
                                code = resp.status_code
                                resp.close()
                                raise _TransientDownloadError(f"HTTP {code} (expected 206)")

                            for chunk in resp.iter_content(chunk_size=self.chunk_size):
                                if cancel_event and cancel_event.is_set():
                                    resp.close()
                                    raise DownloadError("Download cancelled.")
                                if not chunk:
                                    continue
                                handle.seek(offset)
                                handle.write(chunk)
                                offset += len(chunk)
                                report(len(chunk), host)
                            resp.close()
                            ok = True
                        except DownloadError:
                            raise
                        except Exception as exc:
                            last_err = exc
                            attempt += 1
                            url_idx += 1
                            sleep_for = min(self.retry_backoff_base * (2 ** (attempt - 1)), 30.0)
                            log_event(f"  chunk {start}: retry {attempt}/{self.max_retries} "
                                      f"from byte {offset} in {sleep_for:.1f}s - {exc}")
                            time.sleep(sleep_for)
                        finally:
                            self._release_session(session, healthy=ok)
            finally:
                try:
                    handle.close()
                except Exception:
                    pass

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(worker, i) for i in range(workers)]
                for future in as_completed(futures):
                    future.result()
        except DownloadError:
            if destination.exists():
                try:
                    destination.unlink()
                except Exception:
                    pass
            raise

        elapsed = max(time.time() - started, 1e-3)
        speed = (state["received"] / (1024 ** 2)) / elapsed
        log_event(f"  DONE: {format_size(state['received'])} in {elapsed:.0f}s "
                  f"- average {speed:.2f} MB/s across {workers} connections / {len(urls)} node(s) "
                  f"({speed / max(workers, 1):.2f} MB/s each)")
        for host, moved in sorted(per_node.items(), key=lambda kv: -kv[1]):
            log_event(f"    {host}: {format_size(moved)} "
                      f"({(moved / (1024 ** 2)) / elapsed:.1f} MB/s, "
                      f"{100 * moved / max(state['received'], 1):.0f}% of the file)")
        if progress_callback:
            progress_callback(state["received"], total, speed, final=True)

    def _fetch_single_stream(self, session, response, destination, total, progress_callback, cancel_event):
        received = 0
        started = time.time()
        last_update = 0.0
        stream_ok = False

        try:
            with open(destination, "wb") as handle:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if cancel_event and cancel_event.is_set():
                        response.close()
                        if destination.exists():
                            try:
                                destination.unlink()
                            except Exception:
                                pass
                        raise DownloadError("Download cancelled.")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    received += len(chunk)
                    now = time.time()
                    if now - last_update >= 0.1:
                        if progress_callback:
                            elapsed = max(now - started, 1e-3)
                            speed = (received / (1024 ** 2)) / elapsed
                            progress_callback(received, total, speed)
                        last_update = now
            stream_ok = True
        except OSError as exc:
            raise DownloadError(f"Could not write {destination}: {exc}") from exc
        finally:
            self._release_session(session, healthy=stream_ok)

        elapsed = max(time.time() - started, 1e-3)
        speed = (received / (1024 ** 2)) / elapsed
        log_event(f"  DONE: {format_size(received)} in {elapsed:.0f}s - average {speed:.2f} MB/s (single connection)")
        if progress_callback:
            progress_callback(received, total, speed, final=True)

    def fetch(self, entry: Files, destination_dir: Path, progress_callback=None, cancel_event=None) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / Path(entry.name).name
        base_name_stem = Path(entry.name).stem

        if destination.exists():
            return destination

        game_extract_dir = EXTRACT_DIR / base_name_stem
        if game_extract_dir.exists():
            god_output_dir = game_extract_dir / "GOD_Output"
            if god_output_dir.exists() and any(god_output_dir.iterdir()):
                return destination
            iso_files = list(game_extract_dir.rglob("*.iso"))
            if iso_files:
                return destination

        candidates = self._candidate_urls(entry)
        log_event(f"=== {entry.name} ({format_size(entry.size)}) ===")

        resolved_url, total, supports_ranges = self._probe(candidates)

        if resolved_url is None:
            log_event("  PROBE FAILED - no data node, download URL or S3 fallback responded.")
            if not self._auth_header():
                raise DownloadError(
                    f"Could not download {entry.name}. No archive.org API keys are saved - "
                    "add them under Settings > Archive.org API Keys."
                )
            raise DownloadError(f"Request failed for {entry.name}: could not reach archive or fallback URL")

        total = total or entry.size or 0

        on_node = resolved_url not in (candidates[-1], candidates[-2])
        log_event(f"  endpoint: {urlparse(resolved_url).netloc}"
                  f"{'' if on_node else ' (REDIRECTOR/S3 FALLBACK - node lookup failed)'}")
        log_event(f"  range support: {'yes (HTTP 206)' if supports_ranges else 'NO (HTTP 200)'}")
        log_event(f"  size: {format_size(total)}")

        # Written as downloads/incomplete/<name>.part and only given its real name
        # once every byte is in. If the app closes or crashes part-way, the file
        # never shows up as downloaded, and the next start deletes it.
        part = partial_download_path(destination.name, destination_dir)
        part.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._fetch_to(entry, part, destination, candidates, resolved_url, total,
                                  supports_ranges, progress_callback, cancel_event)
        finally:
            # The folder only exists while something is downloading.
            _remove_partial_dir_if_empty(part.parent)

    def _fetch_to(self, entry, part, destination, candidates, resolved_url, total,
                  supports_ranges, progress_callback, cancel_event) -> Path:
        if supports_ranges and total >= self.segment_min_file_size:
            node_urls = [u for u in candidates[:-2]] or [resolved_url]
            check_started = time.time()
            live = self._live_nodes(node_urls) or [resolved_url]
            check_elapsed = time.time() - check_started
            if check_elapsed > 1.0:
                log_event(f"  node check took {check_elapsed:.1f}s "
                          f"({len(live)}/{len(node_urls)} nodes answered)")
            self._fetch_parallel(live, part, total, progress_callback, cancel_event)
            _retry_os(lambda: os.replace(part, destination))
            return destination

        if not supports_ranges:
            reason = "server ignored the Range header"
        else:
            reason = f"file is under the {self.segment_min_file_size // (1024 ** 2)} MB split threshold"
        log_event(f"  MODE: single stream ({reason}) - expect one connection's worth of speed")

        last_err = None
        for attempt in range(self.max_retries):
            if cancel_event and cancel_event.is_set():
                raise DownloadError("Download cancelled.")

            archive_session = self._create_session()
            try:
                response = self._authed_get(archive_session, resolved_url, stream=True, timeout=60)
            except Exception as exc:
                self._release_session(archive_session, healthy=False)
                last_err = exc
                log_event(f"  retry {attempt + 1}/{self.max_retries} - {exc}")
                time.sleep(self.retry_backoff_base * (2 ** attempt))
                continue

            if response.status_code != 200:
                retryable = self._is_retryable_status(response.status_code)
                self._release_session(archive_session, healthy=False)
                last_err = DownloadError(f"HTTP {response.status_code} for {entry.name}")
                if not retryable:
                    log_event(f"  HTTP {response.status_code} - not retryable, giving up")
                    raise last_err
                log_event(f"  retry {attempt + 1}/{self.max_retries} - HTTP {response.status_code}")
                time.sleep(self.retry_backoff_base * (2 ** attempt))
                continue

            try:
                self._fetch_single_stream(archive_session, response, part, total, progress_callback, cancel_event)
                _retry_os(lambda: os.replace(part, destination))
                return destination
            except DownloadError:
                part.unlink(missing_ok=True)
                raise
            except Exception as exc:
                last_err = exc
                time.sleep(self.retry_backoff_base * (2 ** attempt))

        part.unlink(missing_ok=True)
        raise DownloadError(f"Request failed for {entry.name} after {self.max_retries} attempts: {last_err}")

def handle_post_download(file_path: Path, entry: Files | None = None, conversion_mode: str = "3", progress_callback=None) -> bool:
    """Extract / convert a finished download. Returns False (after logging why) if any step fails.

    Never raises: any unexpected error is logged and counts as a failure, so the
    queue can move on to the next game.
    """
    name = Path(file_path).name
    game_extract_dir = EXTRACT_DIR / Path(file_path).stem
    try:
        return _handle_post_download(Path(file_path), conversion_mode, progress_callback, entry)
    except Exception as exc:
        import traceback
        log_event(f"Post-processing {name} failed with an unexpected error: {exc}\n"
                  f"{traceback.format_exc().rstrip()}")
        _undo_processing(game_extract_dir, "failed")
        return False


class _ThreadOutputRouter:
    """Stand-in for sys.stdout / sys.stderr that sends what a worker thread prints
    to that thread's own line callback, and everything else to the real stream.

    xgd_to_god / xgd_extract report progress with print(). They now run inside
    the app instead of as separate .exe processes, and several games can be
    processed at once, so a plain redirect_stdout() would mix their output.
    """

    def __init__(self, original):
        self._original = original
        self._local = threading.local()

    def _sink(self):
        return getattr(self._local, "sink", None)

    def write(self, text):
        sink = self._sink()
        if sink is None:
            if self._original is not None:
                return self._original.write(text)
            return len(text)
        buffer = self._local.buffer + text
        *lines, self._local.buffer = buffer.split("\n")
        for line in lines:
            sink(line)
        return len(text)

    def flush(self):
        if self._sink() is None and self._original is not None:
            self._original.flush()

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._original, name)

    def start(self, sink) -> None:
        self._local.sink = sink
        self._local.buffer = ""

    def stop(self) -> None:
        sink = self._sink()
        leftover = getattr(self._local, "buffer", "")
        self._local.sink = None
        self._local.buffer = ""
        if sink is not None and leftover:
            sink(leftover)


_OUTPUT_ROUTER_LOCK = threading.Lock()


def _output_routers() -> tuple[_ThreadOutputRouter, _ThreadOutputRouter]:
    with _OUTPUT_ROUTER_LOCK:
        if not isinstance(sys.stdout, _ThreadOutputRouter):
            sys.stdout = _ThreadOutputRouter(sys.stdout)
        if not isinstance(sys.stderr, _ThreadOutputRouter):
            sys.stderr = _ThreadOutputRouter(sys.stderr)
        return sys.stdout, sys.stderr


def _run_tool(func, progress_callback, tool_name: str, game: str) -> bool:
    """Run xgd_to_god / xgd_extract in this thread, streaming what it prints to the
    status line. Never raises: a failure is logged with the tool's last lines of output."""
    tail: list[str] = []

    def on_line(line: str) -> None:
        line_clean = line.strip()
        if not line_clean:
            return
        tail.append(line_clean)
        del tail[:-15]
        if progress_callback:
            try:
                progress_callback(line_clean)
            except Exception:
                pass

    out, err = _output_routers()
    out.start(on_line)
    err.start(on_line)
    try:
        func()
        return True
    except Exception as exc:
        kind = "" if isinstance(exc, (xgd_to_god.ConversionError, OSError, ValueError)) \
            else f"{type(exc).__name__}: "
        log_event(f"{tool_name} failed for {game}: {kind}{exc}\n    Last output:\n    "
                  + ("\n    ".join(tail) if tail else "(none)"))
        return False
    finally:
        out.stop()
        err.stop()


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _god_title(iso_path: Path, game_root: Path | None) -> str:
    """Name written into the GOD header (shown on the dashboard).

    The game's folder in extracted/ is named after the download, which is usually
    the proper game name; the ISO inside an archive is often abbreviated. Region /
    version tags in brackets are dropped. Falls back to the ISO name.
    """
    candidates = []
    try:
        if game_root is not None and game_root.resolve() != EXTRACT_DIR.resolve():
            candidates.append(game_root.name)
    except OSError:
        pass
    candidates.append(iso_path.stem)
    for raw in candidates:
        title = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", raw)
        title = re.sub(r"[_\s]+", " ", title).strip(" -.")
        if title:
            return title
    return ""


def run_god(iso_path: Path, output_god_dir: Path, progress_callback, game: str,
                game_root: Path | None = None) -> bool:
    """Convert an ISO to GOD with xgd_to_god (in-process). Logs its output if it fails."""
    if progress_callback:
        progress_callback("Running GOD conversion...")
    argv = [str(iso_path), str(output_god_dir), "--overwrite"]
    title = _god_title(iso_path, game_root)
    if title:
        argv += ["--title", title]

    def work():
        args = xgd_to_god.build_parser().parse_args(argv)
        if not args.source.is_file():
            raise xgd_to_god.ConversionError(f"source ISO does not exist: {args.source}")
        xgd_to_god.convert(args)

    return _run_tool(work, progress_callback, "GOD conversion", game)


def run_extract_xiso(iso_path: Path, marker_dir: Path, progress_callback, game: str) -> bool:
    """Extract an ISO with xgd_extract into <ISO folder>/<ISO name>/ (the Extract ISO option)."""
    if progress_callback:
        progress_callback("Extracting XISO...")
    out_dir = iso_path.parent / iso_path.stem      # loose files go next to the ISO, named after it
    try:
        inside_extracted = EXTRACT_DIR.resolve() in out_dir.resolve().parents
    except OSError:
        inside_extracted = False
    if out_dir.exists() and inside_extracted:
        shutil.rmtree(out_dir, ignore_errors=True)  # never mix with a half-finished earlier attempt
    _mark_processing(marker_dir, "XISO extraction", [out_dir])

    def work():
        if not iso_path.is_file():
            raise xgd_to_god.ConversionError(f"source ISO does not exist: {iso_path}")
        xgd_extract.extract_image(iso_path, out_dir, overwrite=True)

    ok = _run_tool(work, progress_callback, "XISO extraction", game)
    if ok:
        _clear_processing(marker_dir)
    else:
        _undo_processing(marker_dir, "failed")
    return ok


def convert_to_god_or_xiso(iso_path: Path, game_root: Path, progress_callback, game: str) -> bool:
    """GOD conversion; if it fails, delete its partial output and extract as XISO instead."""
    output_god_dir = game_root / "GOD_Output"
    output_god_dir.mkdir(parents=True, exist_ok=True)
    _mark_processing(game_root, "GOD conversion", [output_god_dir])
    if run_god(iso_path, output_god_dir, progress_callback, game, game_root=game_root):
        _clear_processing(game_root)
        return True

    _undo_processing(game_root, "failed")          # deletes the partial GOD_Output
    if output_god_dir.exists():
        shutil.rmtree(output_god_dir, ignore_errors=True)
    log_event(f"GOD conversion failed for {game} - extracting it as XISO instead.")
    if progress_callback:
        progress_callback("GOD conversion failed - extracting as XISO instead...")
    if run_extract_xiso(iso_path, game_root, progress_callback, game):
        log_event(f"{game} was extracted as XISO instead of GOD (GOD conversion failed).")
        return True
    log_event(f"XISO extraction also failed for {game} - nothing was converted.")
    return False


def _handle_post_download(file_path: Path, conversion_mode: str, progress_callback,
                          entry: "Files | None" = None) -> bool:
    name = file_path.name
    if file_path.is_dir():
        log_event(f"Post-processing {name} failed: the download is a folder, not a file.")
        return False

    game_extract_dir = EXTRACT_DIR / file_path.stem
    game_extract_dir.mkdir(parents=True, exist_ok=True)

    # A marker left from an earlier failed attempt: start this one clean.
    if (game_extract_dir / PROCESSING_MARKER_NAME).exists():
        _undo_processing(game_extract_dir, "unfinished")
        game_extract_dir.mkdir(parents=True, exist_ok=True)

    god_output_dir = game_extract_dir / "GOD_Output"
    if conversion_mode == "2" and god_output_dir.exists() and any(god_output_dir.iterdir()):
        log_event(f"GOD output already exists for {name}, skipping extraction/conversion.")
        return True

    iso_path = None
    existing_iso_files = [p for p in game_extract_dir.rglob("*.iso") if "GOD_Output" not in p.parts]
    if existing_iso_files:
        iso_path = existing_iso_files[0]
        log_event(f"Found existing extracted ISO: {iso_path}, skipping archive extraction.")

    if not iso_path:
        if progress_callback:
            progress_callback("Extracting archive/files...")

        is_zip = False
        try:
            is_zip = zipfile.is_zipfile(file_path)
        except Exception:
            pass

        if file_path.suffix.lower() == ".zip" and not is_zip:
            log_event(f"Extraction failed for {name}: it is not a valid zip file "
                      f"(the download is probably damaged or incomplete - delete it and download again).")
            _remove_partial_dir_if_empty(game_extract_dir)
            return False

        is_archive = is_zip or file_path.suffix.lower() in RAR_7Z_SUFFIXES
        if is_archive:
            # If the app dies mid-extraction, the next start deletes the half-extracted
            # files instead of treating them as a finished game. Only the folder's own
            # contents from this run are at stake: it had no ISO in it to begin with.
            was_empty = not any(p.name != PROCESSING_MARKER_NAME for p in game_extract_dir.iterdir())
            if was_empty:
                _mark_processing(game_extract_dir, "extraction", [game_extract_dir])

        if is_zip:
            try:
                with zipfile.ZipFile(file_path, 'r') as zf:
                    zf.extractall(game_extract_dir)
            except Exception as e:
                log_event(f"Zip extraction failed for {name}: {e} "
                          f"(the file may be damaged/incomplete, or the disk may be full)")
                _undo_processing(game_extract_dir, "failed")
                return False
        elif file_path.suffix.lower() in RAR_7Z_SUFFIXES:
            try:
                bman_archive.extract_archive(file_path, game_extract_dir, progress_callback)
            except ArchiveExtractError as exc:
                log_event(f"Extraction failed for {name}: {exc}")
                _undo_processing(game_extract_dir, "failed")
                return False
            except Exception as exc:
                log_event(f"Extraction failed for {name}: {type(exc).__name__}: {exc}")
                _undo_processing(game_extract_dir, "failed")
                return False
        elif file_path.suffix.lower() == ".iso":
            iso_path = file_path

        if is_archive and is_indie_entry(entry):
            # Still inside the crash marker: a crash mid-move is cleaned up like a failed extraction.
            if not arrange_indie_content(game_extract_dir, file_path.name):
                _undo_processing(game_extract_dir, "failed")
                return False

        if is_archive:
            _clear_processing(game_extract_dir)        # extraction finished cleanly
            iso_files = [p for p in game_extract_dir.rglob("*.iso") if "GOD_Output" not in p.parts]
            if iso_files:
                iso_path = iso_files[0]

    if conversion_mode == "3":
        return True

    if not iso_path:
        iso_files = [p for p in game_extract_dir.rglob("*.iso") if "GOD_Output" not in p.parts]
        if iso_files:
            iso_path = iso_files[0]
        else:
            return True

    if conversion_mode == "1":
        return run_extract_xiso(iso_path, game_extract_dir, progress_callback, name)

    if conversion_mode == "2":
        return convert_to_god_or_xiso(iso_path, game_extract_dir, progress_callback, name)

    return True


def find_unextracted_archives() -> list[Path]:
    pending: list[Path] = []
    if not DOWNLOAD_DIR.exists():
        return pending
    for path in sorted(DOWNLOAD_DIR.iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".part":
            continue                    # an unfinished download, never an archive to extract
        is_archive = suffix == ".zip" or suffix in RAR_7Z_SUFFIXES
        if not is_archive:
            try:
                is_archive = zipfile.is_zipfile(path)
            except Exception:
                is_archive = False
        if not is_archive:
            continue
        target = EXTRACT_DIR / path.stem
        try:
            if target.exists() and any(target.iterdir()):
                continue
        except Exception:
            continue
        pending.append(path)
    return pending

def find_extracted_isos() -> list[Path]:
    if not EXTRACT_DIR.exists():
        return []
    found: list[Path] = []
    for iso in sorted(EXTRACT_DIR.rglob("*.iso")):
        try:
            if not iso.is_file():
                continue
        except Exception:
            continue
        if "GOD_Output" in iso.parts:
            continue
        found.append(iso)
    return found

def game_root_for_iso(iso_path: Path) -> Path:
    try:
        rel = iso_path.relative_to(EXTRACT_DIR)
    except ValueError:
        return iso_path.parent
    if len(rel.parts) > 1:
        return EXTRACT_DIR / rel.parts[0]
    return iso_path.parent

def predict_upload_folders(mode: str) -> list[tuple[str, str]]:
    """For Upload All: (game folder inside 'extracted', the folder name it will get when uploaded).

    Worked out before anything is extracted or converted, so long names can be asked
    about up front. Games that go up as GOD or title ID content make no named folder
    of their own and are left out.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    if EXTRACT_DIR.exists():
        try:
            game_dirs = sorted(p for p in EXTRACT_DIR.iterdir() if p.is_dir())
        except Exception:
            game_dirs = []
        for game_dir in game_dirs:
            name = ""
            try:
                if is_catalog_folder(game_dir):
                    seen.add(game_dir.name)
                    continue                    # themes / gamer pictures go loose under FFFE07D1
                kind, path, subfolder = analyze_upload_target(game_dir)
                if kind in ("missing", "empty"):
                    continue
                seen.add(game_dir.name)
                if kind == "god":
                    continue
                if mode == "2" and any(p.is_file() and "GOD_Output" not in p.parts
                                       for p in game_dir.rglob("*.iso")):
                    continue                    # about to be converted to GOD
                if kind == "needs_convert":
                    if mode == "1" and path is not None:
                        name = path.stem        # XISO extraction names the folder after the ISO
                elif kind in ("xiso", "files"):
                    _source, name = hdd_upload_folder(kind, path, subfolder)
            except Exception:
                continue
            if name:
                found.append((game_dir.name, name))

    if mode == "1":
        # Not unpacked yet, so go by the file name: the ISO inside is normally called the same.
        for archive in find_unextracted_archives():
            if archive.stem not in seen and catalog_entry_for_filename(archive.name) is None:
                found.append((archive.stem, archive.stem))
    return found


def bulk_convert_iso(iso_path: Path, conversion_mode: str, progress_callback=None) -> str:
    """Convert one ISO for the bulk tools. Returns "done", "skipped" or "failed"; never raises.

    GOD mode falls back to an XISO extraction if the ISO cannot be converted to GOD.
    """
    try:
        game_root = game_root_for_iso(iso_path)
        if conversion_mode == "2":
            output_god_dir = game_root / "GOD_Output"
            try:
                if output_god_dir.exists() and any(output_god_dir.iterdir()):
                    return "skipped"
            except Exception:
                pass
            ok = convert_to_god_or_xiso(iso_path, game_root, progress_callback, iso_path.name)
        elif conversion_mode == "1":
            already = iso_path.parent / iso_path.stem
            try:
                if already.is_dir() and any(already.iterdir()):
                    return "skipped"
            except Exception:
                pass
            ok = run_extract_xiso(iso_path, game_root, progress_callback, iso_path.name)
        else:
            return "skipped"
    except Exception as exc:
        import traceback
        log_event(f"Converting {iso_path.name} failed with an unexpected error: {exc}\n"
                  f"{traceback.format_exc().rstrip()}")
        _undo_processing(game_root_for_iso(iso_path), "failed")
        return "failed"
    return "done" if ok else "failed"


def reset_scrollable_frame(frame) -> None:
    def _apply():
        try:
            canvas = frame._parent_canvas
            frame.update_idletasks()
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)
            canvas.yview_moveto(0.0)
        except Exception:
            pass
    try:
        _apply()
        frame.after_idle(_apply)
    except Exception:
        pass



