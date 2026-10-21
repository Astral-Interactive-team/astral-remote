# Windows PE subsystem switch for silent production EXE.
# The packager offers no console/GUI option on its own path, so the
# builder verifies (and if needed flips) the subsystem field after
# packaging: CUI (console window on launch) becomes GUI (no window on
# double-click). Only two header bytes change. Flipping invalidates
# Authenticode signatures; unsigned builds are unaffected.
# Ported from src/builder/subsystem.js using struct instead of Buffer.

from __future__ import annotations

import struct
from pathlib import Path

IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
IMAGE_SUBSYSTEM_WINDOWS_CUI = 3
_PE32PLUS_MAGIC = 0x20B
_SUBSYSTEM_OFFSET_FROM_OPTIONAL = 68


def subsystem_offset(data: bytes) -> int:
    """Locate the subsystem field in a PE image. Returns the byte offset or -1."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 64:
        return -1
    if struct.unpack_from("<H", data, 0)[0] != 0x5A4D:
        return -1
    pe_start = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_start + 6 > len(data):
        return -1
    if bytes(data[pe_start : pe_start + 4]) != b"PE\x00\x00":
        return -1
    optional_start = pe_start + 4 + 20
    if optional_start + 2 > len(data):
        return -1
    if struct.unpack_from("<H", data, optional_start)[0] != _PE32PLUS_MAGIC:
        return -1
    field = optional_start + _SUBSYSTEM_OFFSET_FROM_OPTIONAL
    if field + 2 > len(data):
        return -1
    return field


def with_gui_subsystem(data: bytes) -> bytes:
    """Return a copy of the buffer with the GUI subsystem set. Throws on non-PE input."""
    at = subsystem_offset(data)
    if at < 0:
        raise ValueError("Not a 64-bit Windows executable.")
    out = bytearray(data)
    struct.pack_into("<H", out, at, IMAGE_SUBSYSTEM_WINDOWS_GUI)
    return bytes(out)


def read_subsystem(data: bytes) -> int | None:
    """Read the current subsystem value, or None when not a PE image."""
    at = subsystem_offset(data)
    if at < 0:
        return None
    return struct.unpack_from("<H", data, at)[0]


def set_gui_subsystem(exe_path) -> int:
    """Patch the file at exe_path to the GUI subsystem. Returns the previous value."""
    path = Path(exe_path)
    data = path.read_bytes()
    previous = read_subsystem(data)
    if previous is None:
        raise ValueError("Not a 64-bit Windows executable.")
    if previous == IMAGE_SUBSYSTEM_WINDOWS_GUI:
        return previous
    path.write_bytes(with_gui_subsystem(data))
    return previous
