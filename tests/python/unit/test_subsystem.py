# Unit tests for PE subsystem inspection. Mirrors tests/subsystem.test.js
# using a synthetic minimal PE32+ image instead of a real binary.

import struct

import pytest

from astral_remote.builder.subsystem import (
    IMAGE_SUBSYSTEM_WINDOWS_CUI,
    IMAGE_SUBSYSTEM_WINDOWS_GUI,
    read_subsystem,
    set_gui_subsystem,
    subsystem_offset,
    with_gui_subsystem,
)


def _minimal_pe(subsystem=IMAGE_SUBSYSTEM_WINDOWS_CUI) -> bytes:
    pe_start = 64
    buf = bytearray(512)
    struct.pack_into("<H", buf, 0, 0x5A4D)
    struct.pack_into("<I", buf, 0x3C, pe_start)
    buf[pe_start : pe_start + 4] = b"PE\x00\x00"
    optional = pe_start + 4 + 20
    struct.pack_into("<H", buf, optional, 0x20B)
    struct.pack_into("<H", buf, optional + 68, subsystem)
    return bytes(buf)


def test_locates_field_and_reads_value():
    image = _minimal_pe()
    assert subsystem_offset(image) >= 0
    assert read_subsystem(image) == IMAGE_SUBSYSTEM_WINDOWS_CUI


def test_rejects_garbage():
    assert subsystem_offset(b"junk") == -1
    assert subsystem_offset(b"") == -1
    assert read_subsystem(b"junk") is None
    with pytest.raises(ValueError):
        with_gui_subsystem(b"junk")


def test_flips_copy_to_gui_leaves_input_intact():
    image = _minimal_pe()
    flipped = with_gui_subsystem(image)
    assert read_subsystem(flipped) == IMAGE_SUBSYSTEM_WINDOWS_GUI
    assert read_subsystem(image) == IMAGE_SUBSYSTEM_WINDOWS_CUI


def test_patches_file_and_reports_previous(tmp_path):
    exe = tmp_path / "app.exe"
    exe.write_bytes(_minimal_pe())
    assert set_gui_subsystem(exe) == IMAGE_SUBSYSTEM_WINDOWS_CUI
    assert read_subsystem(exe.read_bytes()) == IMAGE_SUBSYSTEM_WINDOWS_GUI
    assert set_gui_subsystem(exe) == IMAGE_SUBSYSTEM_WINDOWS_GUI
