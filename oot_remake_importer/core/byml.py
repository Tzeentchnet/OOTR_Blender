"""Bounds-checked reader for the little-endian BYML subset used by the title."""

from __future__ import annotations

import struct
from typing import TypeAlias


BymlValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | bytes
    | None
    | list["BymlValue"]
    | dict[str, "BymlValue"]
)

BYML_VERSION = 7
MAX_CONTAINER_ITEMS = 2_000_000
MAX_TOTAL_ITEMS = 5_000_000
MAX_DEPTH = 128

TYPE_STRING = 0xA0
TYPE_BINARY = 0xA1
TYPE_FILE = 0xA2
TYPE_ARRAY = 0xC0
TYPE_DICTIONARY = 0xC1
TYPE_STRING_TABLE = 0xC2
TYPE_BOOL = 0xD0
TYPE_INT = 0xD1
TYPE_FLOAT = 0xD2
TYPE_UINT = 0xD3
TYPE_INT64 = 0xD4
TYPE_UINT64 = 0xD5
TYPE_DOUBLE = 0xD6
TYPE_NULL = 0xFF


class BymlError(ValueError):
    """The BYML document is malformed or outside the supported safe subset."""


class _BymlReader:
    def __init__(self, data: bytes):
        self.data = data
        self._active_nodes: set[int] = set()
        self._item_count = 0

        self._require_range(0, 0x10, "BYML header")
        if data[:2] != b"YB":
            raise BymlError("expected little-endian BYML magic")
        self.version = struct.unpack_from("<H", data, 2)[0]
        if self.version != BYML_VERSION:
            raise BymlError(
                f"unsupported BYML version {self.version}; expected {BYML_VERSION}"
            )
        key_table_offset, string_table_offset, self.root_offset = struct.unpack_from(
            "<III", data, 4
        )
        self.keys = self._read_string_table(key_table_offset, "key")
        self.strings = (
            self._read_string_table(string_table_offset, "value")
            if string_table_offset
            else ()
        )
        if not self.root_offset:
            raise BymlError("BYML has no root node")

    def _require_range(self, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset > len(self.data) - size:
            raise BymlError(f"{label} is out of bounds")

    def _u24(self, offset: int, label: str) -> int:
        self._require_range(offset, 3, label)
        return int.from_bytes(self.data[offset:offset + 3], "little")

    def _read_string_table(self, offset: int, label: str) -> tuple[str, ...]:
        if not offset:
            raise BymlError(f"BYML has no {label} string table")
        self._require_range(offset, 4, f"BYML {label} string table")
        if self.data[offset] != TYPE_STRING_TABLE:
            raise BymlError(f"BYML {label} string table has an invalid node type")
        count = self._u24(offset + 1, f"BYML {label} string count")
        if count > MAX_CONTAINER_ITEMS:
            raise BymlError(f"BYML {label} string table is unreasonably large")
        self._require_range(offset + 4, count * 4, f"BYML {label} string offsets")

        result: list[str] = []
        for index in range(count):
            relative = struct.unpack_from("<I", self.data, offset + 4 + index * 4)[0]
            start = offset + relative
            self._require_range(start, 1, f"BYML {label} string")
            end = self.data.find(b"\0", start)
            if end < 0:
                raise BymlError(f"BYML {label} string is not terminated")
            try:
                result.append(self.data[start:end].decode("utf-8"))
            except UnicodeDecodeError as error:
                raise BymlError(f"BYML {label} string is not valid UTF-8") from error
        return tuple(result)

    def _count_items(self, count: int) -> None:
        if count > MAX_CONTAINER_ITEMS:
            raise BymlError("BYML container is unreasonably large")
        self._item_count += count
        if self._item_count > MAX_TOTAL_ITEMS:
            raise BymlError("BYML document contains too many items")

    def _read_binary(self, offset: int, label: str) -> bytes:
        self._require_range(offset, 4, f"BYML {label} header")
        size = struct.unpack_from("<I", self.data, offset)[0]
        self._require_range(offset + 4, size, f"BYML {label} payload")
        return self.data[offset + 4:offset + 4 + size]

    def _read_value(self, node_type: int, value: int, depth: int) -> BymlValue:
        if node_type == TYPE_STRING:
            try:
                return self.strings[value]
            except IndexError as error:
                raise BymlError("BYML string index is out of bounds") from error
        if node_type == TYPE_BINARY:
            return self._read_binary(value, "binary")
        if node_type == TYPE_FILE:
            return self._read_binary(value, "file")
        if node_type in (TYPE_ARRAY, TYPE_DICTIONARY):
            return self._read_node(value, depth + 1)
        if node_type == TYPE_BOOL:
            if value not in (0, 1):
                raise BymlError(f"BYML boolean has invalid value {value}")
            return bool(value)
        if node_type == TYPE_INT:
            return struct.unpack("<i", struct.pack("<I", value))[0]
        if node_type == TYPE_FLOAT:
            return struct.unpack("<f", struct.pack("<I", value))[0]
        if node_type == TYPE_UINT:
            return value
        if node_type == TYPE_INT64:
            self._require_range(value, 8, "BYML signed 64-bit value")
            return struct.unpack_from("<q", self.data, value)[0]
        if node_type == TYPE_UINT64:
            self._require_range(value, 8, "BYML unsigned 64-bit value")
            return struct.unpack_from("<Q", self.data, value)[0]
        if node_type == TYPE_DOUBLE:
            self._require_range(value, 8, "BYML double value")
            return struct.unpack_from("<d", self.data, value)[0]
        if node_type == TYPE_NULL:
            return None
        raise BymlError(f"unsupported BYML node type {node_type:#04x}")

    def _read_node(self, offset: int, depth: int) -> BymlValue:
        if depth >= MAX_DEPTH:
            raise BymlError("BYML nesting is too deep")
        self._require_range(offset, 4, "BYML container")
        if offset in self._active_nodes:
            raise BymlError("BYML contains a container cycle")

        node_type = self.data[offset]
        count = self._u24(offset + 1, "BYML container count")
        self._count_items(count)
        self._active_nodes.add(offset)
        try:
            if node_type == TYPE_ARRAY:
                self._require_range(offset + 4, count, "BYML array type table")
                values_offset = (offset + 4 + count + 3) & ~3
                self._require_range(values_offset, count * 4, "BYML array values")
                return [
                    self._read_value(
                        self.data[offset + 4 + index],
                        struct.unpack_from("<I", self.data, values_offset + index * 4)[0],
                        depth,
                    )
                    for index in range(count)
                ]

            if node_type == TYPE_DICTIONARY:
                self._require_range(offset + 4, count * 8, "BYML dictionary entries")
                result: dict[str, BymlValue] = {}
                for index in range(count):
                    entry = offset + 4 + index * 8
                    key_index = self._u24(entry, "BYML dictionary key")
                    try:
                        key = self.keys[key_index]
                    except IndexError as error:
                        raise BymlError("BYML key index is out of bounds") from error
                    if key in result:
                        raise BymlError(f"BYML dictionary repeats key {key!r}")
                    value = struct.unpack_from("<I", self.data, entry + 4)[0]
                    result[key] = self._read_value(self.data[entry + 3], value, depth)
                return result

            raise BymlError(f"unsupported BYML container type {node_type:#04x}")
        finally:
            self._active_nodes.remove(offset)

    def read(self) -> BymlValue:
        return self._read_node(self.root_offset, 0)


def read_byml(data: bytes) -> BymlValue:
    """Read a complete little-endian BYML 7 document."""

    return _BymlReader(data).read()
