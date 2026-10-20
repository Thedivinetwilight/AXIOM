"""Portable-executable metadata reader (pure standard library).

Ownership detection needs real evidence about a file, and on Windows the
richest evidence a binary carries is its version resource: ``ProductName``,
``CompanyName``, ``InternalName``. This module reads that resource with no
third-party code and no Win32 calls, so the identical logic runs inside the
frozen Windows uninstaller and inside the cross-platform test suite.

The reader never raises on malformed input: a corrupt or non-PE file simply
reports no metadata, which the classifier treats as "no metadata signal".
"""

from __future__ import annotations

import struct
from pathlib import Path

PE_MAGIC = b"MZ"
PE_SIGNATURE = b"PE\0\0"
RT_VERSION = 16
VS_FIXEDFILEINFO_SIGNATURE = 0xFEEF04BD

_STRING_KEYS = (
    "CompanyName", "FileDescription", "FileVersion", "InternalName",
    "LegalCopyright", "OriginalFilename", "ProductName", "ProductVersion",
)


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def is_pe(data: bytes) -> bool:
    """Cheap check that ``data`` starts like a Windows executable."""
    return data[:2] == PE_MAGIC and b"PE\0\0" in data[:0x400]


def parse_version_info(blob: bytes) -> dict[str, str]:
    """Parse a ``VS_VERSION_INFO`` resource blob into a string dictionary.

    Returns whatever ``StringFileInfo`` strings are present (``ProductName``,
    ``CompanyName``, ...). Fixed-file-info fields are exposed as
    ``FileVersion``/``ProductVersion`` when the string table does not carry
    them. Never raises.
    """
    result: dict[str, str] = {}
    try:
        if len(blob) < 6:
            return result
        total_length = _u16(blob, 0)
        value_length = _u16(blob, 2)
        wtype = _u16(blob, 4)
        key, offset = _read_version_key(blob, 6)
        if key != "VS_VERSION_INFO":
            return result
        offset = _align4(offset)
        if value_length >= 52 and wtype == 0 and offset + 52 <= len(blob):
            sig = _u32(blob, offset)
            if sig == VS_FIXEDFILEINFO_SIGNATURE:
                fms, fls = _u32(blob, offset + 8), _u32(blob, offset + 12)
                pms, pls = _u32(blob, offset + 16), _u32(blob, offset + 20)
                result["FileVersionFixed"] = _four_part(fms, fls)
                result["ProductVersionFixed"] = _four_part(pms, pls)
        limit = min(len(blob), total_length or len(blob))
        # Value is in bytes when wType == 0; children begin after it, aligned.
        offset = _align4(offset + (value_length if wtype == 0 else value_length * 2))
        while offset + 6 <= limit:
            child_len = _u16(blob, offset)
            if child_len < 6:
                break
            _parse_version_child(blob[offset:offset + child_len], result)
            offset = _align4(offset + child_len)
    except (struct.error, IndexError, ValueError, UnicodeError):
        return result
    if "ProductVersion" not in result and result.get("ProductVersionFixed"):
        result["ProductVersion"] = result["ProductVersionFixed"]
    if "FileVersion" not in result and result.get("FileVersionFixed"):
        result["FileVersion"] = result["FileVersionFixed"]
    result.pop("ProductVersionFixed", None)
    result.pop("FileVersionFixed", None)
    return result


def _four_part(ms: int, ls: int) -> str:
    return f"{(ms >> 16) & 0xFFFF}.{ms & 0xFFFF}.{(ls >> 16) & 0xFFFF}.{ls & 0xFFFF}"


def _read_version_key(blob: bytes, off: int) -> tuple[str, int]:
    chars: list[str] = []
    while off + 2 <= len(blob):
        code = _u16(blob, off)
        off += 2
        if code == 0:
            break
        chars.append(chr(code))
    return "".join(chars), off


def _align4(off: int) -> int:
    return (off + 3) & ~3


def _parse_version_child(child: bytes, out: dict[str, str]) -> None:
    """Parse one child of VS_VERSION_INFO (StringFileInfo / VarFileInfo)."""
    wtype = _u16(child, 4)
    key, offset = _read_version_key(child, 6)
    offset = _align4(offset)
    if key == "StringFileInfo" or (wtype == 1 and key != "VarFileInfo"):
        limit = len(child)
        while offset + 6 <= limit:
            table_len = _u16(child, offset)
            if table_len < 6:
                break
            _parse_string_table(child[offset:offset + table_len], out)
            offset = _align4(offset + table_len)


def _parse_string_table(table: bytes, out: dict[str, str]) -> None:
    # wLength, wValueLength, wType (three WORDs) precede szKey in every one
    # of these structures, including StringTable.
    _key, offset = _read_version_key(table, 6)
    offset = _align4(offset)
    limit = len(table)
    while offset + 6 <= limit:
        entry_len = _u16(table, offset)
        if entry_len < 6:
            break
        _parse_string_entry(table[offset:offset + entry_len], out)
        offset = _align4(offset + entry_len)


def _parse_string_entry(entry: bytes, out: dict[str, str]) -> None:
    value_words = _u16(entry, 2)
    key, offset = _read_version_key(entry, 6)
    offset = _align4(offset)
    end = min(len(entry), offset + value_words * 2)
    chars: list[str] = []
    while offset + 2 <= end:
        code = _u16(entry, offset)
        offset += 2
        if code == 0:
            break
        chars.append(chr(code))
    if key:
        out[key] = "".join(chars)


# ---------------------------------------------------------------------------
# PE walk: locate the RT_VERSION resource inside a file
# ---------------------------------------------------------------------------

Section = tuple[int, int, int, int]  # (virtual_address, virtual_size, raw_ptr, raw_size)


def _sections(data: bytes, section_off: int, count: int) -> list[Section]:
    sections: list[Section] = []
    for i in range(count):
        off = section_off + i * 40
        if off + 40 > len(data):
            break
        virt_size = _u32(data, off + 8)
        virt_addr = _u32(data, off + 12)
        raw_size = _u32(data, off + 16)
        raw_ptr = _u32(data, off + 20)
        sections.append((virt_addr, virt_size, raw_ptr, raw_size))
    return sections


def _rva_to_offset(rva: int, sections: list[Section]) -> int:
    for virt_addr, virt_size, raw_ptr, raw_size in sections:
        span = max(virt_size, raw_size)
        if virt_addr <= rva < virt_addr + span:
            return rva - virt_addr + raw_ptr
    return rva


def _find_version_blob(data: bytes) -> bytes | None:
    if data[:2] != PE_MAGIC:
        return None
    try:
        pe_off = _u32(data, 0x3C)
        if data[pe_off:pe_off + 4] != PE_SIGNATURE:
            return None
        coff = pe_off + 4
        section_count = _u16(data, coff + 2)
        opt_size = _u16(data, coff + 16)
        opt = coff + 20
        magic = _u16(data, opt)
        if magic == 0x20B:      # PE32+
            dir_off = opt + 112
        elif magic == 0x10B:    # PE32
            dir_off = opt + 96
        else:
            return None
        if dir_off + 24 > len(data):
            return None
        res_rva = _u32(data, dir_off + 2 * 8)
        res_size = _u32(data, dir_off + 2 * 8 + 4)
        if res_rva == 0 or res_size == 0:
            return None
        sections = _sections(data, opt + opt_size, section_count)
        res_off = _rva_to_offset(res_rva, sections)
        return _walk_resource_dir(data, res_off, res_off, sections, 0)
    except (struct.error, IndexError, OverflowError):
        return None


def _walk_resource_dir(data: bytes, dir_off: int, res_base: int,
                       sections: list[Section], level: int) -> bytes | None:
    if level > 4 or dir_off + 16 > len(data) or dir_off < 0:
        return None
    named = _u16(data, dir_off + 12)
    ids = _u16(data, dir_off + 14)
    entry_off = dir_off + 16
    for i in range(named + ids):
        e = entry_off + i * 8
        if e + 8 > len(data):
            return None
        entry_id = _u32(data, e)
        offset = _u32(data, e + 4)
        if level == 0 and (entry_id & 0xFFFF) != RT_VERSION:
            continue
        if offset & 0x80000000:
            child = _walk_resource_dir(data, res_base + (offset & 0x7FFFFFFF),
                                       res_base, sections, level + 1)
            if child is not None:
                return child
        else:
            data_entry = res_base + offset
            if data_entry + 16 > len(data) or data_entry < 0:
                return None
            payload_rva = _u32(data, data_entry)
            size = _u32(data, data_entry + 4)
            payload_off = _rva_to_offset(payload_rva, sections)
            if payload_off < 0 or payload_off >= len(data):
                return None
            return data[payload_off:min(len(data), payload_off + size)]
    return None


def read_version_info(path: Path | str) -> dict[str, str]:
    """Read the version resource of a PE file. ``{}`` when unavailable."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return {}
    blob = _find_version_blob(data)
    if not blob:
        return {}
    return parse_version_info(blob)


def product_strings(path: Path | str) -> dict[str, str]:
    """Metadata relevant to ownership, normalised for comparison."""
    info = read_version_info(path)
    return {
        "product": (info.get("ProductName") or "").strip(),
        "company": (info.get("CompanyName") or "").strip(),
        "description": (info.get("FileDescription") or "").strip(),
        "internal": (info.get("InternalName") or "").strip(),
        "original": (info.get("OriginalFilename") or "").strip(),
        "product_version": (info.get("ProductVersion") or "").strip(),
    }


def summarise(meta: dict[str, str]) -> str:
    """One-line audit description of metadata evidence."""
    parts = []
    if meta.get("product"):
        parts.append(f"ProductName={meta['product']!r}")
    if meta.get("company"):
        parts.append(f"CompanyName={meta['company']!r}")
    if meta.get("product_version"):
        parts.append(f"ProductVersion={meta['product_version']!r}")
    return ", ".join(parts) if parts else "no product strings"
