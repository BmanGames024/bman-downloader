"""Bman Downloader - everything that is not the window: console sources, downloads,
extraction, collections, settings, theming and the self-updater.

The interface lives in bman_ui.py; start the app from bman_downloader.py."""
from __future__ import annotations

import base64
import colorsys
import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict, field, fields as dataclass_fields
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
import zipfile
import itertools
import shutil
import queue
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import bman_archive          # shared with the main app (lives next to BmanDownloader.py)
from bman_archive import RAR_7Z_SUFFIXES, ArchiveExtractError, archive_support


# Where the real .exe (or this .py file) lives.
# Nuitka doesn't set sys.frozen; it defines __compiled__ instead. In a Nuitka
# onefile build, __file__ and sys.executable point into the temporary unpack
# folder, while sys.argv[0] is the actual BmanRetroDownloader.exe the user ran.
IS_NUITKA = "__compiled__" in globals()
if IS_NUITKA:
    EXE_PATH = Path(sys.argv[0]).resolve()
elif getattr(sys, "frozen", False):          # PyInstaller
    EXE_PATH = Path(sys.executable).resolve()
else:                                        # plain python: BmanDownloader.py, one folder up
    EXE_PATH = Path(__file__).resolve().parent.parent / "BmanDownloader.py"
IS_FROZEN = IS_NUITKA or bool(getattr(sys, "frozen", False))

RUN_DIR = EXE_PATH.parent

CREDS_FILE = "archive_creds.json"

INDEX_CACHE_FILE = "index_cache.json"
INDEX_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60     # archive.org game list: refreshed once a week
INDEX_CACHE_PARTIAL_TTL_SECONDS = 15 * 60
INDEX_CACHE_VERSION = 2

SETTINGS_FILE = "app_settings.json"


APP_VERSION = "2.4"
# Runs inside Bman Downloader and is updated with it. Its own updater stays off:
# it would replace BmanDownloader.exe with the old standalone retro exe.
EMBEDDED_IN_MAIN_APP = True
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/BmanGames024/bman-retro-downloader/refs/heads/main/version.json"

MIN_UPDATE_BYTES = 5 * 1024 * 1024

DEFAULT_MAX_SEGMENTS = 32
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

# Shared with the main app: <exe folder>\core. Retro keeps its own settings, caches,
# collections and logs in core\retro\ (same file names as the main app's, different
# contents), and shares only the archive.org login (core\archive_creds.json).
CORE_DIR = RUN_DIR / "core"
MAIN_DIR = CORE_DIR / "retro"
# Its own game folders: the main app looks for Xbox ISOs in extracted\ and loose
# archives in downloads\, so retro games must not land in those.
DOWNLOAD_DIR = RUN_DIR / "retro_downloads"
EXTRACT_DIR = RUN_DIR / "retro_extracted"
try:
    MAIN_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# After a download finishes:
#   auto    - extract disc games (big archives emulators can't open), keep cartridge ROMs as downloaded
#   extract - always extract archives
#   keep    - never extract; leave the file exactly as downloaded
POST_MODE_AUTO = "auto"
POST_MODE_EXTRACT = "extract"
POST_MODE_KEEP = "keep"
POST_MODES = (
    (POST_MODE_AUTO, "Auto (extract disc games, keep cartridge ROMs zipped)"),
    (POST_MODE_EXTRACT, "Always extract"),
    (POST_MODE_KEEP, "Keep as downloaded"),
)
POST_MODE_LABELS = dict(POST_MODES)
POST_MODE_SETTING_KEY = "post_download_mode"
CONSOLE_SETTING_KEY = "library_console"
DELETE_AFTER_EXTRACT_KEY = "delete_archive_after_extract"


def download_dir_for(entry) -> Path:
    """downloads/<console>/ - each console's games kept apart."""
    console = getattr(entry, "console", "") or ""
    return DOWNLOAD_DIR / console if console else DOWNLOAD_DIR


def download_key(entry) -> str:
    """How a game's download is named on the Downloads tab: "<console>/<file name>"."""
    name = Path(getattr(entry, "name", "") or "").name
    console = getattr(entry, "console", "") or ""
    return f"{console}/{name}" if console else name


def download_key_for_path(path: Path) -> str:
    """The same key for a file already sitting in downloads/."""
    path = Path(path)
    console = console_for_download(path)
    return f"{console}/{path.name}" if console else path.name


def console_for_download(file_path: Path) -> str:
    """The console a downloaded file belongs to, from the folder it sits in."""
    try:
        parent = Path(file_path).parent
        if parent.parent.resolve() == DOWNLOAD_DIR.resolve() and parent.name in CONSOLE_NAMES:
            return parent.name
    except OSError:
        pass
    return ""


def extract_dir_for(file_path: Path) -> Path:
    """extracted/<console>/<game>/ for a file in downloads/<console>/."""
    file_path = Path(file_path)
    console = console_for_download(file_path)
    base = EXTRACT_DIR / console if console else EXTRACT_DIR
    return base / file_path.stem


def resolve_post_mode(mode: str | None, console: str | None) -> str:
    if mode in (POST_MODE_EXTRACT, POST_MODE_KEEP):
        return mode
    return POST_MODE_EXTRACT if console_is_disc(console) else POST_MODE_KEEP

EXCLUDED_EXTENSIONS = (".xml", ".sqlite", ".torrent", ".jpg", ".jpeg", ".png", ".gif",
                       ".txt", ".nfo", ".json", ".md5", ".sha1", ".sfv", ".dat", ".htm", ".html")

def is_excluded_file(name: str) -> bool:
    return name.lower().endswith(EXCLUDED_EXTENSIONS)

# --- Consoles and sources -----------------------------------------------------
# Every system in Vimm's Vault except the Xbox family. "disc" systems usually come
# as big archives that need extracting before an emulator can load them;
# cartridge ROMs are small and most emulators open the .zip directly.
# (key, display name, media)
CONSOLES = (
    ("Atari2600", "Atari 2600", "cart"),
    ("Atari5200", "Atari 5200", "cart"),
    ("NES", "Nintendo (NES)", "cart"),
    ("SMS", "Master System", "cart"),
    ("Atari7800", "Atari 7800", "cart"),
    ("TG16", "TurboGrafx-16", "cart"),
    ("Genesis", "Genesis", "cart"),
    ("TGCD", "TurboGrafx-CD", "disc"),
    ("SNES", "Super Nintendo", "cart"),
    ("CDi", "CD-i", "disc"),
    ("SegaCD", "Sega CD", "disc"),
    ("Jaguar", "Jaguar", "cart"),
    ("32X", "Sega 32X", "cart"),
    ("Saturn", "Saturn", "disc"),
    ("PS1", "PlayStation", "disc"),
    ("JaguarCD", "Jaguar CD", "disc"),
    ("N64", "Nintendo 64", "cart"),
    ("Dreamcast", "Dreamcast", "disc"),
    ("PS2", "PlayStation 2", "disc"),
    ("GameCube", "GameCube", "disc"),
    ("PS3", "PlayStation 3", "disc"),
    ("Wii", "Wii", "disc"),
    ("WiiWare", "WiiWare", "cart"),
    ("WiiU", "Wii U", "disc"),
    ("GB", "Game Boy", "cart"),
    ("Lynx", "Lynx", "cart"),
    ("GG", "Game Gear", "cart"),
    ("VB", "Virtual Boy", "cart"),
    ("GBC", "Game Boy Color", "cart"),
    ("GBA", "Game Boy Advance", "cart"),
    ("DS", "Nintendo DS", "cart"),
    ("PSP", "PlayStation Portable", "disc"),
    ("3DS", "Nintendo 3DS", "cart"),
    # Not in Vimm's Vault. Kept zipped: Vita3K installs games straight from the .zip.
    ("Vita", "PlayStation Vita", "cart"),
)

CONSOLE_KEYS = tuple(key for key, _name, _media in CONSOLES)
CONSOLE_NAMES = {key: name for key, name, _media in CONSOLES}
CONSOLE_MEDIA = {key: media for key, _name, media in CONSOLES}
ALL_CONSOLES_LABEL = "All consoles"

def console_display_name(key: str | None) -> str:
    return CONSOLE_NAMES.get(key or "", key or "Unknown")

def console_key_for_label(label: str | None) -> str | None:
    """The console key for a dropdown label, or None for 'All consoles'."""
    for key, name in CONSOLE_NAMES.items():
        if name == label:
            return key
    return None

def console_is_disc(key: str | None) -> bool:
    return CONSOLE_MEDIA.get(key or "") == "disc"

SOURCES_FILE = "sources.json"

_SOURCES_HELP = (
    "Built into the app at build time (PyInstaller --add-data \"sources.json;.\"). "
    "List the archive.org items to index for each console. An item is the part of its "
    "URL after /details/ - for https://archive.org/details/some-item it is \"some-item\". "
    "If one item holds several consoles in subfolders, use "
    "{\"item\": \"some-item\", \"folder\": \"Folder Name\"} to take only that folder."
)


@dataclass(frozen=True)
class Source:
    console: str
    identifier: str
    folder: str = ""

    def matches(self, file_name: str) -> bool:
        if not self.folder:
            return True
        prefix = self.folder.strip("/").lower() + "/"
        return file_name.lower().startswith(prefix)


def sources_path() -> Path:
    """The source list this build uses.

    Packaged exe: the copy built into it (users can't change it - new sources
    arrive with app updates). Run from source: sources.json next to the .py files,
    which is the one to edit before building.
    """
    return resource_path(SOURCES_FILE)


def sources_editable() -> bool:
    """Only when running from source; the exe's built-in list is read-only."""
    return not IS_FROZEN


def write_sources_template(path: Path | None = None) -> Path:
    """Write sources.json with every console listed and no items yet."""
    path = path or sources_path()
    payload = {"_help": _SOURCES_HELP,
               "consoles": {key: [] for key in CONSOLE_KEYS}}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _parse_source_item(item) -> tuple | None:
    """(identifier, folder) for one entry in sources.json, or None if it isn't usable."""
    if isinstance(item, str):
        identifier, folder = item.strip(), ""
    elif isinstance(item, dict):
        identifier = str(item.get("item") or "").strip()
        folder = str(item.get("folder") or "").strip()
    else:
        return None
    # Accept a pasted URL (/details/ or /download/) as well as a bare identifier.
    for marker in ("archive.org/details/", "archive.org/download/"):
        if marker in identifier:
            identifier = identifier.split(marker, 1)[1].split("/")[0].split("?")[0].split("#")[0]
            break
    return (identifier, folder) if identifier else None


def _read_consoles(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    consoles = raw.get("consoles") if isinstance(raw, dict) else None
    if not isinstance(consoles, dict):
        raise ValueError('no "consoles" section')
    return raw, consoles


def load_sources() -> list[Source]:
    """Read the built-in source list. Never raises."""
    path = sources_path()
    if not path.exists():
        if sources_editable():
            try:
                write_sources_template(path)
                log_event(f"Created {path.name} - add archive.org items to it, then build.")
            except OSError as exc:
                log_event(f"Could not create {path.name}: {exc}")
        else:
            log_event("This build has no built-in source list - it was built without "
                      "--add-data \"sources.json;.\".")
        return []
    try:
        _raw, consoles = _read_consoles(path)
    except Exception as exc:
        log_event(f"{path.name} could not be read ({exc}).")
        return []

    out: list[Source] = []
    seen: set = set()
    for key, items in consoles.items():
        if key not in CONSOLE_NAMES:
            log_event(f"{path.name}: unknown console \"{key}\" ignored "
                      f"(known: {', '.join(CONSOLE_KEYS)}).")
            continue
        if isinstance(items, (str, dict)):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            parsed = _parse_source_item(item)
            if parsed is None:
                continue
            source = Source(console=key, identifier=parsed[0], folder=parsed[1])
            if source in seen:
                continue
            seen.add(source)
            out.append(source)
    return out


def source_identifiers(sources: list[Source]) -> list[str]:
    """Each archive.org item once, in the order first listed."""
    return list(dict.fromkeys(s.identifier for s in sources))


def entry_console(entry) -> str:
    return getattr(entry, "console", "") or ""


_APOSTROPHES_RE = re.compile(r"['\u2018\u2019\u201b\u02bc`\u00b4]")
_NON_WORD_RE = re.compile(r"[^0-9a-z]+")


def normalize_search_text(text: str) -> str:
    """Lower-case, accents off, apostrophes dropped, other punctuation turned into spaces.

    "Luigi's Mansion" -> "luigis mansion", "Pokémon: Red" -> "pokemon red".
    """
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _APOSTROPHES_RE.sub("", text.replace("&", " and "))   # "Ratchet & Clank" = "ratchet and clank"
    return _NON_WORD_RE.sub(" ", text).strip()


def entry_title(entry) -> str:
    """The file's name without any folder it sits in inside its archive.org item."""
    return Path(getattr(entry, "name", "") or "").name


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
    # Files built into the exe sit in its retro\ folder (next to this module).
    try:
        base_path = Path(sys._MEIPASS) / "retro"
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

STEAMGRIDDB_API_KEY = "63c24bdc673777b1810d7d2324a0f61f"
STEAMGRIDDB_BASE_URL = "https://www.steamgriddb.com/api/v2"

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
LOG_DIR = MAIN_DIR / "logs"
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

    log_event(f"Bman Retro Downloader started - logging to {LOG_FILE_PATH}")
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
    console: str = ""

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
    creds_path = CORE_DIR / CREDS_FILE        # shared with the main app
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
        data = json.loads((MAIN_DIR / SETTINGS_FILE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def save_settings(data: dict) -> None:
    try:
        (MAIN_DIR / SETTINGS_FILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Could not save settings: {exc}")


def delete_after_extract_enabled() -> bool:
    return bool(load_settings().get(DELETE_AFTER_EXTRACT_KEY, False))


def saved_post_mode() -> str:
    mode = load_settings().get(POST_MODE_SETTING_KEY)
    return mode if mode in POST_MODE_LABELS else POST_MODE_AUTO


def save_post_mode(mode: str) -> None:
    if mode not in POST_MODE_LABELS:
        return
    data = load_settings()
    data[POST_MODE_SETTING_KEY] = mode
    save_settings(data)



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
    console: str = ""

    @classmethod
    def from_entry(cls, entry) -> "CollectionGame":
        return cls(
            name=entry.name,
            archive_identifier=getattr(entry, "archive_identifier", None),
            size=getattr(entry, "size", None),
            date=getattr(entry, "date", None),
            source_type=getattr(entry, "source_type", "archive"),
            console=getattr(entry, "console", "") or "",
        )

    def to_files(self) -> Files:
        return Files(
            id=self.name,
            name=self.name,
            size=self.size,
            date=self.date,
            source_type=self.source_type or "archive",
            archive_identifier=self.archive_identifier,
            console=self.console or "",
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
            "console": self.console,
        }

    def copy(self) -> "CollectionGame":
        return CollectionGame(self.name, self.archive_identifier, self.size, self.date,
                              self.source_type, self.console)

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
    return MAIN_DIR / COLLECTIONS_FILE


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
                f"This collection was made by a newer version of Bman Retro Downloader "
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
            full = entry.name.lower()
            base = Path(entry.name).name.lower()
            for key in ((entry.console, full), ("", full), (entry.console, base), ("", base)):
                lookup.setdefault(key, entry)
    except Exception:
        return 0

    matched = 0
    for game in collection.games:
        name = (game.name or "").lower()
        base = Path(game.name or "").name.lower()
        console = game.console or ""
        entry = (lookup.get((console, name)) or lookup.get(("", name))
                 or lookup.get((console, base)) or lookup.get(("", base)))
        if entry is None:
            continue
        matched += 1
        if game.name != entry.name and game.archive_identifier in (None, "", entry.archive_identifier):
            game.name = entry.name      # the file's real place inside its archive.org item
        if game.size is None:
            game.size = entry.size
        if not game.date:
            game.date = entry.date
        if not game.archive_identifier:
            game.archive_identifier = entry.archive_identifier
        if not game.console:
            game.console = entry.console
    return matched



COMMUNITY_REPO_OWNER = "BmanGames024"
COMMUNITY_REPO_NAME = "bman-community"
COMMUNITY_BRANCH = "main"

COMMUNITY_INDEX_PATH = "retro/index.json"
COMMUNITY_ITEM_DIR = "retro/collections"
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


def _community_api_read(path: str) -> tuple:
    session = None
    try:
        session = _community_session()
        url = f"{COMMUNITY_API_BASE}/{quote(path)}?ref={quote(COMMUNITY_BRANCH)}"
        resp = session.get(url, headers=_community_headers(community_token()),
                           timeout=COMMUNITY_TIMEOUT)
        if resp.status_code == 404:
            return None, None
        if resp.status_code in (401, 403):
            raise CommunityError(f"GitHub refused the request (HTTP {resp.status_code}).")
        if resp.status_code != 200:
            raise CommunityError(f"GitHub returned HTTP {resp.status_code}.")
        body = resp.json()
        encoded = body.get("content")
        if not encoded:
            raise CommunityError("That community file is too large for this app to read.")
        raw = base64.b64decode(encoded)
        return json.loads(raw.decode("utf-8")), body.get("sha")
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
        url = f"{COMMUNITY_RAW_BASE}/{quote(path)}?cb={int(time.time())}"
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
        url = f"{COMMUNITY_API_BASE}/{quote(path)}"
        resp = session.put(url, json=body, headers=_community_headers(token),
                           timeout=COMMUNITY_TIMEOUT)
        if resp.status_code in (200, 201):
            try:
                return (resp.json().get("content") or {}).get("sha") or ""
            except Exception:
                return ""
        if resp.status_code == 409 or (resp.status_code == 422 and sha):
            raise CommunityConflict("Somebody else shared at the same moment.")
        if resp.status_code in (401, 403):
            raise CommunityError(
                "GitHub rejected the write. The sharing token for this build has "
                "probably expired or been revoked - browsing still works, and an "
                "app update will fix sharing.")
        raise CommunityError(f"GitHub returned HTTP {resp.status_code} while sharing.")
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
        url = f"{COMMUNITY_API_BASE}/{quote(path)}"
        resp = session.request(
            "DELETE", url,
            json={"message": message, "branch": COMMUNITY_BRANCH, "sha": sha},
            headers=_community_headers(token), timeout=COMMUNITY_TIMEOUT,
        )
        if resp.status_code not in (200, 204):
            raise CommunityError(f"GitHub returned HTTP {resp.status_code} while removing the file.")
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
        return cls(
            id=entry_id,
            name=name[:COMMUNITY_MAX_NAME],
            description=str(data.get("description") or "")[:COMMUNITY_MAX_DESCRIPTION],
            games=max(games, 0),
            size=size,
            shared=str(data.get("shared") or ""),
            updated=str(data.get("updated") or ""),
            app_version=str(data.get("app_version") or ""),
        )


def _community_cache_path() -> Path:
    return MAIN_DIR / COMMUNITY_CACHE_FILE


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
    return MAIN_DIR / COMMUNITY_LISTS_DIR / f"{entry_id}.json"


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
        for path in (MAIN_DIR / COMMUNITY_LISTS_DIR).iterdir():
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
        except CommunityConflict:
            log_event(f"Community: {path} changed under us, retrying ({attempt + 1}/{attempts}).")
            time.sleep(0.5 + attempt * 0.7)
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
        except CommunityConflict:
            log_event(f"Community: index changed under us, retrying ({attempt + 1}/{attempts}).")
            time.sleep(0.7 + attempt * 0.8)
    raise CommunityError(
        "The community list is busy right now - a few people shared at once. "
        "Give it a moment and try again.")


def publish_community_collection(collection: GameCollection) -> CommunityEntry:
    if not collection.games:
        raise CommunityError("There's nothing in that collection to share.")
    if len(collection.games) > COMMUNITY_MAX_GAMES:
        raise CommunityError(
            f"That collection has {len(collection.games):,} games in it. The community "
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

    entry = CommunityEntry(
        id=entry_id,
        name=name,
        description=body["description"],
        games=len(collection.games),
        size=collection.total_size(),
        shared=now,
        updated=now,
        app_version=APP_VERSION,
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

    _community_rewrite_index(
        mutate, f"{'Update' if is_update else 'Add'} index entry: {name}")
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


def parse_version(text: str) -> tuple:
    parts = []
    for chunk in str(text).strip().lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)

def check_for_update(timeout: int = 10) -> dict | None:
    if EMBEDDED_IN_MAIN_APP:
        return None                 # updated together with Bman Downloader
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
    if not IS_FROZEN or EMBEDDED_IN_MAIN_APP:
        return                      # the main app cleans up after its own updates
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

class Index:
    def __init__(self, entries: list[Files], cookies: list[dict], user_agent: str,
                 s3: dict | None = None, sources: list | None = None,
                 failed: list | None = None):
        self.entries = entries
        self.cookies = cookies
        self.user_agent = user_agent
        self.s3 = s3 or {}
        self.sources = list(sources or [])
        self.failed = list(failed or [])     # archive.org items that could not be read
        self.load_error = ""                 # set when the list could not be built at all

    @classmethod
    def empty(cls, error: str = "") -> "Index":
        """A game list with nothing in it, so the window can still open and explain the problem."""
        creds = grab_archive_credentials()
        index = cls(entries=[], cookies=creds.get("cookies", []), user_agent=SPOOF,
                    s3=creds.get("s3", {}), sources=load_sources())
        index.load_error = error
        return index

    def consoles_present(self) -> list[str]:
        """Console keys that have at least one game, in the standard order."""
        present = {e.console for e in self.entries}
        return [key for key in CONSOLE_KEYS if key in present]

    @staticmethod
    def _sources_signature(sources: list) -> str:
        return json.dumps(sorted([s.console, s.identifier, s.folder] for s in sources),
                          separators=(",", ":"))

    @staticmethod
    def _cache_path() -> Path:
        return MAIN_DIR / INDEX_CACHE_FILE

    @classmethod
    def _read_cache(cls, sources: list) -> list[Files] | None:
        cache_path = cls._cache_path()
        if not cache_path.exists():
            return None
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if blob.get("version") != INDEX_CACHE_VERSION:
            return None
        if blob.get("sources") != cls._sources_signature(sources):
            return None

        fetched_at = blob.get("fetched_at", 0)
        try:
            age = time.time() - float(fetched_at)
        except (TypeError, ValueError):
            return None
        ttl = INDEX_CACHE_PARTIAL_TTL_SECONDS if blob.get("partial") else INDEX_CACHE_TTL_SECONDS
        if age < 0 or age > ttl:
            return None

        valid_keys = {f.name for f in dataclass_fields(Files)}
        entries: list[Files] = []
        try:
            for raw in blob.get("entries", []):
                entries.append(Files(**{k: v for k, v in raw.items() if k in valid_keys}))
        except Exception:
            return None

        return entries or None

    @classmethod
    def _write_cache(cls, entries: list[Files], sources: list, partial: bool = False) -> None:
        cache_path = cls._cache_path()
        tmp_path = cache_path.with_suffix(".tmp")
        try:
            payload = {
                "version": INDEX_CACHE_VERSION,
                "fetched_at": time.time(),
                "sources": cls._sources_signature(sources),
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
    def _source_for(file_name: str, item_sources: list):
        # A source limited to a folder beats one that takes the whole item.
        for source in item_sources:
            if source.folder and source.matches(file_name):
                return source
        for source in item_sources:
            if not source.folder:
                return source
        return None

    @classmethod
    def load_combined(cls, use_cache: bool = True) -> Index:
        """Build the game list from the archive.org items in sources.json.

        Never fails just because sources.json is empty: the window still opens and
        explains what to do. Raises IndexLoadError only if items are listed but none
        of them could be read.
        """
        user_agent = SPOOF
        sources = load_sources()
        creds = grab_archive_credentials()

        def build(entries, failed=()):
            return cls(entries=entries, cookies=creds.get("cookies", []), user_agent=user_agent,
                       s3=creds.get("s3", {}), sources=sources, failed=failed)

        if not sources:
            return build([])

        if use_cache:
            cached = cls._read_cache(sources)
            if cached is not None:
                return build(cached)

        by_item: dict = {}
        for source in sources:
            by_item.setdefault(source.identifier, []).append(source)

        all_entries: list[Files] = []
        failed: list[str] = []
        temp_session = curl_cffi_requests.Session(impersonate="chrome124")
        try:
            for identifier, item_sources in by_item.items():
                meta_url = f"{ARCHIVE_METADATA_URL}/{quote(identifier)}"
                try:
                    resp = temp_session.get(meta_url, timeout=30)
                    if resp.status_code != 200:
                        failed.append(identifier)
                        log_event(f"Index: HTTP {resp.status_code} for {identifier}")
                        continue
                    data = resp.json()
                    files = data.get("files") or []
                    if not files:
                        failed.append(identifier)
                        log_event(f"Index: {identifier} has no files (wrong item name, or it was taken down).")
                        continue
                    added = 0
                    for f in files:
                        fname = f.get("name")
                        if not fname or is_excluded_file(fname):
                            continue
                        if f.get("source") == "metadata":
                            continue        # archive.org's own bookkeeping files
                        source = cls._source_for(fname, item_sources)
                        if source is None:
                            continue
                        try:
                            size_val = int(f.get("size")) if f.get("size") else None
                        except (ValueError, TypeError):
                            size_val = None
                        all_entries.append(Files(
                            id=f"{identifier}/{fname}",
                            name=fname,
                            size=size_val,
                            date=f.get("mtime"),
                            source_type="archive",
                            archive_identifier=identifier,
                            console=source.console,
                        ))
                        added += 1
                    log_event(f"Index: {identifier} - {added:,} files "
                              f"({', '.join(sorted({console_display_name(s.console) for s in item_sources}))})")
                except Exception as exc:
                    failed.append(identifier)
                    log_event(f"Index: could not read {identifier} - {exc}")
        finally:
            temp_session.close()

        if not all_entries:
            raise IndexLoadError(
                "None of the game sources could be loaded.\n\n"
                "Check your internet connection and try again."
                + (f"\n\nFailed: {', '.join(failed)}" if failed else ""))

        all_entries.sort(key=lambda e: (CONSOLE_KEYS.index(e.console), entry_title(e).lower()))
        cls._write_cache(all_entries, sources, partial=bool(failed))
        return build(all_entries, failed)

    def _search_text(self, entry) -> tuple:
        """(spaced, squashed) forms of a title for matching, worked out once per game."""
        cache = self.__dict__.setdefault("_search_cache", {})
        key = entry.id
        found = cache.get(key)
        if found is None:
            spaced = normalize_search_text(Path(entry_title(entry)).stem)
            found = (spaced, spaced.replace(" ", ""))
            cache[key] = found
        return found

    def search(self, term: str, console: str | None = None) -> list[Files]:
        """Every word typed must appear in the title.

        Case, accents and punctuation don't matter: "luigis mansion" finds
        "Luigi's Mansion", "pokemon" finds "Pokémon", "spiderman" finds "Spider-Man".
        """
        tokens = normalize_search_text(term).split()
        if not tokens:
            return []
        results = []
        for entry in self.entries:
            if console is not None and entry.console != console:
                continue
            if is_excluded_file(entry.name):
                continue
            spaced, squashed = self._search_text(entry)
            if all(token in spaced or token in squashed for token in tokens):
                results.append(entry)
        return results

    def browse(self, console: str) -> list[Files]:
        """Every game for one console (what an empty search shows once a console is picked)."""
        return [entry for entry in self.entries if entry.console == console]


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
        markers = (list(EXTRACT_DIR.glob(f"*/{PROCESSING_MARKER_NAME}"))
                   + list(EXTRACT_DIR.glob(f"*/*/{PROCESSING_MARKER_NAME}")))
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
        identifier = entry.archive_identifier
        if not identifier:
            raise DownloadError(f"{entry.name} has no archive.org item - refresh the game list.")
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

        if destination.exists():
            return destination

        # Downloaded before, extracted, and the archive deleted afterwards: nothing to fetch.
        if is_extracted(extract_dir_for(destination)):
            log_event(f"{entry_title(entry)} is already extracted - not downloading it again.")
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

def is_extracted(game_extract_dir: Path) -> bool:
    """True if this game folder holds a finished extraction."""
    try:
        if not game_extract_dir.is_dir():
            return False
        if (game_extract_dir / PROCESSING_MARKER_NAME).exists():
            return False
        return any(p.name != PROCESSING_MARKER_NAME for p in game_extract_dir.iterdir())
    except OSError:
        return False


def is_archive_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix == ".zip" or suffix in RAR_7Z_SUFFIXES:
        return True
    try:
        return path.is_file() and zipfile.is_zipfile(path)
    except Exception:
        return False


def handle_post_download(file_path: Path, entry: Files | None = None,
                         conversion_mode: str = POST_MODE_AUTO, progress_callback=None) -> bool:
    """Extract a finished download if the mode calls for it. Returns False (after logging why) on failure.

    Never raises: any unexpected error is logged and counts as a failure, so the
    queue can move on to the next game.
    """
    file_path = Path(file_path)
    game_extract_dir = extract_dir_for(file_path)
    console = (getattr(entry, "console", "") if entry is not None else "") or console_for_download(file_path)
    mode = resolve_post_mode(conversion_mode, console)
    try:
        if not file_path.exists():
            if is_extracted(game_extract_dir):
                return True             # extracted earlier and the archive was deleted
            log_event(f"Post-processing {file_path.name} failed: the downloaded file is missing.")
            return False
        if mode == POST_MODE_KEEP or not is_archive_file(file_path):
            return True
        ok = extract_archive(file_path, game_extract_dir, progress_callback)
        if ok and delete_after_extract_enabled():
            try:
                _retry_os(file_path.unlink, tries=8)
                log_event(f"Deleted {file_path.name} after extracting it (setting is on).")
            except OSError as exc:
                log_event(f"Extracted {file_path.name} but could not delete the archive: {exc}")
        return ok
    except Exception as exc:
        import traceback
        log_event(f"Post-processing {file_path.name} failed with an unexpected error: {exc}\n"
                  f"{traceback.format_exc().rstrip()}")
        _undo_processing(game_extract_dir, "failed")
        return False


def extract_archive(file_path: Path, game_extract_dir: Path, progress_callback=None) -> bool:
    """Unpack a .zip / .7z / .rar into game_extract_dir. Half-finished output is cleaned up."""
    name = file_path.name
    if file_path.is_dir():
        log_event(f"Extracting {name} failed: the download is a folder, not a file.")
        return False

    game_extract_dir.mkdir(parents=True, exist_ok=True)
    if (game_extract_dir / PROCESSING_MARKER_NAME).exists():
        _undo_processing(game_extract_dir, "unfinished")    # left from an earlier failed attempt
        game_extract_dir.mkdir(parents=True, exist_ok=True)

    if is_extracted(game_extract_dir):
        log_event(f"{name} is already extracted - skipping.")
        return True

    if progress_callback:
        progress_callback("Extracting...")

    try:
        is_zip = zipfile.is_zipfile(file_path)
    except Exception:
        is_zip = False

    if file_path.suffix.lower() == ".zip" and not is_zip:
        log_event(f"Extraction failed for {name}: it is not a valid zip file "
                  f"(the download is probably damaged or incomplete - delete it and download again).")
        _remove_partial_dir_if_empty(game_extract_dir)
        return False

    # If the app dies mid-extraction, the next start deletes the half-extracted
    # files instead of treating them as a finished game.
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
    else:
        # .7z / .rar are unpacked in-process (py7zr / unrar2-cffi) - no 7-Zip needed.
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

    _clear_processing(game_extract_dir)
    return True


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def downloaded_files() -> list[Path]:
    """Every finished download: downloads/<console>/* plus anything loose in downloads/."""
    found: list[Path] = []
    if not DOWNLOAD_DIR.exists():
        return found
    folders = [DOWNLOAD_DIR]
    try:
        folders += sorted(p for p in DOWNLOAD_DIR.iterdir()
                          if p.is_dir() and p.name != PARTIAL_DIR_NAME)
    except OSError:
        pass
    for folder in folders:
        try:
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix.lower() != ".part":
                    found.append(path)
        except OSError:
            continue
    return found


def find_unextracted_archives() -> list[Path]:
    """Downloaded archives that have not been extracted yet."""
    return [path for path in downloaded_files()
            if is_archive_file(path) and not is_extracted(extract_dir_for(path))]


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



def _normalize_pack_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def pack_search_dirs() -> list[Path]:
    candidates = [RUN_DIR, MAIN_DIR]
    try:
        candidates.append(resource_path(""))
    except Exception:
        pass
    try:
        candidates.append(Path.cwd())
    except Exception:
        pass

    ordered: list[Path] = []
    for directory in candidates:
        try:
            resolved = Path(directory).resolve()
        except Exception:
            continue
        if resolved not in ordered:
            ordered.append(resolved)
    return ordered

