"""Validated reader for the game's Switch 2 NUP container."""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    import zstandard
except ModuleNotFoundError:
    zstandard = None


EXPECTED_TITLE_ID = "0400486024DEC000"
NO_DIRECTORY = 0xFFFFFFFF
KINDS = {0: "base", 1: "update", 2: "addon"}
META_NAMES = {
    1: "npdm",
    3: "icon",
    4: "display_version",
    5: "title_name",
    6: "publisher",
    8: "source_dump",
    9: "localized_titles",
    10: "localized_icons",
}
MAX_TOC_SIZE = 512 << 20
MAX_MEMBER_SIZE = 2 << 30


class NupError(ValueError):
    """The NUP is malformed, unsupported, or failed an integrity check."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise NupError(message)


@dataclass(frozen=True, slots=True)
class NupFile:
    name: str
    size: int
    offset: int
    extent_size: int
    sha256: bytes
    codec: int


@dataclass(frozen=True, slots=True)
class NupPackage:
    index: int
    title_id: str
    version: int
    kind: str
    standalone_update: bool
    root_dir: int
    exefs_dir: int | None
    romfs_dir: int | None
    metadata: dict[str, str | bytes]


class NupArchive:
    """Parse the NUP table of contents and read individual verified members."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if not self.path.is_file():
            raise NupError(f"NUP does not exist: {self.path}")

        self.size = self.path.stat().st_size
        self.mtime_ns = self.path.stat().st_mtime_ns
        self.header_version = 0
        self.blob_offset = 0
        self.packages: tuple[NupPackage, ...] = ()
        self._directories: list[tuple[str, int, int]] = []
        self._files: list[NupFile] = []
        self._children: tuple[int, ...] = ()
        self._package_files: dict[int, dict[str, NupFile]] = {}
        self._parse()

    @property
    def identity(self) -> tuple[str, int, int]:
        return str(self.path.resolve()), self.size, self.mtime_ns

    def _read_at(self, stream, offset: int, size: int) -> bytes:
        _require(offset >= 0 and size >= 0 and offset + size <= self.size, "NUP read is out of bounds")
        stream.seek(offset)
        data = stream.read(size)
        _require(len(data) == size, "short NUP read")
        return data

    def _read_metadata(self, stream, offset: int, size: int) -> dict[str, str | bytes]:
        if offset == 0 or size == 0:
            return {}
        block = self._read_at(stream, offset, size)
        result: dict[str, str | bytes] = {}
        position = 0
        while position + 8 <= len(block):
            kind, _, length = struct.unpack_from("<HHI", block, position)
            payload = block[position + 8:position + 8 + length]
            _require(len(payload) == length, "NUP metadata TLV is out of bounds")
            position += 8 + length
            key = META_NAMES.get(kind, f"type_{kind}")
            binary = key in ("npdm", "icon") or key.startswith(("type_", "localized_"))
            result[key] = payload if binary else payload.decode("utf-8", "replace")
        _require(position == len(block), "NUP metadata has trailing bytes")
        return result

    def _parse(self) -> None:
        with self.path.open("rb") as stream:
            superblock = self._read_at(stream, 0, 0x80)
            _require(superblock[:4] == b"NUP\x01", "not a NUP container")
            header_size, self.header_version, toc_offset, toc_size = struct.unpack_from("<HHQQ", superblock, 4)
            toc_sha256 = superblock[0x18:0x38]
            self.blob_offset, recorded_size, package_count, flags = struct.unpack_from("<QQII", superblock, 0x38)
            _require(header_size >= 0x80 and flags == 0, "unsupported NUP superblock")
            _require(recorded_size == self.size, "NUP size differs from its superblock")
            _require(0 < toc_size <= MAX_TOC_SIZE, "NUP table of contents is unreasonably large")
            _require(
                0x80 <= toc_offset <= self.blob_offset <= self.size
                and toc_size <= self.blob_offset - toc_offset,
                "NUP table of contents is out of bounds",
            )

            toc = self._read_at(stream, toc_offset, toc_size)
            _require(hashlib.sha256(toc).digest() == toc_sha256, "NUP table of contents SHA-256 mismatch")
            magic, toc_packages, directory_count, file_count, pool_offset, pool_size = struct.unpack_from(
                "<IIQQQQ", toc, 0
            )
            _require(
                magic == 0x01434F54 and toc_packages == package_count and package_count > 0,
                "invalid NUP table of contents header",
            )

            position = 0x30
            records_end = position + package_count * 0x40 + directory_count * 0x18 + file_count * 0x50
            _require(records_end <= pool_offset and pool_offset + pool_size <= len(toc), "NUP records are out of bounds")
            string_pool = toc[pool_offset:pool_offset + pool_size]

            def read_name(offset: int, length: int) -> str:
                _require(offset >= 0 and length >= 0 and offset + length <= len(string_pool), "NUP name is out of bounds")
                try:
                    return string_pool[offset:offset + length].decode("utf-8")
                except UnicodeDecodeError as error:
                    raise NupError("NUP name is not valid UTF-8") from error

            packages: list[NupPackage] = []
            for index in range(package_count):
                title_id, root, exefs, romfs, version, kind, package_flags = struct.unpack_from(
                    "<QIIIIBB", toc, position
                )
                metadata_offset, metadata_size = struct.unpack_from("<QI", toc, position + 0x20)
                _require(kind in KINDS, f"unknown NUP package kind {kind}")
                packages.append(
                    NupPackage(
                        index=index,
                        title_id=f"{title_id:016X}",
                        version=version,
                        kind=KINDS[kind],
                        standalone_update=bool(package_flags & 1),
                        root_dir=root,
                        exefs_dir=None if exefs == NO_DIRECTORY else exefs,
                        romfs_dir=None if romfs == NO_DIRECTORY else romfs,
                        metadata=self._read_metadata(stream, metadata_offset, metadata_size),
                    )
                )
                position += 0x40

            for _ in range(directory_count):
                name_offset, name_length, first_child, child_count = struct.unpack_from("<IIII", toc, position)
                self._directories.append((read_name(name_offset, name_length), first_child, child_count))
                position += 0x18

            for _ in range(file_count):
                name_offset, name_length, size, extent_offset, extent_size = struct.unpack_from(
                    "<IIQQQ", toc, position
                )
                sha256 = toc[position + 0x20:position + 0x40]
                codec = toc[position + 0x40]
                _require(codec in (0, 1), f"unknown NUP member codec {codec}")
                _require(
                    size == 0
                    or extent_offset >= self.blob_offset
                    and extent_size > 0
                    and extent_offset + extent_size <= self.size,
                    "NUP member extent is out of bounds",
                )
                self._files.append(
                    NupFile(
                        name=read_name(name_offset, name_length),
                        size=size,
                        offset=extent_offset,
                        extent_size=extent_size,
                        sha256=sha256,
                        codec=codec,
                    )
                )
                position += 0x50

            child_count = (pool_offset - records_end) // 4
            _require(records_end + child_count * 4 == pool_offset, "NUP child table is misaligned")
            self._children = struct.unpack_from(f"<{child_count}I", toc, records_end)
            _require(
                all(child < directory_count + file_count for child in self._children),
                "NUP child index is out of bounds",
            )
            _require(all(package.root_dir < directory_count for package in packages), "NUP package root is invalid")
            self.packages = tuple(packages)

    def default_package(self) -> NupPackage:
        candidates = [package for package in self.packages if package.romfs_dir is not None]
        _require(bool(candidates), "NUP has no RomFS package")
        updates = [package for package in candidates if package.kind == "update"]
        return max(updates or candidates, key=lambda package: package.version)

    def walk(self, directory_index: int, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, NupFile]]:
        _require(depth < 128, "NUP directory tree is too deep or cyclic")
        _require(0 <= directory_index < len(self._directories), "NUP directory index is out of bounds")
        _, first_child, child_count = self._directories[directory_index]
        _require(first_child + child_count <= len(self._children), "NUP child range is out of bounds")
        for child in self._children[first_child:first_child + child_count]:
            if child < len(self._directories):
                name = self._directories[child][0]
                _require(name not in ("", ".", "..") and "/" not in name and "\\" not in name, "unsafe NUP directory")
                yield from self.walk(child, f"{prefix}{name}/", depth + 1)
                continue
            record = self._files[child - len(self._directories)]
            _require(
                record.name not in ("", ".", "..") and "/" not in record.name and "\\" not in record.name,
                "unsafe NUP file name",
            )
            yield prefix + record.name, record

    def package_files(self, package: NupPackage | None = None) -> dict[str, NupFile]:
        package = package or self.default_package()
        if package.romfs_dir is None:
            raise NupError("selected NUP package has no RomFS")
        if package.index not in self._package_files:
            entries: dict[str, NupFile] = {}
            for path, record in self.walk(package.romfs_dir):
                _require(path not in entries, f"duplicate NUP path {path!r}")
                entries[path] = record
            self._package_files[package.index] = entries
        return self._package_files[package.index]

    def read_member(self, record: NupFile, *, verify: bool = True, max_size: int = MAX_MEMBER_SIZE) -> bytes:
        _require(record.size <= max_size, f"NUP member is larger than the {max_size:,}-byte safety limit")
        if record.size == 0:
            return b""
        with self.path.open("rb") as stream:
            stored_size = record.size if record.codec == 0 else record.extent_size
            stored = self._read_at(stream, record.offset, stored_size)
        if record.codec == 0:
            data = stored
        else:
            if zstandard is None:
                raise NupError("this NUP member needs Blender's zstandard module")
            try:
                data = zstandard.ZstdDecompressor().decompress(stored, max_output_size=record.size)
            except zstandard.ZstdError as error:
                raise NupError(f"failed to decompress NUP member {record.name!r}: {error}") from error
        _require(len(data) == record.size, "decompressed NUP member has the wrong size")
        if verify:
            _require(hashlib.sha256(data).digest() == record.sha256, "NUP member SHA-256 mismatch")
        return data

    def read_path(self, path: str, package: NupPackage | None = None, *, verify: bool = True) -> bytes:
        entries = self.package_files(package)
        try:
            record = entries[path]
        except KeyError as error:
            raise NupError(f"RomFS path does not exist: {path}") from error
        return self.read_member(record, verify=verify)
