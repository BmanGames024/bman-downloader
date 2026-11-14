"""Bman Downloader settings for the coder.

Everything the app is set up with in code: which archive.org items it uses, default
limits, cache times, GitHub repos and update URLs. The app reads these when it starts;
rebuild the exe after changing any of them.

These are not the settings users change in the app (those are saved per user in
core\\app_settings.json). Defaults here are what a new user starts with.

The Retro Downloader has its own settings in retro\\bman_core.py.
"""


# ============================================================================
# APP VERSION AND UPDATES
# ============================================================================
# Raise APP_VERSION for every release and set the same number in version.json on GitHub.

APP_VERSION = "1.84"

# version.json the app checks for updates (version, url, sha256, notes)
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/BmanGames024/bman-downloader/refs/heads/main/version.json"

# a downloaded update smaller than this is treated as broken
MIN_UPDATE_BYTES = 5 * 1024 * 1024

# tries to download an update before giving up
UPDATE_DOWNLOAD_ATTEMPTS = 3


# ============================================================================
# ARCHIVE.ORG SOURCES
# ============================================================================
# Every archive.org item the game list is built from.
#   True  = its games can be converted (GOD / XEX options shown)
#   False = downloaded / extracted / uploaded as they are

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

# title ID content (XBLA, DLC, indie...): only the title ID folder is uploaded, to
# Content\0000000000000000\, with no upload folder to pick
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

# original Xbox games (covers and compatibility info come from the OG Xbox lists)
OG_XBOX_SOURCES = frozenset({
    "mxogcxpt2-xbox-ztm",
    "mxogcxo",
    "mxogcx-xbox-ztm",
})

# indie games: extracted into <title ID>\00000002\ (see arrange_indie_content)
INDIE_SOURCE = "xbox_360_indie_games"

INDIE_TITLE_ID = "584E07D2"              # every XNA indie game; used when the zip name has none

# content type folder indie games go in
INDIE_CONTENT_TYPE = "00000002"

# Themes tab
THEME_SOURCE = "xbox_360_themes"

# Gamer Pictures tab
GAMERPIC_SOURCE = "xbox_360_gamer_pictures"

# theme wallpaper previews
WALLPAPER_SOURCE = "xbox360-theme-wallpapers-zip"

# files in a source with these endings are never listed as games
EXCLUDED_EXTENSIONS = (".xml", ".sqlite", ".torrent")


# ============================================================================
# ARCHIVE.ORG CONNECTION
# ============================================================================

ARCHIVE_BASE_URL = "https://archive.org"

ARCHIVE_S3_BASE_URL = "https://s3.us.archive.org"

# page where users get their S3 API keys
ARCHIVE_S3_KEYS_URL = "https://archive.org/account/s3.php"

ARCHIVE_METADATA_URL = "https://archive.org/metadata"

# browser user agent sent to archive.org
SPOOF = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ============================================================================
# GAME LIST CACHE (archive.org)
# ============================================================================

INDEX_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60     # archive.org game list: refreshed once a week

# retry sooner when some sources failed last time
INDEX_CACHE_PARTIAL_TTL_SECONDS = 15 * 60

INDEX_FETCH_TIMEOUT = 12        # seconds per archive.org source

# sources fetched at the same time
INDEX_FETCH_WORKERS = 6

CATALOG_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60   # archive.org themes / gamer pictures: once a week


# ============================================================================
# DOWNLOADS
# ============================================================================
# Defaults for Settings > Downloads (each user can change them there).

# parallel connections per file
DEFAULT_MAX_SEGMENTS = 128

# work chunk size
DEFAULT_MIN_SEGMENT_MB = 8

# files smaller than this use one connection
DEFAULT_SEGMENT_MIN_FILE_MB = 16

# retries per chunk
DEFAULT_MAX_RETRIES = 5

# seconds; attempt N waits backoff x 2^N
DEFAULT_RETRY_BACKOFF = 1.5

# (minimum, maximum, step) of each slider in Settings > Downloads
DOWNLOAD_SETTING_RANGES = {
    "max_segments": (1, 128, 1),
    "min_segment_mb": (1, 128, 1),
    "segment_min_file_mb": (1, 256, 1),
    "max_retries": (1, 15, 1),
    "retry_backoff": (0.5, 5.0, 0.5),
}

DOWNLOAD_AHEAD_LIMIT = 5    # finished downloads allowed to wait for their turn before downloading pauses


# ============================================================================
# DOWNLOADS TAB
# ============================================================================

DOWNLOAD_QUEUE_ROWS = 15     # queued downloads that get a row on the Downloads tab ahead of time

DOWNLOAD_PAGE_SIZE = 15      # downloaded files listed per page once there are more than this

DOWNLOAD_ROW_SLACK = 0      # just-finished rows allowed to linger before the page is tidied

CATALOG_PAGE_SIZE = 1000       # themes / gamer picture packs listed per page on their tabs


# ============================================================================
# FTP UPLOADS
# ============================================================================
# Defaults for Settings > FTP (each user can change them there).

DEFAULT_FTP_PORT = 21

DEFAULT_FTP_USER = "xbox"

DEFAULT_FTP_PASS = "xbox"

# seconds
DEFAULT_FTP_TIMEOUT = 15

# files uploaded at once
DEFAULT_FTP_PARALLEL = 4

# highest choice offered
MAX_FTP_PARALLEL = 4

FTP_BLOCK_SIZE = 1024 * 1024

FTP_SOCKET_BUFFER = 4 * 1024 * 1024

FTP_PASSIVE_CONNECT_TIMEOUT = 5

# drive used when the console's drive can't be worked out
FTP_FALLBACK_DRIVE = "Hdd1"


# ============================================================================
# WHERE UPLOADS GO (FTP and HDD)
# ============================================================================

# title ID content and GOD games always go to FORCED_CONTENT_SUBPATH
FORCE_TITLE_ID_TO_CONTENT = True

FORCED_CONTENT_SUBPATH = ("Content", "0000000000000000")

# how deep to look for title ID folders in an extracted game
# (content sources look 2 levels deeper)
CONTENT_SCAN_DEPTH = 6


# ============================================================================
# HDD (direct-attached Xbox 360 drive)
# ============================================================================

DEFAULT_HDD_PARALLEL = 4

MAX_HDD_PARALLEL = 6

HDD_BLOCK_SIZE = 1024 * 1024

# how often the app checks whether the drive is plugged in
HDD_POLL_MS = 20000

# FATX file name limit
HDD_MAX_NAME = 42

# names this long get the 'pick a shorter name' prompt
HDD_RENAME_AT = 40

# name limit on a normal (non-FATX) drive
NORMAL_MAX_NAME = 255

# quick picks in the HDD folder browser (FATX drive)
HDD_COMMON_PATHS = (
    ("Root", "/"),
    ("Content/0's", "/Content/0000000000000000")
)

# quick picks in the HDD folder browser (normal drive)
HDD_COMMON_PATHS_NORMAL = (
    ("Root", "/"),
    ("Content/0's", "/Content/0000000000000000")
)


# ============================================================================
# COVER ART
# ============================================================================

# GitHub repo the covers come from
COVER_REPO = "BmanGames024/OG-Xbox-and-360-Cover-Art"

COVER_REPO_BRANCH = "main"

COVER_REPO_OG_FOLDER = "Original Xbox Covers"

COVER_REPO_360_FOLDER = "Xbox 360 Covers"

COVER_REPO_INDEX_TTL_SECONDS = 24 * 60 * 60

COVER_REPO_TIMEOUT = (5, 15)                 # (connect, read) seconds

COVER_OFFLINE_PAUSE = 2 * 60                 # after a network failure, don't retry for a bit

COVER_MIN_MATCH_SCORE = 0.01                 # as dbox: any title sharing a real word; the best wins

COVER_NUMBER_MISMATCH = 0.85                 # "Halo 3" for "Halo 2" only if nothing better

COVER_PREFETCH_COUNT = 8

COVER_PREFETCH_WORKERS = 2

# MobCat's original Xbox game list (OG Xbox title info)
MOBCAT_BASE_URL = "https://raw.githubusercontent.com/MobCat/MobCats-original-xbox-game-list/main"

MOBCAT_INDEX_TTL_SECONDS = 7 * 24 * 60 * 60

MOBCAT_OFFLINE_PAUSE = 2 * 60        # after a network failure, don't retry for a bit

MOBCAT_TIMEOUT = 20

MOBCAT_MIN_MATCH_SCORE = 0.75        # below this, no cover beats the wrong cover

MOBCAT_MAIN_TITLE_WEIGHT = 0.9     # "Blinx 2" for "Blinx 2: Masters of Time & Space"


# ============================================================================
# COMMUNITY COLLECTIONS
# ============================================================================

COMMUNITY_REPO_OWNER = "BmanGames024"

COMMUNITY_REPO_NAME = "bman-community"

COMMUNITY_BRANCH = "main"

COMMUNITY_CACHE_TTL_SECONDS = 5 * 60

COMMUNITY_TIMEOUT = 20

# limits for a shared collection
COMMUNITY_MAX_GAMES = 5000

COMMUNITY_MAX_NAME = 80

COMMUNITY_MAX_DESCRIPTION = 400

# GitHub token used to submit community collections (split in parts).
# It is built into the exe, so anyone with the exe can extract it.
COMMUNITY_TOKEN_PARTS = (
    "github_pat_11AODNBHQ08ot3K0wdpd",
    "Dt_bVRAxwrutgChJRlRdqbkxCxgSvPM",
    "oturyTtn7yVi9yD6WCELRMWa06afIM5",
)


# ============================================================================
# GAMER PICTURES AND THEME WALLPAPERS
# ============================================================================

GAMERPICS_SITE = "https://xboxgamer.pics"

GAMERPICS_ASSETS = "https://assets.xboxgamer.pics/titles"

GAMERPICS_SITE_TTL_SECONDS = 7 * 24 * 60 * 60

GAMERPICS_IMAGE_CACHE_MAX = 800

WALLPAPER_TTL_SECONDS = 7 * 24 * 60 * 60

WALLPAPER_THUMB_SIZE = (480, 270)        # kept on disk; the strip shows them smaller

WALLPAPER_THUMB_DISK_MAX = 3000          # thumbnails kept on disk before the oldest go

WALLPAPER_THUMB_MEMORY_MAX = 200


# ============================================================================
# LOOK, LOGS AND UI TIMING
# ============================================================================

DEFAULT_ACCENT = "#1F6AA5"

# colour choices in Settings > Appearance
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

# older log files in core\logs are deleted
LOG_FILES_TO_KEEP = 30

# how often background work is drawn on screen
UI_PUMP_MS = 60

UI_PUMP_BUDGET = 300

UI_PENDING_CAP = 5000
