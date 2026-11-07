"""Decode dictionary-compressed RomFS resources and locate model textures."""

from __future__ import annotations

import re
import struct
from pathlib import PurePosixPath

import zstandard

from .nup import NupArchive, NupError, NupPackage


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MODEL_SUFFIX = ".bfres.zs"
TEXTURE_SUFFIX = ".bntx.zs"
DICTIONARY_PATH = "Pack/ZsDic.pack.zs"
MAX_RESOURCE_SIZE = 2 << 30
ASCII_STRING = re.compile(rb"[A-Za-z0-9_./-]{4,}")


class ResourceError(ValueError):
    """A compressed resource or archive is malformed or unsupported."""


def _checked(data: bytes, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ResourceError("archive range is out of bounds")
    return data[offset:offset + size]


def read_sarc(data: bytes) -> dict[str, bytes]:
    """Read the safe subset of SARC used by the game's zstd dictionary pack."""
    if data[:4] != b"SARC":
        raise ResourceError("expected a SARC archive")
    endian = "<" if data[6:8] == b"\xff\xfe" else ">"
    header_size, file_size, data_start = struct.unpack_from(endian + "HxxII", data, 4)
    if file_size != len(data) or header_size < 20 or _checked(data, header_size, 4) != b"SFAT":
        raise ResourceError("invalid SARC header")
    count = struct.unpack_from(endian + "H", data, header_size + 6)[0]
    names_start = header_size + 12 + count * 16
    if _checked(data, names_start, 4) != b"SFNT":
        raise ResourceError("invalid SARC string table")
    names_start += 8
    result: dict[str, bytes] = {}
    for index in range(count):
        _, attribute, low, high = struct.unpack_from(
            endian + "4I", _checked(data, header_size + 12 + index * 16, 16)
        )
        name_offset = names_start + (attribute & 0xFFFFFF) * 4
        if attribute >> 24 != 1 or not names_start <= name_offset < data_start or high < low:
            raise ResourceError("invalid SARC member")
        try:
            name_end = data.index(0, name_offset, data_start)
            name = data[name_offset:name_end].decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise ResourceError("invalid SARC member name") from error
        if (
            name.startswith("/")
            or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            or name in result
        ):
            raise ResourceError("unsafe or duplicate SARC member name")
        result[name] = _checked(data, data_start + low, high - low)
    return result


def classify_bfres(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".camera.bfres.zs"):
        return "camera"
    if lowered.endswith(".skeleton.bfres.zs"):
        return "skeleton"
    if lowered.endswith(".anim.bfres.zs"):
        return "animation"
    return "model"


def model_paths(entries: dict[str, object]) -> tuple[str, ...]:
    """Importable model resources, including the mesh-less animation rigs.

    `<Name>_Animation.Skeleton.bfres.zs` holds only the FSKL that
    `<Name>_Animation.anim.bfres.zs` was authored against; it imports as a
    bare armature.
    """
    return tuple(
        sorted(
            path
            for path in entries
            if path.startswith("Model/")
            and path.lower().endswith(MODEL_SUFFIX)
            and classify_bfres(path) in ("model", "skeleton")
        )
    )


def animation_paths(entries: dict[str, object]) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path in entries
            if path.startswith("Model/")
            and path.lower().endswith(MODEL_SUFFIX)
            and classify_bfres(path) == "animation"
        )
    )


def build_texture_index(entries: dict[str, object]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for path in entries:
        name = PurePosixPath(path).name
        if not name.lower().endswith(TEXTURE_SUFFIX):
            continue
        grouped.setdefault(name[:-len(TEXTURE_SUFFIX)], []).append(path)
    return {name: tuple(sorted(paths)) for name, paths in grouped.items()}


def texture_resource_path(
    texture_name: str,
    texture_index: dict[str, tuple[str, ...]],
) -> str | None:
    paths = texture_index.get(texture_name)
    if not paths:
        return None
    return min(paths, key=lambda path: (not path.startswith("Tex/"), len(path), path))


def referenced_texture_paths(model_data: bytes, texture_index: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    references: set[str] = set()
    for match in ASCII_STRING.finditer(model_data):
        value = match.group().decode("ascii")
        candidate = value.rsplit("/", 1)[-1]
        if candidate.lower().endswith(".bntx"):
            candidate = candidate[:-5]
        if candidate in texture_index:
            references.add(candidate)

    selected: list[str] = []
    for reference in references:
        selected.append(texture_resource_path(reference, texture_index))
    return tuple(sorted(selected))


class ResourceDecoder:
    """Decode .zs members using dictionaries stored in Pack/ZsDic.pack.zs."""

    def __init__(self, archive: NupArchive, package: NupPackage | None = None):
        self.archive = archive
        self.package = package or archive.default_package()
        self._dictionaries: dict[int, zstandard.ZstdCompressionDict] | None = None

    def _decode_frame(
        self,
        raw: bytes,
        *,
        dictionaries: dict[int, zstandard.ZstdCompressionDict] | None,
        source: str,
    ) -> bytes:
        if raw[:4] != ZSTD_MAGIC:
            raise ResourceError(f"{source} is not a zstd frame")
        try:
            frame = zstandard.get_frame_parameters(raw)
        except zstandard.ZstdError as error:
            raise ResourceError(f"{source} has an invalid zstd header: {error}") from error
        content_size = frame.content_size
        unknown = {
            getattr(zstandard, "CONTENTSIZE_UNKNOWN", -1),
            getattr(zstandard, "CONTENTSIZE_ERROR", -2),
        }
        if content_size in unknown or content_size < 0:
            raise ResourceError(f"{source} does not declare its decompressed size")
        if content_size > MAX_RESOURCE_SIZE:
            raise ResourceError(f"{source} exceeds the {MAX_RESOURCE_SIZE:,}-byte safety limit")

        dictionary = None
        if frame.dict_id:
            if dictionaries is None or frame.dict_id not in dictionaries:
                raise ResourceError(f"{source} needs unavailable zstd dictionary {frame.dict_id:#x}")
            dictionary = dictionaries[frame.dict_id]
        try:
            data = zstandard.ZstdDecompressor(dict_data=dictionary).decompress(
                raw, max_output_size=content_size
            )
        except zstandard.ZstdError as error:
            raise ResourceError(f"failed to decompress {source}: {error}") from error
        if len(data) != content_size:
            raise ResourceError(f"{source} decompressed to {len(data):,} bytes, expected {content_size:,}")
        return data

    def dictionaries(self) -> dict[int, zstandard.ZstdCompressionDict]:
        if self._dictionaries is not None:
            return self._dictionaries
        try:
            raw = self.archive.read_path(DICTIONARY_PATH, self.package)
        except NupError as error:
            raise ResourceError(f"cannot read {DICTIONARY_PATH}: {error}") from error
        pack = self._decode_frame(raw, dictionaries=None, source=DICTIONARY_PATH)
        dictionaries: dict[int, zstandard.ZstdCompressionDict] = {}
        for name, member in read_sarc(pack).items():
            try:
                dictionary = zstandard.ZstdCompressionDict(member)
                dictionary_id = dictionary.dict_id()
            except zstandard.ZstdError as error:
                raise ResourceError(f"invalid zstd dictionary {name!r}: {error}") from error
            if dictionary_id in dictionaries:
                raise ResourceError(f"duplicate zstd dictionary id {dictionary_id:#x}")
            dictionaries[dictionary_id] = dictionary
        if not dictionaries:
            raise ResourceError(f"{DICTIONARY_PATH} contains no zstd dictionaries")
        self._dictionaries = dictionaries
        return dictionaries

    def decode(self, path: str, expected_magic: bytes | None = None) -> bytes:
        try:
            raw = self.archive.read_path(path, self.package)
        except NupError as error:
            raise ResourceError(str(error)) from error
        data = self._decode_frame(raw, dictionaries=self.dictionaries(), source=path) if raw[:4] == ZSTD_MAGIC else raw
        if expected_magic is not None and data[:len(expected_magic)] != expected_magic:
            raise ResourceError(f"{path} does not contain expected {expected_magic!r} data")
        return data
