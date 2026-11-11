"""
fatx360.py -- Xbox 360 / OG Xbox FATX volume access.

This is the engine behind the "Test (FATX)" tab. It has no GUI and no
dependency on the rest of the app, so it can be imported, unit tested and
poked at from a plain python shell:

    import fatx360
    fatx360.list_physical_drives()
    d = fatx360.open_disk(r"\\\\.\\PHYSICALDRIVE1", writable=False)
    parts = fatx360.probe_partitions(d)
    v = fatx360.FatxVolume(d, parts[-1].offset, parts[-1].size)
    v.listdir("/")

What Windows actually objects to
--------------------------------
An Xbox 360 drive has no MBR/GPT that Windows recognises and its volumes are
FATX, which Windows has no driver for, so Explorer offers to format it and
nothing else. There is no encryption in the way: the "security sector" at
0x2000 is a signed blob the console checks, it does not lock the data. So the
job here is (a) open the raw physical device, (b) find the FATX partitions at
their fixed offsets, (c) implement FATX.

Besides the retail internal drive this also opens:

  * devkit drives, which carry a small partition table in sector 0 instead of
    using the fixed retail offsets (see devkit_partitions);
  * USB sticks the console has set up as storage. Those stay FAT32 as far as
    Windows is concerned and hold a hidden Xbox360 folder full of Data0000,
    Data0001... files; joined end to end those files are the "disk", and the
    FATX volume with Content on it starts 0x20000000 in (see SplitFileDevice);
  * memory units and anything else that is one bare FATX volume from byte 0.

Layout of a retail internal drive (phat, slim and E all use this table):

    0x00002000  0x0007DFFC  Security sector           (binary, not FATX)
    0x00080000  0x80000000  System cache              (not FATX)
    0x80080000  0xA0E30000  Game cache                (not FATX)
    0x10C080000 0x0CE30000  SysExt                    FATX
    0x118EB0000 0x08000000  SysExt2                   FATX
    0x120EB0000 0x10000000  Backwards compatibility   FATX
    0x130EB0000 to end      Data (Content/Games/etc)  FATX

Endianness is the one real difference between the two consoles. Both write the
dword 0x58544146 as the superblock signature; the OG Xbox writes it
little-endian so the bytes on disk read "FATX", the 360 writes it big-endian so
they read "XTAF". Everything else in the superblock, the FAT and the directory
entries follows the same endianness, so the magic tells us how to read the
rest. Date epoch differs too: 1980 on the 360, 2000 on the OG Xbox.

Sources: free60 FATX page, mborgerson/fatx (libfatx) for the geometry maths.
"""

from __future__ import annotations

import ctypes
import datetime
import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------
# on-disk constants
# --------------------------------------------------------------------------

FATX_SIGNATURE = 0x58544146          # 'FATX' as a dword
MAGIC_LE = b"FATX"                   # OG Xbox writes the dword little-endian
MAGIC_BE = b"XTAF"                   # the 360 writes it big-endian

SUPERBLOCK_SIZE = 4096
FAT_OFFSET = 4096                    # FAT starts one superblock in
FAT_RESERVED_ENTRIES = 1             # cluster numbering starts at 1
DIRENT_SIZE = 64
MAX_FILENAME = 42

DELETED_MARKER = 0xE5
END_OF_DIR_FF = 0xFF
END_OF_DIR_00 = 0x00

ATTR_READONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20

SECURITY_SECTOR_OFFSET = 0x2000

# offset, size (None == "everything left on the disk"), label
RETAIL_PARTITIONS = [
    (0x10C080000, 0x0CE30000, "SysExt"),
    (0x118EB0000, 0x08000000, "SysExt2"),
    (0x120EB0000, 0x10000000, "Compatibility (BC)"),
    (0x130EB0000, None, "Data"),
]

# Anything smaller than this cannot hold a Data partition, so there is no point
# probing it as a 360 internal drive.
MIN_RETAIL_DISK = 0x130EB0000 + (16 * 1024 * 1024)

# A devkit drive starts with this dword (big-endian) followed by a table of
# start-sector / sector-count pairs, also big-endian, at these positions.
DEVKIT_MAGIC = 0x00020000
DEVKIT_TABLE = [("Devkit Content", 0x08), ("Devkit Dashboard", 0x10)]

# USB storage set up by the console: <drive>:\Xbox360\Data0000, Data0001...
USB_FOLDER = "Xbox360"
USB_DATA_OFFSET = 0x20000000

# A real memory unit: a small cache volume at 0, the data volume right after.
MEMORY_UNIT_DATA_OFFSET = 0x7FF000

# The size field in a directory record is 32 bits.
MAX_FILE_SIZE = 0xFFFFFFFF

# array typecodes for 2- and 4-byte unsigned values (the width of "I" is up to
# the C compiler, so check rather than assume).
_U16 = "H"
_U32 = "I" if array("I").itemsize == 4 else "L"


class FatxError(Exception):
    pass


# --------------------------------------------------------------------------
# raw block devices
# --------------------------------------------------------------------------

class BlockDevice:
    """Byte-addressable view over something that only likes sector-aligned IO."""

    sector_size = 512
    size = 0
    writable = False
    name = ""

    def _raw_read(self, lba_offset: int, length: int) -> bytes:
        raise NotImplementedError

    def _raw_write(self, lba_offset: int, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def _io_lock(self):
        """
        One lock per device. A raw read or write is "move to a position, then
        transfer", and there is a single position per handle, so two threads
        doing that at once would land data in each other's place. Created on
        first use; dict.setdefault makes that safe without a second lock.
        """
        lock = self.__dict__.get("_io_lock_obj")
        if lock is None:
            lock = self.__dict__.setdefault("_io_lock_obj", threading.RLock())
        return lock

    # -- aligned wrappers -------------------------------------------------

    def read(self, offset: int, length: int) -> bytes:
        """Read `length` bytes from any offset, aligning behind your back."""
        if length <= 0:
            return b""
        ss = self.sector_size
        start = (offset // ss) * ss
        end = ((offset + length + ss - 1) // ss) * ss
        with self._io_lock:
            raw = self._raw_read(start, end - start)
        head = offset - start
        return raw[head:head + length]

    def write(self, offset: int, data: bytes) -> None:
        """Write at any offset; partial sectors are read-modify-written."""
        if not data:
            return
        if not self.writable:
            raise FatxError("device is open read-only")
        ss = self.sector_size
        start = (offset // ss) * ss
        end = ((offset + len(data) + ss - 1) // ss) * ss
        head = offset - start
        with self._io_lock:
            if head == 0 and (offset + len(data)) == end:
                self._raw_write(start, data)
                return
            buf = bytearray(self._raw_read(start, end - start))
            buf[head:head + len(data)] = data
            self._raw_write(start, bytes(buf))


class ImageFile(BlockDevice):
    """A dumped .img/.bin. Handy for testing without risking real hardware."""

    def __init__(self, path, writable: bool = False, sector_size: int = 512):
        self.path = Path(path)
        self.writable = writable
        self.sector_size = sector_size
        self.name = str(self.path)
        self._fh = open(self.path, "r+b" if writable else "rb")
        self._fh.seek(0, os.SEEK_END)
        self.size = self._fh.tell()

    def _raw_read(self, lba_offset: int, length: int) -> bytes:
        self._fh.seek(lba_offset)
        data = self._fh.read(length)
        if len(data) < length:
            data += b"\x00" * (length - len(data))
        return data

    def _raw_write(self, lba_offset: int, data: bytes) -> None:
        self._fh.seek(lba_offset)
        self._fh.write(data)

    def flush(self) -> None:
        if self.writable:
            self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def usb_data_files(folder) -> list[str]:
    """The Data0000, Data0001... files of a console-formatted USB stick, in order."""
    found: dict[int, str] = {}
    try:
        names = os.listdir(folder)
    except OSError as exc:
        raise FatxError(f"could not read {folder}: {exc}")
    for name in names:
        m = re.match(r"^Data(\d{4})$", name, re.IGNORECASE)
        path = os.path.join(str(folder), name)
        if m and os.path.isfile(path):
            found[int(m.group(1))] = path
    if 0 not in found:
        raise FatxError(f"no Data0000 file in {folder} -- not Xbox 360 USB storage")
    for i in range(max(found) + 1):
        if i not in found:
            raise FatxError(f"Data{i:04d} is missing from {folder}")
    return [found[i] for i in range(len(found))]


class SplitFileDevice(BlockDevice):
    """
    A USB stick the console configured as storage. Windows sees an ordinary
    FAT32 drive with a hidden Xbox360 folder; the Data#### files inside it,
    joined end to end, are what the console treats as the disk.

    These are plain files, so no Administrator rights are needed.
    """

    usb_storage = True

    def __init__(self, folder, writable: bool = False):
        self.path = Path(folder)
        self.name = str(folder)
        self.writable = writable
        self.sector_size = 512
        self._files: list = []
        self._starts: list[int] = []
        self._lengths: list[int] = []
        pos = 0
        try:
            for path in usb_data_files(folder):
                fh = open(path, "r+b" if writable else "rb", buffering=0)
                self._files.append(fh)
                length = fh.seek(0, os.SEEK_END)
                self._starts.append(pos)
                self._lengths.append(length)
                pos += length
        except OSError as exc:
            self.close()
            hint = ""
            if writable and isinstance(exc, PermissionError):
                hint = " -- the stick may be write-protected or in use"
            raise FatxError(f"could not open the USB storage in {folder}: {exc}{hint}")
        except FatxError:
            self.close()
            raise
        self.size = pos

    def _spans(self, start: int, length: int):
        """Yield (file, offset inside it, byte count) covering start..start+length."""
        lo, hi = 0, len(self._starts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._starts[mid] <= start:
                lo = mid + 1
            else:
                hi = mid
        i = max(lo - 1, 0)
        while length > 0 and i < len(self._files):
            inner = start - self._starts[i]
            take = min(self._lengths[i] - inner, length)
            if take > 0:
                yield self._files[i], inner, take
                start += take
                length -= take
            i += 1

    def _raw_read(self, lba_offset: int, length: int) -> bytes:
        chunks = []
        got_total = 0
        for fh, inner, take in self._spans(lba_offset, length):
            fh.seek(inner)
            got = 0
            while got < take:
                data = fh.read(take - got)
                if not data:
                    break
                chunks.append(data)
                got += len(data)
            got_total += got
            if got < take:
                break
        out = b"".join(chunks)
        if got_total < length:
            out += b"\x00" * (length - got_total)
        return out

    def _raw_write(self, lba_offset: int, data: bytes) -> None:
        view = memoryview(data)
        for fh, inner, take in self._spans(lba_offset, len(view)):
            fh.seek(inner)
            part, view = view[:take], view[take:]
            while len(part):
                n = fh.write(part)
                if not n:
                    raise FatxError(f"write at 0x{lba_offset:x} failed -- the stick stopped accepting data")
                part = part[n:]
        if len(view):
            raise FatxError(f"write at 0x{lba_offset:x} runs past the end of the USB storage")

    def flush(self) -> None:
        if not self.writable:
            return
        for fh in self._files:
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    def close(self) -> None:
        for fh in getattr(self, "_files", []):
            try:
                fh.close()
            except Exception:
                pass


if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_NO_BUFFERING = 0x20000000
    FILE_FLAG_WRITE_THROUGH = 0x80000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0

    _k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                               ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _k32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                      ctypes.POINTER(ctypes.c_int64), ctypes.c_uint32]
    _k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]

    class _DISK_GEOMETRY(ctypes.Structure):
        _fields_ = [("Cylinders", ctypes.c_int64),
                    ("MediaType", ctypes.c_int),
                    ("TracksPerCylinder", ctypes.c_uint32),
                    ("SectorsPerTrack", ctypes.c_uint32),
                    ("BytesPerSector", ctypes.c_uint32)]

    class _DISK_GEOMETRY_EX(ctypes.Structure):
        _fields_ = [("Geometry", _DISK_GEOMETRY),
                    ("DiskSize", ctypes.c_int64),
                    ("Data", ctypes.c_ubyte * 1)]


class PhysicalDrive(BlockDevice):
    r"""\\.\PHYSICALDRIVEn. Needs an elevated process; raw IO is admin-only."""

    def __init__(self, path: str, writable: bool = False):
        if not IS_WINDOWS:
            raise FatxError("physical drive access is Windows-only")
        self.path = path
        self.name = path
        self.writable = writable
        access = GENERIC_READ | (GENERIC_WRITE if writable else 0)
        handle = _k32.CreateFileW(path, access,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  None, OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            hint = ""
            if err == 5:
                hint = (" -- access denied. Raw disk access needs the app to be "
                        "running as Administrator.")
            elif err == 2:
                hint = " -- no such drive. Re-scan the drive list."
            raise FatxError(f"could not open {path} (winerror {err}){hint}")
        self._handle = handle

        geo = _DISK_GEOMETRY_EX()
        returned = ctypes.c_uint32(0)
        ok = _k32.DeviceIoControl(handle, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX,
                                  None, 0, ctypes.byref(geo), ctypes.sizeof(geo),
                                  ctypes.byref(returned), None)
        if ok:
            self.sector_size = geo.Geometry.BytesPerSector or 512
            self.size = geo.DiskSize
        else:
            self.sector_size = 512
            self.size = 0

    def _seek(self, offset: int) -> None:
        newpos = ctypes.c_int64(0)
        if not _k32.SetFilePointerEx(self._handle, ctypes.c_int64(offset),
                                     ctypes.byref(newpos), 0):
            raise FatxError(f"seek to 0x{offset:x} failed "
                            f"(winerror {ctypes.get_last_error()})")

    def _raw_read(self, lba_offset: int, length: int) -> bytes:
        self._seek(lba_offset)
        buf = ctypes.create_string_buffer(length)
        got = ctypes.c_uint32(0)
        out = bytearray()
        done = 0
        while done < length:
            chunk = min(length - done, 8 * 1024 * 1024)
            if not _k32.ReadFile(self._handle, ctypes.byref(buf, done), chunk,
                                 ctypes.byref(got), None):
                raise FatxError(f"read at 0x{lba_offset + done:x} failed "
                                f"(winerror {ctypes.get_last_error()})")
            if got.value == 0:
                break
            done += got.value
        out = bytes(buf.raw[:done])
        if done < length:
            out += b"\x00" * (length - done)
        return out

    def _raw_write(self, lba_offset: int, data: bytes) -> None:
        self._seek(lba_offset)
        buf = ctypes.create_string_buffer(data, len(data))
        written = ctypes.c_uint32(0)
        done = 0
        while done < len(data):
            chunk = min(len(data) - done, 8 * 1024 * 1024)
            if not _k32.WriteFile(self._handle, ctypes.byref(buf, done), chunk,
                                  ctypes.byref(written), None):
                err = ctypes.get_last_error()
                hint = " -- Windows refused the write (drive may need to be Offline in Disk Management)" if err == 5 else ""
                raise FatxError(f"write at 0x{lba_offset + done:x} failed "
                                f"(winerror {err}){hint}")
            if written.value == 0:
                raise FatxError("write returned 0 bytes")
            done += written.value

    def close(self) -> None:
        # Windows buffers writes to a physical-drive handle. Closing flushes,
        # but flush first and explicitly so a failure to hit the platter is an
        # error we can see rather than something CloseHandle swallows.
        try:
            if self.writable:
                _k32.FlushFileBuffers(self._handle)
        except Exception:
            pass
        try:
            _k32.CloseHandle(self._handle)
        except Exception:
            pass

    def flush(self) -> None:
        if self.writable:
            _k32.FlushFileBuffers(self._handle)


def is_usb_storage_path(target) -> bool:
    """True for something shaped like E:\\Xbox360, whether or not it is there right now."""
    text = str(target).rstrip("\\/")
    return os.path.basename(text.replace("\\", "/")).lower() == USB_FOLDER.lower()


def open_disk(target, writable: bool = False) -> BlockDevice:
    """
    Open a \\\\.\\PHYSICALDRIVEn path, the Xbox360 folder of a console-formatted
    USB stick, or an image file.
    """
    text = str(target)
    if text.upper().startswith("\\\\.\\PHYSICALDRIVE") or text.upper().startswith("\\\\?\\"):
        return PhysicalDrive(text, writable=writable)
    if os.path.isdir(text):
        return SplitFileDevice(text, writable=writable)
    if is_usb_storage_path(text):
        raise FatxError(f"{text} is not there (winerror 2) -- is the USB stick still plugged in?")
    return ImageFile(text, writable=writable)


def is_admin() -> bool:
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------
# drive enumeration
# --------------------------------------------------------------------------

@dataclass
class DriveInfo:
    device_id: str
    model: str
    size: int
    interface: str
    serial: str
    kind: str = "physical"      # "physical" (raw disk) or "usb" (Xbox360\\Data#### files)

    @property
    def label(self) -> str:
        return f"{self.device_id}  --  {self.model or 'Unknown'}  ({human_size(self.size)})"

    @property
    def needs_admin(self) -> bool:
        """Raw disks are admin-only; the files on a USB stick are not."""
        return self.kind == "physical"


def human_size(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def list_physical_drives() -> list[DriveInfo]:
    """Enumerate physical disks. Does not need admin -- opening them does."""
    if not IS_WINDOWS:
        return []
    ps = ("Get-CimInstance Win32_DiskDrive | "
          "Select-Object DeviceID,Model,Size,InterfaceType,SerialNumber | "
          "ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(out.stdout or "[]")
    except Exception:
        return _list_drives_by_probe()

    if isinstance(payload, dict):
        payload = [payload]
    drives = []
    for row in payload:
        drives.append(DriveInfo(
            device_id=str(row.get("DeviceID") or ""),
            model=(row.get("Model") or "").strip(),
            size=int(row.get("Size") or 0),
            interface=(row.get("InterfaceType") or "").strip(),
            serial=(str(row.get("SerialNumber") or "")).strip(),
        ))
    drives.sort(key=lambda d: _drive_index(d.device_id))
    return drives


def list_usb_storage() -> list[DriveInfo]:
    """
    Drive letters that hold console-formatted USB storage (an Xbox360 folder
    with Data0000 in it). device_id is that folder, which open_disk accepts.
    """
    if not IS_WINDOWS:
        return []
    found: list[DriveInfo] = []
    try:
        _k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        _k32.GetDriveTypeW.restype = ctypes.c_uint
        # 1 == SEM_FAILCRITICALERRORS: an empty card reader must not pop up a
        # "there is no disk in the drive" box while we look through the letters.
        old_mode = _k32.SetErrorMode(1)
    except Exception:
        return found
    try:
        mask = _k32.GetLogicalDrives()
        for i in range(26):
            if not (mask >> i) & 1:
                continue
            letter = chr(65 + i)
            root = f"{letter}:\\"
            try:
                if _k32.GetDriveTypeW(root) not in (2, 3):      # removable, fixed
                    continue
                folder = root + USB_FOLDER
                if not os.path.isfile(os.path.join(folder, "Data0000")):
                    continue
                size = sum(os.path.getsize(p) for p in usb_data_files(folder))
            except (FatxError, OSError):
                continue
            found.append(DriveInfo(device_id=folder, model=f"Xbox 360 USB storage on {letter}:",
                                   size=size, interface="USB", serial="", kind="usb"))
    finally:
        try:
            _k32.SetErrorMode(old_mode)
        except Exception:
            pass
    return found


def list_storage_devices() -> list[DriveInfo]:
    """Everything FATX can live on: physical disks, then console-formatted USB sticks."""
    return list_physical_drives() + list_usb_storage()


def _drive_index(device_id: str) -> int:
    m = re.search(r"(\d+)$", device_id or "")
    return int(m.group(1)) if m else 999


def _list_drives_by_probe() -> list[DriveInfo]:
    """Fallback when PowerShell is unavailable: just try opening the first 16."""
    found = []
    for i in range(16):
        path = rf"\\.\PHYSICALDRIVE{i}"
        try:
            dev = PhysicalDrive(path, writable=False)
        except FatxError:
            continue
        found.append(DriveInfo(path, "", dev.size, "", ""))
        dev.close()
    return found


# --------------------------------------------------------------------------
# security sector
# --------------------------------------------------------------------------

@dataclass
class SecuritySector:
    serial: str
    firmware: str
    model: str
    sectors: int
    looks_valid: bool
    raw: bytes = b""

    def describe(self) -> str:
        if not self.looks_valid:
            return "No Xbox 360 security sector found at 0x2000."
        return (f"Security sector: model={self.model or '?'}  "
                f"serial={self.serial or '?'}  fw={self.firmware or '?'}  "
                f"sectors={self.sectors} ({human_size(self.sectors * 512)})")


def read_security_sector(dev: BlockDevice) -> SecuritySector:
    """
    The blob the console signs to prove the drive is a genuine MS unit. It does
    not gate reads -- it is only useful here to confirm we are looking at a 360
    drive rather than someone's backup disk.
    """
    raw = dev.read(SECURITY_SECTOR_OFFSET, 512)

    def ascii_at(off, length):
        chunk = raw[off:off + length]
        chunk = chunk.split(b"\x00")[0]
        try:
            text = chunk.decode("ascii", "ignore").strip()
        except Exception:
            text = ""
        return "".join(c for c in text if 32 <= ord(c) < 127)

    serial = ascii_at(0x00, 0x14)
    firmware = ascii_at(0x14, 0x08)
    model = ascii_at(0x1C, 0x28)
    sectors = struct.unpack_from("<I", raw, 0x58)[0]

    valid = bool(model) and sectors > 0 and raw != b"\x00" * 512
    return SecuritySector(serial, firmware, model, sectors, valid, raw)


# --------------------------------------------------------------------------
# partition probing
# --------------------------------------------------------------------------

@dataclass
class PartitionInfo:
    name: str
    offset: int
    size: int
    endian: str            # "big" == 360, "little" == OG Xbox
    sectors_per_cluster: int
    root_cluster: int
    volume_id: int

    @property
    def label(self) -> str:
        kind = "360" if self.endian == "big" else "OGXbox"
        return (f"{self.name}  @ 0x{self.offset:X}  {human_size(self.size)}  "
                f"[{kind}, {human_size(self.sectors_per_cluster * 512)} clusters]")


def _read_superblock(dev: BlockDevice, offset: int):
    head = dev.read(offset, 20)
    if len(head) < 20:
        return None
    magic = head[:4]
    if magic == MAGIC_BE:
        endian = "big"
        fmt = ">III"
    elif magic == MAGIC_LE:
        endian = "little"
        fmt = "<III"
    else:
        return None
    volume_id, spc, root = struct.unpack_from(fmt, head, 4)
    if spc not in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024):
        return None
    if root == 0 or root > 0x0FFFFFFF:
        return None
    return endian, volume_id, spc, root


def devkit_partitions(dev: BlockDevice) -> list[tuple[str, int, int]]:
    """
    (name, offset, size) for each entry in a devkit drive's partition table, or
    nothing when the drive does not start with one. The table is big-endian and
    counts in 512-byte sectors.
    """
    try:
        table = dev.read(0, 0x18)
    except Exception:
        return []
    if len(table) < 0x18 or struct.unpack_from(">I", table, 0)[0] != DEVKIT_MAGIC:
        return []
    parts = []
    for name, pos in DEVKIT_TABLE:
        sector, count = struct.unpack_from(">II", table, pos)
        offset, size = sector * 512, count * 512
        if not offset or (dev.size and offset + SUPERBLOCK_SIZE > dev.size):
            continue
        if not size or (dev.size and offset + size > dev.size):
            size = (dev.size - offset) if dev.size else size
        parts.append((name, offset, size))
    return parts


def _partition_at(dev: BlockDevice, name: str, offset: int, size: int | None):
    """A PartitionInfo if a FATX superblock really sits at `offset`, else None."""
    if dev.size and offset >= dev.size:
        return None
    sb = _read_superblock(dev, offset)
    if not sb:
        return None
    endian, volume_id, spc, root = sb
    actual = size
    if actual is None:
        actual = (dev.size - offset) if dev.size else 0
    elif dev.size:
        actual = min(actual, dev.size - offset)
    actual &= ~(dev.sector_size - 1)
    return PartitionInfo(name, offset, actual, endian, spc, root, volume_id)


def _mountable(dev: BlockDevice, part: PartitionInfo) -> bool:
    """The superblock looked right; check the geometry adds up as well."""
    try:
        FatxVolume(dev, part.offset, part.size)
        return True
    except (FatxError, OSError, struct.error):
        return False


def probe_partitions(dev: BlockDevice, include_unknown_offsets: bool = True) -> list[PartitionInfo]:
    """
    Find the FATX partitions on a device. In order: console-formatted USB
    storage has exactly one, at a fixed offset inside the Data files; a devkit
    drive lists its own in a table in sector 0; anything else is checked at the
    retail fixed offsets. Offsets are only candidates -- a partition counts as
    real when the magic is actually there, so a drive laid out differently just
    yields fewer hits instead of garbage.
    """
    found: list[PartitionInfo] = []

    if getattr(dev, "usb_storage", False):
        part = _partition_at(dev, "USB Content", USB_DATA_OFFSET, None)
        if part is not None and part.endian == "big" and _mountable(dev, part):
            found.append(part)
        return found

    for name, offset, size in devkit_partitions(dev):
        part = _partition_at(dev, name, offset, size)
        if part is not None and part.endian == "big" and _mountable(dev, part):
            found.append(part)
    taken = [(p.offset, p.offset + p.size) for p in found]

    if include_unknown_offsets and not found:
        # Some drives and most loose/rebuilt ones put a FATX volume right at 0.
        whole = _partition_at(dev, "Whole disk", 0, None)
        unit = _partition_at(dev, "Memory Unit Data", MEMORY_UNIT_DATA_OFFSET, None)
        if unit is not None and _mountable(dev, unit):
            # A memory unit: nothing on it lives at the hard drive offsets.
            if whole is not None:
                whole.name = "Memory Unit Cache"
                whole.size = MEMORY_UNIT_DATA_OFFSET
                found.append(whole)
            found.append(unit)
            return found
        if whole is not None:
            found.append(whole)

    for offset, size, name in RETAIL_PARTITIONS:
        if any(a <= offset < b for a, b in taken):
            continue
        part = _partition_at(dev, name, offset, size)
        if part is not None:
            found.append(part)

    return found


def deep_scan(dev: BlockDevice, limit: int | None = None, step: int = 4096,
              progress=None, cancel=None) -> list[PartitionInfo]:
    """
    Brute-force hunt for FATX superblocks when the fixed offsets come up empty
    (rebuilt drive, odd dump, HDD from a devkit). Reads in 8 MB gulps and looks
    for the magic on 4 KB boundaries. Sizes are guesses -- each hit runs to the
    next hit or the end of the disk.
    """
    if limit is None:
        limit = min(dev.size or 0, 12 * 1024 * 1024 * 1024)
    chunk_size = 8 * 1024 * 1024
    hits: list[tuple[int, tuple]] = []
    pos = 0
    while pos < limit:
        if cancel is not None and cancel():
            break
        n = min(chunk_size, limit - pos)
        try:
            buf = dev.read(pos, n)
        except FatxError:
            break
        for magic in (MAGIC_BE, MAGIC_LE):
            start = 0
            while True:
                idx = buf.find(magic, start)
                if idx < 0:
                    break
                start = idx + 1
                abs_off = pos + idx
                if abs_off % step:
                    continue
                sb = _read_superblock(dev, abs_off)
                if sb:
                    hits.append((abs_off, sb))
        pos += n
        if progress:
            progress(pos, limit)

    hits.sort(key=lambda h: h[0])
    out = []
    for i, (offset, (endian, volume_id, spc, root)) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else (dev.size or offset)
        name = next((nm for off, _sz, nm in RETAIL_PARTITIONS if off == offset), "Unknown")
        out.append(PartitionInfo(name, offset, max(0, end - offset), endian, spc, root, volume_id))
    return out


# --------------------------------------------------------------------------
# the filesystem itself
# --------------------------------------------------------------------------

@dataclass
class FatxEntry:
    name: str
    attributes: int
    first_cluster: int
    size: int
    created: datetime.datetime | None
    modified: datetime.datetime | None
    accessed: datetime.datetime | None
    # where the 64-byte record lives, so writes can find it again
    dirent_cluster: int = 0
    dirent_index: int = 0
    path: str = ""
    # first cluster of the directory this entry sits in (0 == not recorded)
    parent_cluster: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & ATTR_DIRECTORY)

    @property
    def size_text(self) -> str:
        return "" if self.is_dir else human_size(self.size)


def _decode_dt(date: int, time: int, epoch: int):
    if date == 0:
        return None
    year = ((date >> 9) & 0x7F) + epoch
    month = (date >> 5) & 0x0F
    day = date & 0x1F
    hour = (time >> 11) & 0x1F
    minute = (time >> 5) & 0x3F
    second = (time & 0x1F) * 2
    try:
        return datetime.datetime(year, max(1, month), max(1, day),
                                 min(23, hour), min(59, minute), min(59, second))
    except ValueError:
        return None


def _encode_dt(dt: datetime.datetime | None, epoch: int) -> tuple[int, int]:
    if dt is None:
        dt = datetime.datetime.now()
    year = max(0, min(127, dt.year - epoch))
    date = (year << 9) | ((dt.month & 0x0F) << 5) | (dt.day & 0x1F)
    time = ((dt.hour & 0x1F) << 11) | ((dt.minute & 0x3F) << 5) | ((dt.second // 2) & 0x1F)
    return date, time


VALID_NAME = re.compile(r"^[A-Za-z0-9 !#$%&'()\-.@\[\]^_`{}~+,;=]{1,42}$")


class FatxVolume:
    """
    One FATX partition. Read operations work on a read-only device; anything
    that mutates raises unless the underlying device was opened writable.

    Safe to use from several threads at once. Everything that touches the
    allocation table or a directory happens under one lock; the bulk of a file
    copy (pushing its data into clusters that were reserved up front) does not
    hold it, so several files can be on their way to the drive together.
    """

    # The uploader only runs more than one writer on volumes that say this.
    thread_safe = True

    # The FAT is cached in pieces and written through: a change lands in the
    # cached piece and on the disk in the same call, so the cache never holds
    # anything the drive does not. That is what lets it survive writes instead
    # of being thrown away after every allocation.
    FAT_CHUNK_BYTES = 256 * 1024
    FAT_CACHE_CHUNKS = 128

    IO_RUN_BYTES = 8 * 1024 * 1024

    def __init__(self, dev: BlockDevice, offset: int, size: int,
                 endian: str | None = None, epoch: int | None = None,
                 usb: bool | None = None):
        self.dev = dev
        self.offset = offset
        self.size = size
        self._lock = threading.RLock()

        sb = _read_superblock(dev, offset)
        if not sb:
            raise FatxError(f"no FATX superblock at 0x{offset:X}")
        detected_endian, self.volume_id, self.sectors_per_cluster, self.root_cluster = sb

        self.endian = endian or detected_endian
        self.epoch = epoch if epoch is not None else (1980 if self.endian == "big" else 2000)
        self._pack = ">" if self.endian == "big" else "<"

        # USB storage the console set up itself (the Xbox360\Data#### files)
        # always uses 4-byte FAT entries, however small the stick is.
        self.usb = bool(getattr(dev, "usb_storage", False)) if usb is None else bool(usb)

        # FATX counts its sectors-per-cluster in 512-byte units no matter what
        # the device reports. Some SATA-to-USB bridges and 4Kn drives present
        # 4096-byte logical sectors; multiplying by that made every cluster 8x
        # too big and threw off the FAT size and the data-area start, so the
        # volume read as garbage or errors. BlockDevice.read already aligns to
        # the real device sector size, so this stays correct on both.
        self.sector_size = 512
        self.bytes_per_cluster = self.sectors_per_cluster * 512

        # Geometry, straight out of libfatx: entry count is one per cluster plus
        # the reserved entry 0, FAT16 below 0xFFF0 entries, table rounded up to
        # 4 KB, data area starts immediately after.
        entries = self.size // self.bytes_per_cluster + FAT_RESERVED_ENTRIES
        self.fat_type = 16 if entries < 0xFFF0 else 32
        if self.usb:
            self.fat_type = 32
        self.fat_entry_size = 2 if self.fat_type == 16 else 4
        fat_bytes = entries * self.fat_entry_size
        if fat_bytes % 4096:
            fat_bytes += 4096 - (fat_bytes % 4096)
        self.fat_offset = self.offset + FAT_OFFSET
        self.fat_size = fat_bytes
        self.cluster_offset = self.fat_offset + self.fat_size
        self.num_clusters = (self.size - self.fat_size - FAT_OFFSET) // self.bytes_per_cluster
        self.num_clusters += FAT_RESERVED_ENTRIES
        # Highest cluster whose data still fits inside the partition. Nothing is
        # ever allocated past it -- the cluster after it would run off the end.
        self.max_cluster = self.num_clusters - FAT_RESERVED_ENTRIES

        self.end_marker = 0xFFFF if self.fat_type == 16 else 0xFFFFFFFF
        self._fat_fmt = self._pack + ("H" if self.fat_entry_size == 2 else "I")
        self._alloc_cursor = FAT_RESERVED_ENTRIES
        self._fat_chunks: OrderedDict = OrderedDict()
        # directory first cluster -> (entries, {lower-case name: entry})
        self._dir_cache: dict = {}
        # directory first cluster -> slot number below which nothing is free
        self._slot_hint: dict = {}
        # (directory first cluster, lower-case name) of files still being copied
        self._writing: set = set()

        if self.max_cluster < 1:
            raise FatxError(f"partition at 0x{offset:X} is too small to hold a FATX volume")
        if self.root_cluster >= self.num_clusters + FAT_RESERVED_ENTRIES:
            raise FatxError(f"root cluster {self.root_cluster} outside the volume "
                            f"-- offset 0x{offset:X} is probably not a real partition")

    # -- info ------------------------------------------------------------

    def describe(self) -> str:
        return (f"FATX{self.fat_type}  {'360 (big-endian)' if self.endian == 'big' else 'OG Xbox (little-endian)'}\n"
                f"  Offset            0x{self.offset:X}\n"
                f"  Size              {human_size(self.size)}\n"
                f"  Volume ID         {self.volume_id:08X}\n"
                f"  Cluster size      {human_size(self.bytes_per_cluster)} "
                f"({self.sectors_per_cluster} sectors)\n"
                f"  Clusters          {self.num_clusters}\n"
                f"  FAT at            0x{self.fat_offset:X} ({human_size(self.fat_size)})\n"
                f"  Data at           0x{self.cluster_offset:X}\n"
                f"  Root cluster      {self.root_cluster}")

    @property
    def writable(self) -> bool:
        return bool(self.dev.writable)

    def _require_write(self):
        if not self.writable:
            raise FatxError("volume is mounted read-only -- tick 'Enable writes' first")

    # -- FAT -------------------------------------------------------------

    def _fat_entry_offset(self, cluster: int) -> int:
        return self.fat_offset + cluster * self.fat_entry_size

    def _invalidate_fat_cache(self) -> None:
        with self._lock:
            self._fat_chunks.clear()

    def _fat_chunk(self, index: int) -> bytearray:
        chunk = self._fat_chunks.get(index)
        if chunk is not None:
            self._fat_chunks.move_to_end(index)
            return chunk
        start = index * self.FAT_CHUNK_BYTES
        length = min(self.FAT_CHUNK_BYTES, self.fat_size - start)
        if index < 0 or length <= 0:
            raise FatxError("FAT lookup outside the table")
        chunk = bytearray(self.dev.read(self.fat_offset + start, length))
        self._fat_chunks[index] = chunk
        while len(self._fat_chunks) > max(2, self.FAT_CACHE_CHUNKS):
            self._fat_chunks.popitem(last=False)
        return chunk

    def _fat_get(self, cluster: int) -> int:
        index, inner = divmod(cluster * self.fat_entry_size, self.FAT_CHUNK_BYTES)
        return struct.unpack_from(self._fat_fmt, self._fat_chunk(index), inner)[0]

    def _fat_put(self, cluster: int, value: int) -> None:
        """Change the cached copy only. Callers follow up with the disk write."""
        index, inner = divmod(cluster * self.fat_entry_size, self.FAT_CHUNK_BYTES)
        struct.pack_into(self._fat_fmt, self._fat_chunk(index), inner, value)

    def read_fat(self, cluster: int) -> int:
        if cluster < 0 or cluster >= self.num_clusters + FAT_RESERVED_ENTRIES:
            raise FatxError(f"cluster {cluster} out of range")
        with self._lock:
            return self._fat_get(cluster)

    def write_fat(self, cluster: int, value: int) -> None:
        self._require_write()
        with self._lock:
            self._fat_put(cluster, value)
            self.dev.write(self._fat_entry_offset(cluster), struct.pack(self._fat_fmt, value))

    def _write_fat_values(self, pairs) -> None:
        """
        Write many FAT entries, coalescing consecutive clusters into single
        writes. A 7 GB file is ~450k clusters; one 4-byte write each means 450k
        read-modify-write cycles on 512-byte sectors, which turns a ten minute
        copy into an overnight job. Allocation is almost always contiguous, so
        in practice this collapses to a handful of writes.

        Each run goes out as whole 512-byte sectors taken from the cached table,
        so the device never has to read a sector back just to patch part of it.
        """
        self._require_write()
        pairs = sorted(pairs)
        if not pairs:
            return
        es = self.fat_entry_size
        # Each run is changed in the cache and written out before the next one
        # is touched, and a run is capped well below the cache size. Otherwise a
        # big or badly scattered allocation could push a changed piece of the
        # table out of the cache before it reached the disk, and the change
        # would be lost.
        run_cap = max(1, self.FAT_CACHE_CHUNKS // 4) * (self.FAT_CHUNK_BYTES // es)
        with self._lock:
            i = 0
            while i < len(pairs):
                j = i
                while (j + 1 < len(pairs) and pairs[j + 1][0] == pairs[j][0] + 1
                       and j + 1 - i < run_cap):
                    j += 1
                for cluster, value in pairs[i:j + 1]:
                    self._fat_put(cluster, value)
                lo = (pairs[i][0] * es) & ~511
                hi = min(self.fat_size, ((pairs[j][0] + 1) * es + 511) & ~511)
                blob = bytearray()
                pos = lo
                while pos < hi:
                    index, inner = divmod(pos, self.FAT_CHUNK_BYTES)
                    take = min(self.FAT_CHUNK_BYTES - inner, hi - pos)
                    blob += self._fat_chunk(index)[inner:inner + take]
                    pos += take
                self.dev.write(self.fat_offset + lo, bytes(blob))
                i = j + 1

    def _is_end(self, entry: int) -> bool:
        top = 0xFFF8 if self.fat_type == 16 else 0xFFFFFFF8
        return entry >= top

    def cluster_chain(self, first: int, limit: int | None = None) -> list[int]:
        chain = []
        cluster = first
        seen = set()
        cap = limit or (self.num_clusters + 8)
        top = self.num_clusters + FAT_RESERVED_ENTRIES
        with self._lock:
            while cluster and not self._is_end(cluster):
                if cluster in seen:
                    raise FatxError(f"cluster chain loops at {cluster} -- filesystem is damaged")
                if cluster >= top:
                    raise FatxError(f"chain runs off the end of the volume at cluster {cluster}")
                seen.add(cluster)
                chain.append(cluster)
                if len(chain) > cap:
                    raise FatxError("cluster chain is implausibly long -- filesystem is damaged")
                cluster = self._fat_get(cluster)
        return chain

    def cluster_offset_of(self, cluster: int) -> int:
        if cluster < FAT_RESERVED_ENTRIES:
            raise FatxError(f"cluster {cluster} is reserved")
        return self.cluster_offset + (cluster - FAT_RESERVED_ENTRIES) * self.bytes_per_cluster

    def read_cluster(self, cluster: int) -> bytes:
        return self.dev.read(self.cluster_offset_of(cluster), self.bytes_per_cluster)

    def write_cluster(self, cluster: int, data: bytes) -> None:
        self._require_write()
        if len(data) < self.bytes_per_cluster:
            data = data + b"\x00" * (self.bytes_per_cluster - len(data))
        self.dev.write(self.cluster_offset_of(cluster), data[:self.bytes_per_cluster])

    # -- allocation ------------------------------------------------------

    def free_space(self) -> tuple[int, int]:
        """(free_bytes, total_bytes). Walks the whole FAT, so it is not free."""
        es = self.fat_entry_size
        typecode = _U16 if es == 2 else _U32
        free = 0
        step = 4 * 1024 * 1024
        with self._lock:
            pos = FAT_RESERVED_ENTRIES * es
            end = (self.max_cluster + 1) * es
            while pos < end:
                count = min(step, end - pos)
                raw = self.dev.read(self._fat_entry_offset(0) + pos, count)
                # A free entry is all zero bytes whichever way round the volume
                # stores its numbers, so there is no need to byte-swap to count.
                free += array(typecode, raw[:count]).count(0)
                pos += count
        return free * self.bytes_per_cluster, self.max_cluster * self.bytes_per_cluster

    def _next_free_entry(self, chunk: bytearray, lo: int, hi: int) -> int:
        """Index of the first free entry in chunk[lo:hi] (entry units), or -1."""
        es = self.fat_entry_size
        zero = b"\x00" * es
        pos, limit = lo * es, hi * es
        while True:
            pos = chunk.find(zero, pos, limit)
            if pos < 0:
                return -1
            if pos % es == 0:
                return pos // es
            pos += es - (pos % es)

    def _find_free_clusters(self, count: int) -> list[int]:
        """
        Scan the FAT for free entries, starting where the last allocation left
        off and wrapping once. The two passes cover disjoint ranges, so nothing
        can be picked twice and no membership test is needed -- which matters,
        because a per-candidate `in list` check is quadratic and a large file
        needs hundreds of thousands of clusters.
        """
        es = self.fat_entry_size
        per_chunk = self.FAT_CHUNK_BYTES // es
        first, last = FAT_RESERVED_ENTRIES, self.max_cluster
        found: list[int] = []
        with self._lock:
            cursor = max(first, min(self._alloc_cursor, last + 1))
            for begin, end in ((cursor, last + 1), (first, cursor)):
                pos = begin
                while pos < end and len(found) < count:
                    index, lo = divmod(pos, per_chunk)
                    base = index * per_chunk
                    chunk = self._fat_chunk(index)
                    hi = min(len(chunk) // es, end - base)
                    while lo < hi and len(found) < count:
                        i = self._next_free_entry(chunk, lo, hi)
                        if i < 0:
                            break
                        want = min(hi, i + count - len(found))
                        j = i + 1
                        if want > j and chunk.count(0, j * es, want * es) == (want - j) * es:
                            j = want                # the usual case: one clean run
                        else:
                            while j < want and not any(chunk[j * es:(j + 1) * es]):
                                j += 1
                        found.extend(range(base + i, base + j))
                        lo = j
                    pos = base + hi
                if len(found) >= count:
                    break

            if len(found) < count:
                raise FatxError(
                    f"not enough free space on the partition: needed {count} clusters "
                    f"({human_size(count * self.bytes_per_cluster)}), found {len(found)} "
                    f"({human_size(len(found) * self.bytes_per_cluster)})")
            self._alloc_cursor = found[-1] + 1
        return found

    def _allocate_chain(self, count: int) -> list[int]:
        self._require_write()
        with self._lock:
            clusters = self._find_free_clusters(count)
            pairs = [(c, clusters[i + 1] if i + 1 < len(clusters) else self.end_marker)
                     for i, c in enumerate(clusters)]
            self._write_fat_values(pairs)
        return clusters

    def _release_clusters(self, clusters) -> None:
        with self._lock:
            self._write_fat_values([(c, 0) for c in clusters])
            # Freed space may sit behind the cursor; pull the cursor back to it
            # rather than to the very front, so the next search does not have to
            # wade through everything that is already full.
            if clusters:
                self._alloc_cursor = max(FAT_RESERVED_ENTRIES,
                                         min(self._alloc_cursor, min(clusters)))

    def _free_chain(self, first: int) -> None:
        self._require_write()
        if not first or self._is_end(first):
            return
        with self._lock:
            self._release_clusters(self.cluster_chain(first))

    # -- directory entries ----------------------------------------------

    def _parse_dirent(self, raw: bytes, cluster: int, index: int, parent_path: str):
        name_len = raw[0]
        if name_len in (END_OF_DIR_FF, END_OF_DIR_00):
            return "end"
        if name_len == DELETED_MARKER:
            return "deleted"
        if name_len == 0 or name_len > MAX_FILENAME:
            return "deleted"
        attributes = raw[1]
        name = raw[2:2 + name_len].decode("latin-1", "replace")
        first_cluster, size = struct.unpack_from(self._pack + "II", raw, 0x2C)
        cd, ct, md, mt, ad, at = struct.unpack_from(self._pack + "HHHHHH", raw, 0x34)
        return FatxEntry(
            name=name,
            attributes=attributes,
            first_cluster=first_cluster,
            size=0 if (attributes & ATTR_DIRECTORY) else size,
            created=_decode_dt(cd, ct, self.epoch),
            modified=_decode_dt(md, mt, self.epoch),
            accessed=_decode_dt(ad, at, self.epoch),
            dirent_cluster=cluster,
            dirent_index=index,
            path=(parent_path.rstrip("/") + "/" + name) if parent_path != "/" else "/" + name,
        )

    def _build_dirent(self, name: str, attributes: int, first_cluster: int,
                      size: int, when: datetime.datetime | None = None) -> bytes:
        encoded = name.encode("latin-1", "replace")
        if not encoded or len(encoded) > MAX_FILENAME:
            raise FatxError(f"'{name}' is not a legal FATX name (1-42 characters)")
        buf = bytearray(b"\xFF" * DIRENT_SIZE)
        buf[0] = len(encoded)
        buf[1] = attributes
        buf[2:2 + len(encoded)] = encoded
        struct.pack_into(self._pack + "II", buf, 0x2C, first_cluster, size)
        d, t = _encode_dt(when, self.epoch)
        struct.pack_into(self._pack + "HHHHHH", buf, 0x34, d, t, d, t, d, t)
        return bytes(buf)

    @staticmethod
    def _listing_order(entry: FatxEntry):
        return (not entry.is_dir, entry.name.lower())

    def _read_dir(self, first_cluster: int, base: str) -> list[FatxEntry]:
        """Parse a directory off the disk, in the order it is stored."""
        out: list[FatxEntry] = []
        if not first_cluster:
            return out
        per_cluster = self.bytes_per_cluster // DIRENT_SIZE
        for cluster in self.cluster_chain(first_cluster):
            data = self.read_cluster(cluster)
            stop = False
            for i in range(per_cluster):
                raw = data[i * DIRENT_SIZE:(i + 1) * DIRENT_SIZE]
                if len(raw) < DIRENT_SIZE:
                    stop = True
                    break
                res = self._parse_dirent(raw, cluster, i, base)
                if res == "end":
                    stop = True
                    break
                if res == "deleted":
                    continue
                res.parent_cluster = first_cluster
                out.append(res)
            if stop:
                break
        return out

    def _dir(self, first_cluster: int, base: str) -> tuple:
        """
        (entries, name index) for a directory, read once and then kept up to
        date as things are added and removed. Copying a game used to re-read
        every folder on the way down several times per file; now each folder is
        read from the drive once.
        """
        with self._lock:
            cached = self._dir_cache.get(first_cluster)
            if cached is None:
                entries = self._read_dir(first_cluster, base)
                entries.sort(key=self._listing_order)
                index: dict = {}
                for entry in entries:
                    index.setdefault(entry.name.lower(), entry)
                cached = (entries, index)
                if first_cluster:
                    self._dir_cache[first_cluster] = cached
            return cached

    def _remember_entry(self, parent_cluster: int, base: str, raw: bytes,
                        cluster: int, index: int) -> FatxEntry:
        """Turn a record that was just written into an entry and cache it."""
        entry = self._parse_dirent(raw, cluster, index, base)
        entry.parent_cluster = parent_cluster
        cached = self._dir_cache.get(parent_cluster)
        if cached is not None:
            cached[0].append(entry)
            cached[1].setdefault(entry.name.lower(), entry)
        return entry

    def _forget_entry(self, entry: FatxEntry) -> None:
        """Drop a deleted entry from whatever is cached about its folder."""
        if entry.is_dir and entry.first_cluster:
            self._dir_cache.pop(entry.first_cluster, None)
            self._slot_hint.pop(entry.first_cluster, None)
        parent = entry.parent_cluster
        if not parent:
            # Entry came from somewhere that did not record its folder, so
            # there is no telling which listing it sits in. Start over.
            self._dir_cache.clear()
            self._slot_hint.clear()
            return
        self._slot_hint.pop(parent, None)
        cached = self._dir_cache.get(parent)
        if cached is None:
            return
        entries, index = cached
        entries[:] = [e for e in entries
                      if (e.dirent_cluster, e.dirent_index) != (entry.dirent_cluster, entry.dirent_index)]
        index.clear()
        for other in sorted(entries, key=self._listing_order):
            index.setdefault(other.name.lower(), other)

    def listdir(self, path: str = "/") -> list[FatxEntry]:
        is_root = path in ("", "/", None)
        with self._lock:
            entry = None if is_root else self.stat(path)
            if entry is None and not is_root:
                # Falling back to the root here would be far worse than an error:
                # a caller listing a folder that does not exist would silently get
                # the root's contents and believe them.
                raise FatxError(f"no such directory: {path}")
            if entry is None:
                cluster = self.root_cluster
                base = "/"
            else:
                if not entry.is_dir:
                    raise FatxError(f"{path} is a file, not a directory")
                cluster = entry.first_cluster
                base = entry.path
            return self._list_cluster_chain(cluster, base)

    def _list_cluster_chain(self, first_cluster: int, base: str) -> list[FatxEntry]:
        if not first_cluster:
            return []
        with self._lock:
            out = list(self._dir(first_cluster, base)[0])
        out.sort(key=self._listing_order)
        return out

    def stat(self, path: str) -> FatxEntry | None:
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            return None
        with self._lock:
            cluster = self.root_cluster
            base = "/"
            entry = None
            for i, part in enumerate(parts):
                match = self._dir(cluster, base)[1].get(part.lower())
                if match is None:
                    return None
                entry = match
                base = match.path
                if i < len(parts) - 1:
                    if not match.is_dir:
                        return None
                    cluster = match.first_cluster
            return entry

    # -- reading files ---------------------------------------------------

    def read_file(self, entry_or_path, chunk_size: int | None = None):
        """
        Yield the file's bytes. Consecutive clusters are pulled in one read
        rather than one per cluster -- files land contiguously in practice, so
        this is the difference between 8 MB reads and 16 KB ones.
        """
        entry = entry_or_path if isinstance(entry_or_path, FatxEntry) else self.stat(entry_or_path)
        if entry is None:
            raise FatxError(f"not found: {entry_or_path}")
        if entry.is_dir:
            raise FatxError(f"{entry.name} is a directory")
        remaining = entry.size
        if remaining <= 0 or not entry.first_cluster:
            return

        run_limit = max(1, self.IO_RUN_BYTES // self.bytes_per_cluster)
        for start, length in _cluster_runs(self.cluster_chain(entry.first_cluster), run_limit):
            if remaining <= 0:
                break
            data = self.dev.read(self.cluster_offset_of(start), length * self.bytes_per_cluster)
            block = data[:min(remaining, len(data))]
            remaining -= len(block)
            if block:
                yield block

    def extract(self, entry_or_path, dest: Path, progress=None, cancel=None) -> Path:
        entry = entry_or_path if isinstance(entry_or_path, FatxEntry) else self.stat(entry_or_path)
        if entry is None:
            raise FatxError(f"not found: {entry_or_path}")
        dest = Path(dest)
        if dest.is_dir():
            dest = dest / _safe_host_name(entry.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        done = 0
        with open(dest, "wb") as fh:
            for chunk in self.read_file(entry):
                if cancel is not None and cancel():
                    raise FatxError("cancelled")
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, entry.size)
        return dest

    def extract_tree(self, path: str, dest_dir: Path, progress=None, cancel=None) -> int:
        """Recursively pull a directory out. Returns the number of files written."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for entry in self.listdir(path):
            if cancel is not None and cancel():
                break
            target = dest_dir / _safe_host_name(entry.name)
            if entry.is_dir:
                count += self.extract_tree(entry.path, target, progress, cancel)
            else:
                if progress:
                    progress(entry.path, 0, entry.size)
                self.extract(entry, target,
                             progress=(lambda d, t, p=entry.path: progress(p, d, t)) if progress else None,
                             cancel=cancel)
                count += 1
        return count

    # -- writing ---------------------------------------------------------

    def _resolve_dir(self, path: str) -> tuple[int, str]:
        """(first cluster, canonical path) of a directory that must exist."""
        if path in ("", "/", None):
            return self.root_cluster, "/"
        entry = self.stat(path)
        if entry is None or not entry.is_dir:
            raise FatxError(f"no such directory: {path}")
        return entry.first_cluster, entry.path

    def _dir_cluster_for(self, path: str) -> int:
        return self._resolve_dir(path)[0]

    def _find_free_slot(self, dir_first_cluster: int) -> tuple[int, int]:
        """Find a usable 64-byte slot, extending the directory if it is full."""
        per_cluster = self.bytes_per_cluster // DIRENT_SIZE
        with self._lock:
            chain = self.cluster_chain(dir_first_cluster)
            if not chain:
                raise FatxError("directory has no clusters -- filesystem is damaged")
            start = self._slot_hint.get(dir_first_cluster, 0)
            for pos in range(start // per_cluster, len(chain)):
                cluster = chain[pos]
                data = self.read_cluster(cluster)
                first = start % per_cluster if pos == start // per_cluster else 0
                for i in range(first, per_cluster):
                    marker = data[i * DIRENT_SIZE]
                    if marker not in (DELETED_MARKER, END_OF_DIR_FF, END_OF_DIR_00):
                        continue
                    if marker != DELETED_MARKER:
                        self._terminate_after(chain, pos, i, data)
                    self._slot_hint[dir_first_cluster] = pos * per_cluster + i + 1
                    return cluster, i
            # Directory is full: hang another cluster off the end, blank it to 0xFF
            # so every slot in it already reads as end-of-directory.
            new_cluster = self._allocate_chain(1)[0]
            self.write_cluster(new_cluster, b"\xFF" * self.bytes_per_cluster)
            self.write_fat(chain[-1], new_cluster)
            self._slot_hint[dir_first_cluster] = len(chain) * per_cluster + 1
            return new_cluster, 0

    def _terminate_after(self, chain: list[int], pos: int, index: int, data: bytes) -> None:
        """
        The slot about to be used was the end-of-directory mark, so the slot
        after it becomes the new end. It normally already reads that way; if
        something stale is sitting there, blank its first byte so old records
        past the end cannot come back to life.
        """
        per_cluster = self.bytes_per_cluster // DIRENT_SIZE
        if index + 1 < per_cluster:
            if data[(index + 1) * DIRENT_SIZE] not in (END_OF_DIR_FF, END_OF_DIR_00):
                self.dev.write(self.cluster_offset_of(chain[pos]) + (index + 1) * DIRENT_SIZE,
                               bytes([END_OF_DIR_FF]))
        elif pos + 1 < len(chain):
            offset = self.cluster_offset_of(chain[pos + 1])
            if self.dev.read(offset, 1)[0] not in (END_OF_DIR_FF, END_OF_DIR_00):
                self.dev.write(offset, bytes([END_OF_DIR_FF]))

    def _write_dirent(self, cluster: int, index: int, raw: bytes) -> None:
        self.dev.write(self.cluster_offset_of(cluster) + index * DIRENT_SIZE, raw)

    def _check_name_free(self, dir_path: str, name: str) -> None:
        with self._lock:
            cluster, base = self._resolve_dir(dir_path)
            if name.lower() in self._dir(cluster, base)[1]:
                raise FatxError(f"'{name}' already exists in {dir_path}")

    def mkdir(self, dir_path: str, name: str) -> FatxEntry:
        self._require_write()
        _validate_name(name)
        with self._lock:
            parent_cluster, base = self._resolve_dir(dir_path)
            if name.lower() in self._dir(parent_cluster, base)[1]:
                raise FatxError(f"'{name}' already exists in {dir_path}")
            if (parent_cluster, name.lower()) in self._writing:
                raise FatxError(f"'{name}' is being written in {dir_path} right now")

            new_cluster = self._allocate_chain(1)[0]
            try:
                self.write_cluster(new_cluster, b"\xFF" * self.bytes_per_cluster)
                cluster, index = self._find_free_slot(parent_cluster)
                raw = self._build_dirent(name, ATTR_DIRECTORY, new_cluster, 0)
                self._write_dirent(cluster, index, raw)
            except Exception:
                try:
                    self._release_clusters([new_cluster])
                except Exception:
                    pass
                raise
            entry = self._remember_entry(parent_cluster, base, raw, cluster, index)
            self._dir_cache[new_cluster] = ([], {})
            return entry

    def ensure_dir(self, path: str) -> str:
        """
        mkdir -p. Returns the path that now exists. Used by the uploader, which
        mirrors a local folder tree and cannot assume any of it is there yet.
        """
        self._require_write()
        parts = [p for p in str(path).replace("\\", "/").split("/") if p]
        current = "/"
        with self._lock:
            for part in parts:
                nxt = _join(current, part)
                existing = self.stat(nxt)
                if existing is None:
                    self.mkdir(current, part)
                elif not existing.is_dir:
                    raise FatxError(f"{nxt} already exists as a file")
                current = nxt
        return current

    def write_file(self, dir_path: str, name: str, source: Path | bytes,
                   progress=None, cancel=None, overwrite: bool = False) -> FatxEntry:
        """
        Copy a host file (or a bytes blob) into the volume.

        Three steps: reserve the clusters (locked), pour the data into them (not
        locked, so other files can be doing the same), then add the directory
        record (locked). The record goes in last, so a copy that dies half way
        leaves no half-file behind -- just clusters that get handed back.
        """
        self._require_write()
        _validate_name(name)

        if isinstance(source, (bytes, bytearray)):
            blob = bytes(source)
            total = len(blob)

            def opener():
                return io.BytesIO(blob)
        else:
            src = Path(source)
            total = src.stat().st_size

            def opener():
                return open(src, "rb")

        if total > MAX_FILE_SIZE:
            raise FatxError(f"'{name}' is {human_size(total)} -- a FATX file cannot be "
                            f"larger than 4 GB")

        bpc = self.bytes_per_cluster
        needed = max(1, (total + bpc - 1) // bpc)

        with self._lock:
            parent_cluster, base = self._resolve_dir(dir_path)
            key = (parent_cluster, name.lower())
            if key in self._writing:
                raise FatxError(f"'{name}' is already being written in {dir_path}")
            existing = self._dir(parent_cluster, base)[1].get(name.lower())
            if existing is not None:
                if not overwrite:
                    raise FatxError(f"'{name}' already exists in {dir_path}")
                self.delete(existing)
            clusters = self._allocate_chain(needed)
            self._writing.add(key)

        committed = False
        try:
            # Same trick as read_file: consecutive clusters go out in one write.
            # Without this a GOD upload is millions of 16 KB writes.
            run_limit = max(1, self.IO_RUN_BYTES // bpc)
            written = 0
            with opener() as fh:
                for start, length in _cluster_runs(clusters, run_limit):
                    if cancel is not None and cancel():
                        raise FatxError("cancelled")
                    want = min(length * bpc, total - written)
                    data = _read_exactly(fh, want)
                    if len(data) < want:
                        raise FatxError("source shrank while copying")
                    written += len(data)
                    if len(data) < length * bpc:
                        data += b"\x00" * (length * bpc - len(data))
                    self.dev.write(self.cluster_offset_of(start), data)
                    if progress:
                        progress(written, total)
                if fh.read(1):
                    raise FatxError("source grew while copying")

            with self._lock:
                cluster, index = self._find_free_slot(parent_cluster)
                raw = self._build_dirent(name, ATTR_ARCHIVE, clusters[0], total)
                self._write_dirent(cluster, index, raw)
                committed = True
                return self._remember_entry(parent_cluster, base, raw, cluster, index)
        except BaseException:
            # Hand the clusters back so a failed copy does not quietly eat
            # space that nothing references any more -- but never once the
            # directory record is on the disk and pointing at them.
            if not committed:
                try:
                    self._release_clusters(clusters)
                except Exception:
                    pass
            raise
        finally:
            with self._lock:
                self._writing.discard(key)

    def delete(self, entry_or_path, recursive: bool = True) -> None:
        """Mark the entry deleted (0xE5) and release its clusters."""
        self._require_write()
        with self._lock:
            entry = entry_or_path if isinstance(entry_or_path, FatxEntry) else self.stat(entry_or_path)
            if entry is None:
                raise FatxError(f"not found: {entry_or_path}")

            if entry.is_dir:
                children = self._list_cluster_chain(entry.first_cluster, entry.path)
                if children and not recursive:
                    raise FatxError(f"{entry.name} is not empty")
                for child in children:
                    self.delete(child, recursive=True)

            if entry.first_cluster:
                self._free_chain(entry.first_cluster)

            offset = self.cluster_offset_of(entry.dirent_cluster) + entry.dirent_index * DIRENT_SIZE
            self.dev.write(offset, bytes([DELETED_MARKER]))
            self._forget_entry(entry)

    def rename(self, entry_or_path, new_name: str) -> FatxEntry:
        self._require_write()
        _validate_name(new_name)
        with self._lock:
            entry = entry_or_path if isinstance(entry_or_path, FatxEntry) else self.stat(entry_or_path)
            if entry is None:
                raise FatxError(f"not found: {entry_or_path}")
            parent = entry.path.rsplit("/", 1)[0] or "/"
            self._check_name_free(parent, new_name)

            offset = self.cluster_offset_of(entry.dirent_cluster) + entry.dirent_index * DIRENT_SIZE
            raw = bytearray(self.dev.read(offset, DIRENT_SIZE))
            encoded = new_name.encode("latin-1", "replace")
            raw[0] = len(encoded)
            raw[2:2 + MAX_FILENAME] = encoded + b"\xFF" * (MAX_FILENAME - len(encoded))
            self.dev.write(offset, bytes(raw))
            # Every path underneath a renamed folder has changed, so nothing
            # cached about the tree can be trusted any more.
            self._dir_cache.clear()
            self._slot_hint.clear()
            return self.stat(_join(parent, new_name))


# --------------------------------------------------------------------------
# formatting -- only used to build test images
# --------------------------------------------------------------------------

def format_partition(dev: BlockDevice, offset: int, size: int,
                     sectors_per_cluster: int = 32, endian: str = "big",
                     volume_id: int = 0x12345678, allow_physical: bool = False,
                     usb: bool | None = None) -> FatxVolume:
    """
    Lay down a blank FATX volume. Deliberately refuses a physical drive unless
    told twice -- the point of this function is to make test images, not to
    wipe someone's console storage by accident.
    """
    if isinstance(dev, PhysicalDrive) and not allow_physical:
        raise FatxError("refusing to format a physical drive (pass allow_physical=True)")
    if not dev.writable:
        raise FatxError("device is read-only")

    pack = ">" if endian == "big" else "<"
    magic = MAGIC_BE if endian == "big" else MAGIC_LE

    if usb is None:
        usb = bool(getattr(dev, "usb_storage", False))
    bytes_per_cluster = sectors_per_cluster * 512
    entries = size // bytes_per_cluster + FAT_RESERVED_ENTRIES
    entry_size = 4 if (usb or entries >= 0xFFF0) else 2
    fat_bytes = entries * entry_size
    if fat_bytes % 4096:
        fat_bytes += 4096 - (fat_bytes % 4096)

    sb = bytearray(b"\x00" * SUPERBLOCK_SIZE)
    sb[0:4] = magic
    struct.pack_into(pack + "III", sb, 4, volume_id, sectors_per_cluster, 1)
    dev.write(offset, bytes(sb))

    # zero the FAT
    zero = b"\x00" * min(fat_bytes, 4 * 1024 * 1024)
    remaining = fat_bytes
    pos = offset + FAT_OFFSET
    while remaining > 0:
        n = min(len(zero), remaining)
        dev.write(pos, zero[:n])
        pos += n
        remaining -= n

    vol = FatxVolume(dev, offset, size, endian=endian, usb=usb)
    # entry 0 is the media descriptor, root cluster is a one-cluster chain
    vol.write_fat(0, 0xFFFFFFF8 if vol.fat_type == 32 else 0xFFF8)
    vol.write_fat(vol.root_cluster, vol.end_marker)
    vol.write_cluster(vol.root_cluster, b"\xFF" * vol.bytes_per_cluster)
    return vol


def build_test_image(path: Path, data_size: int = 64 * 1024 * 1024) -> Path:
    """
    Produce a small stand-in for a 360 drive: correct retail offsets, a fake
    security sector and a formatted Data partition. Lets the Test tab be
    exercised end to end with no console hardware attached.
    """
    path = Path(path)
    total = 0x130EB0000 + data_size
    with open(path, "wb") as fh:
        fh.truncate(total)
    dev = ImageFile(path, writable=True)
    try:
        sec = bytearray(b"\x00" * 512)
        sec[0x00:0x14] = b"TESTSERIAL0000000000"[:0x14]
        sec[0x14:0x1C] = b"TESTFW01"
        model = b"FATX TEST DRIVE"
        sec[0x1C:0x1C + len(model)] = model
        struct.pack_into("<I", sec, 0x58, total // 512)
        dev.write(SECURITY_SECTOR_OFFSET, bytes(sec))
        format_partition(dev, 0x130EB0000, data_size, sectors_per_cluster=32)
    finally:
        dev.close()
    return path


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _validate_name(name: str) -> None:
    if not name or len(name) > MAX_FILENAME:
        raise FatxError("name must be 1-42 characters")
    if any(ch in name for ch in '\\/:*?"<>|'):
        raise FatxError(f"'{name}' contains a character FATX will not accept")


def _join(dir_path: str, name: str) -> str:
    if dir_path in ("", "/"):
        return "/" + name
    return dir_path.rstrip("/") + "/" + name


def _safe_host_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "_unnamed"


def _cluster_runs(clusters, run_limit: int):
    """Group a cluster list into (first cluster, how many) runs of neighbours."""
    start = None
    length = 0
    for cluster in clusters:
        if start is not None and cluster == start + length and length < run_limit:
            length += 1
            continue
        if start is not None:
            yield start, length
        start, length = cluster, 1
    if start is not None:
        yield start, length


def _read_exactly(fh, want: int) -> bytes:
    """Read `want` bytes, coming back short only if the file really ends."""
    if want <= 0:
        return b""
    data = fh.read(want)
    if len(data) >= want or not data:
        return data
    parts = [data]
    got = len(data)
    while got < want:
        more = fh.read(want - got)
        if not more:
            break
        parts.append(more)
        got += len(more)
    return b"".join(parts)


def _bytes_reader(data: bytes, chunk: int):
    for i in range(0, max(len(data), 1), chunk):
        yield data[i:i + chunk]


def _file_reader(path: Path, chunk: int):
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            yield block


# --------------------------------------------------------------------------
# tiny CLI, for poking at things without the GUI
# --------------------------------------------------------------------------

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="FATX / Xbox 360 disk tool")
    ap.add_argument("--drives", action="store_true", help="list physical drives")
    ap.add_argument("--make-test-image", metavar="PATH", help="build a fake 360 disk image")
    ap.add_argument("target", nargs="?", help=r"\\.\PHYSICALDRIVEn or an image file")
    ap.add_argument("--scan", action="store_true", help="probe the retail partition offsets")
    ap.add_argument("--deep", action="store_true", help="brute-force scan for FATX volumes")
    ap.add_argument("--part", type=lambda v: int(v, 0), help="partition offset to open")
    ap.add_argument("--ls", metavar="PATH", help="list a directory")
    args = ap.parse_args(argv)

    if args.drives:
        for d in list_physical_drives():
            print(d.label)
        return 0

    if args.make_test_image:
        print("wrote", build_test_image(args.make_test_image))
        return 0

    if not args.target:
        ap.print_help()
        return 1

    dev = open_disk(args.target, writable=False)
    try:
        print(f"{dev.name}: {human_size(dev.size)}, {dev.sector_size}-byte sectors")
        print(read_security_sector(dev).describe())
        parts = deep_scan(dev) if args.deep else probe_partitions(dev)
        for p in parts:
            print("  " + p.label)
        if args.part is not None or (args.ls and parts):
            offset = args.part if args.part is not None else parts[-1].offset
            match = next((p for p in parts if p.offset == offset), None)
            size = match.size if match else (dev.size - offset)
            vol = FatxVolume(dev, offset, size)
            print(vol.describe())
            for e in vol.listdir(args.ls or "/"):
                kind = "<DIR>" if e.is_dir else e.size_text
                print(f"  {kind:>12}  {e.name}")
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))