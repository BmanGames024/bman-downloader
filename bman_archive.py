"""Archive extraction for .7z and .rar without 7-Zip or any other .exe.

.7z  -> py7zr        (pip install py7zr)
.rar -> unrar2-cffi  (pip install unrar2-cffi) - RarLab's own UnRAR source built as a
                      Python extension, so RAR4/RAR5 and multi-part sets work.

Both stream straight to disk, so multi-GB ISOs never sit in memory.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

RAR_7Z_SUFFIXES = (".rar", ".7z")

ProgressFn = Callable[[str], None] | None


class ArchiveExtractError(Exception):
    """Extraction failed; the message is meant for the log as-is."""


# ----------------------------------------------------------------------------- helpers

class _Progress:
    """Turns byte counts into "Extracting archive... 42%" messages, at most one per percent."""

    def __init__(self, callback: ProgressFn, total: int):
        self.callback = callback
        self.total = max(int(total or 0), 0)
        self.last_pct = -1
        self.last_time = 0.0

    def update(self, done: int) -> None:
        if not self.callback:
            return
        if self.total > 0:
            pct = min(int(done * 100 / self.total), 100)
            if pct == self.last_pct:
                return
            self.last_pct = pct
            text = f"Extracting archive... {pct}%"
        else:
            now = time.monotonic()
            if now - self.last_time < 1.0:
                return
            self.last_time = now
            text = f"Extracting archive... {done / (1024 ** 3):.2f} GB"
        try:
            self.callback(text)
        except Exception:
            pass


def archive_support() -> dict[str, str | None]:
    """{'7z': None or reason it is unavailable, 'rar': None or reason}."""
    status: dict[str, str | None] = {}
    try:
        _load_py7zr()
        status["7z"] = None
    except ArchiveExtractError as exc:
        status["7z"] = str(exc)
    try:
        _load_unrar()
        status["rar"] = None
    except ArchiveExtractError as exc:
        status["rar"] = str(exc)
    return status


def extract_archive(archive: Path, dest: Path, progress: ProgressFn = None) -> None:
    """Extract a .7z or .rar into dest. Raises ArchiveExtractError on any failure."""
    suffix = Path(archive).suffix.lower()
    if suffix == ".7z":
        extract_7z(archive, dest, progress)
    elif suffix == ".rar":
        extract_rar(archive, dest, progress)
    else:
        raise ArchiveExtractError(f"{Path(archive).name} is not a .7z or .rar file")


# ----------------------------------------------------------------------------- .7z

def _load_py7zr():
    try:
        import py7zr
        return py7zr
    except Exception as exc:
        raise ArchiveExtractError(f"7z support is missing (py7zr could not load: {exc}). "
                                  "Run: pip install py7zr") from exc


def extract_7z(archive: Path, dest: Path, progress: ProgressFn = None) -> None:
    py7zr = _load_py7zr()
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        from py7zr.exceptions import PasswordRequired, UnsupportedCompressionMethodError
    except Exception:                       # very old py7zr
        PasswordRequired = UnsupportedCompressionMethodError = ()  # type: ignore

    try:
        with py7zr.SevenZipFile(archive, mode="r") as zf:
            total = 0
            try:
                total = int(zf.archiveinfo().uncompressed)
            except Exception:
                pass
            callback = _make_7z_callback(py7zr, _Progress(progress, total))
            if callback is not None:
                try:
                    zf.extractall(path=dest, callback=callback)
                    return
                except TypeError:           # py7zr version without the callback argument
                    pass
            zf.extractall(path=dest)
    except ArchiveExtractError:
        raise
    except PasswordRequired as exc:
        raise ArchiveExtractError(f"{archive.name} is password protected") from exc
    except UnsupportedCompressionMethodError as exc:
        raise ArchiveExtractError(f"{archive.name} uses a 7z method py7zr cannot unpack "
                                  f"(usually BCJ2): {exc}") from exc
    except MemoryError as exc:
        raise ArchiveExtractError(f"Not enough memory to unpack {archive.name}") from exc
    except OSError as exc:
        raise ArchiveExtractError(f"Could not write the files from {archive.name} "
                                  f"(disk full or no permission?): {exc}") from exc
    except Exception as exc:
        raise ArchiveExtractError(f"{archive.name} could not be unpacked "
                                  f"(damaged or incomplete download?): "
                                  f"{type(exc).__name__}: {exc}") from exc


def _make_7z_callback(py7zr, reporter: _Progress):
    if reporter.callback is None:
        return None
    try:
        from py7zr.callbacks import ExtractCallback
    except Exception:
        return None

    class _Callback(ExtractCallback):
        def __init__(self):
            self.done = 0

        def report_start_preparation(self):
            pass

        def report_start(self, processing_file_path, processing_bytes):
            pass

        def report_update(self, decompressed_bytes):
            # py7zr sends the bytes decompressed since the previous update, as a string
            try:
                self.done += int(decompressed_bytes)
            except (TypeError, ValueError):
                return
            reporter.update(self.done)

        def report_end(self, processing_file_path, wrote_bytes):
            pass

        def report_postprocess(self):
            pass

        def report_warning(self, message):
            pass

    try:
        return _Callback()
    except Exception:
        return None


# ----------------------------------------------------------------------------- .rar

# From UnRAR's dll.hpp
_UCM_CHANGEVOLUME, _UCM_PROCESSDATA, _UCM_NEEDPASSWORD = 0, 1, 2
_UCM_CHANGEVOLUMEW, _UCM_NEEDPASSWORDW = 3, 4
_RAR_VOL_ASK = 0
_ERAR_SUCCESS, _ERAR_END_ARCHIVE = 0, 10
_ERAR_MESSAGES = {
    11: "not enough memory",
    12: "the data is damaged (CRC error) - the download is probably corrupt",
    13: "the archive is damaged",
    14: "unknown archive format",
    15: "a file could not be opened (missing part of a multi-part set?)",
    16: "a file could not be created (no permission, or a bad path?)",
    17: "a file could not be closed",
    18: "read error",
    19: "write error (disk full?)",
    20: "buffer too small",
    21: "unknown error",
    22: "the archive is password protected",
    23: "a file reference could not be resolved",
    24: "wrong password",
    25: "the archive needs a dictionary larger than this PC allows",
}


def _load_unrar():
    try:
        from unrar.cffi import unrarlib          # registers the C callback stub
        from unrar.cffi._unrarlib import ffi, lib
        return ffi, lib, unrarlib
    except Exception as exc:
        raise ArchiveExtractError(f"RAR support is missing (unrar2-cffi could not load: {exc}). "
                                  "Run: pip install unrar2-cffi") from exc


def _rar_error(code: int, name: str = "") -> str:
    where = f" ({name})" if name else ""
    return f"{_ERAR_MESSAGES.get(code, f'UnRAR error {code}')}{where}"


def _rar_total_size(unrarlib, archive: Path) -> int:
    """Uncompressed size of everything in the archive, from the headers only (fast)."""
    sizes: dict[str, int] = {}
    try:
        with unrarlib.RarArchive.open_for_metadata(str(archive)) as rar:
            for header in rar.iterate_headers():
                try:
                    if not (header.Flags & unrarlib.FLAGS_RHDF_DIRECTORY):
                        size = header.UnpSize + (header.UnpSizeHigh << 32)
                        name = header.FileNameW
                        sizes[name] = max(sizes.get(name, 0), size)   # split files repeat
                finally:
                    header.skip()
    except Exception:
        return sum(sizes.values())
    return sum(sizes.values())


def extract_rar(archive: Path, dest: Path, progress: ProgressFn = None) -> None:
    ffi, lib, unrarlib = _load_unrar()
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    reporter = _Progress(progress, _rar_total_size(unrarlib, archive))
    state = {"done": 0, "problem": None}

    def on_event(msg, p1, p2):
        try:
            if msg == _UCM_PROCESSDATA:
                state["done"] += int(p2)
                reporter.update(state["done"])
                return 1
            if msg in (_UCM_CHANGEVOLUME, _UCM_CHANGEVOLUMEW):
                if p2 == _RAR_VOL_ASK:          # next part is not on disk - stop, don't loop
                    try:
                        if msg == _UCM_CHANGEVOLUMEW:
                            missing = ffi.string(ffi.cast("wchar_t *", p1))
                        else:
                            missing = ffi.string(ffi.cast("char *", p1)).decode("utf-8", "replace")
                        state["problem"] = f"the next part is missing: {Path(missing).name}"
                    except Exception:
                        state["problem"] = "the next part of this multi-part archive is missing"
                    return -1
                return 1
            if msg in (_UCM_NEEDPASSWORD, _UCM_NEEDPASSWORDW):
                state["problem"] = "the archive is password protected"
                return -1
            return 1
        except Exception:
            return 1

    user_data = ffi.new_handle(on_event)                       # must stay alive until close
    open_data = unrarlib.RAROpenArchiveDataEx(str(archive), lib.C_RAR_OM_EXTRACT)
    handle = lib.RAROpenArchiveEx(open_data.value)
    open_result = open_data.value.OpenResult
    if open_result != _ERAR_SUCCESS or handle == ffi.NULL:
        if handle != ffi.NULL:
            lib.RARCloseArchive(handle)
        raise ArchiveExtractError(f"{archive.name} could not be opened: {_rar_error(open_result)}")

    dest_w = ffi.new("wchar_t[]", str(dest))
    current = ""
    try:
        lib.RARSetCallbackPtr(handle, lib.PyUNRARCALLBACKStub, user_data)
        while True:
            header = ffi.new("struct RARHeaderDataEx *")
            result = lib.RARReadHeaderEx(handle, header)
            if result == _ERAR_END_ARCHIVE:
                break
            if result != _ERAR_SUCCESS:
                raise ArchiveExtractError(
                    f"{archive.name}: {state['problem'] or _rar_error(result)}")
            try:
                current = ffi.string(header.FileNameW)
            except Exception:
                current = ""
            result = lib.RARProcessFileW(handle, lib.C_RAR_EXTRACT, dest_w, ffi.NULL)
            if result != _ERAR_SUCCESS:
                raise ArchiveExtractError(
                    f"{archive.name}: {state['problem'] or _rar_error(result, current)}")
    finally:
        try:
            lib.RARSetCallbackPtr(handle, ffi.NULL, ffi.NULL)
        except Exception:
            pass
        lib.RARCloseArchive(handle)
    reporter.update(reporter.total or state["done"])
