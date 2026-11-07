"""TerrainArc scene, height, and material-map decoding."""

from __future__ import annotations

import math
import re
import struct
import sys
from array import array
from dataclasses import dataclass
from pathlib import PurePosixPath

import zstandard


TSCB_MAGIC = b"TSCB"
TSCB_VERSION = 0x10D
TARB_MAGIC = b"TARB"
TARB_VERSION = 1
HEIGHT_RESOURCE_TYPE = 0x0000
MATERIAL_RESOURCE_TYPE = 0x2000
HEIGHT_MAP_SIZE = 257
MATERIAL_MAP_SIZE = 256
BLOCK_HEIGHT = 16
MAX_TERRAIN_TYPES = 64
MAX_TERRAIN_MATERIALS = 4096
MAX_TERRAIN_AREAS = 1_000_000
MAX_AREA_FILES = 64
MAX_TERRAIN_RESOURCE_SIZE = 64 * 1024 * 1024
SAFE_TERRAIN_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class TerrainError(ValueError):
    """A TerrainArc scene or resource is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class TerrainResourceType:
    type_id: int
    extension: str


@dataclass(frozen=True, slots=True)
class TerrainMaterial:
    material_id: int
    uv_scale: tuple[float, float]
    color: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class TerrainFile:
    type_id: int
    extension: str
    path: str
    minimum: float
    maximum: float


@dataclass(frozen=True, slots=True)
class TerrainArea:
    name: str
    center_x: float
    center_z: float
    size: float
    files: tuple[TerrainFile, ...]

    @property
    def minimum_x(self) -> float:
        return self.center_x - self.size * 0.5

    @property
    def maximum_x(self) -> float:
        return self.center_x + self.size * 0.5

    @property
    def minimum_z(self) -> float:
        return self.center_z - self.size * 0.5

    @property
    def maximum_z(self) -> float:
        return self.center_z + self.size * 0.5

    def file(self, type_id: int) -> TerrainFile | None:
        return next((resource for resource in self.files if resource.type_id == type_id), None)


@dataclass(frozen=True, slots=True)
class TerrainScene:
    source_path: str
    world_scale: float
    height_scale: float
    resource_types: tuple[TerrainResourceType, ...]
    materials: tuple[TerrainMaterial, ...]
    areas: tuple[TerrainArea, ...]

    @property
    def height_areas(self) -> tuple[TerrainArea, ...]:
        return tuple(
            area for area in self.areas if area.file(HEIGHT_RESOURCE_TYPE) is not None
        )


@dataclass(frozen=True, slots=True)
class HeightMap:
    size: int
    values: array


@dataclass(frozen=True, slots=True)
class TerrainMaterialMap:
    size: int
    material_ids: bytes
    weights: bytes


class _Reader:
    def __init__(self, data: bytes, source: str):
        self.data = data
        self.source = source

    def require(self, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise TerrainError(f"{self.source} {label} is out of bounds")

    def u32(self, offset: int, label: str) -> int:
        self.require(offset, 4, label)
        return struct.unpack_from("<I", self.data, offset)[0]

    def i32(self, offset: int, label: str) -> int:
        self.require(offset, 4, label)
        return struct.unpack_from("<i", self.data, offset)[0]

    def f32(self, offset: int, label: str) -> float:
        self.require(offset, 4, label)
        value = struct.unpack_from("<f", self.data, offset)[0]
        if not math.isfinite(value):
            raise TerrainError(f"{self.source} {label} is not finite")
        return value

    def relative(self, field: int, label: str, *, optional: bool = False) -> int:
        relative = self.i32(field, label)
        if relative == 0 and optional:
            return 0
        target = field + relative
        self.require(target, 1, label)
        return target

    def string(self, offset: int, label: str) -> str:
        self.require(offset, 1, label)
        try:
            end = self.data.index(0, offset)
        except ValueError as error:
            raise TerrainError(f"{self.source} {label} is not terminated") from error
        try:
            value = self.data[offset:end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise TerrainError(f"{self.source} {label} is not UTF-8") from error
        if not value:
            raise TerrainError(f"{self.source} {label} is empty")
        return value


def _bounded_count(value: int, maximum: int, source: str, label: str) -> int:
    if value > maximum:
        raise TerrainError(f"{source} has {value:,} {label}, above the limit of {maximum:,}")
    return value


def _pointer_array(
    reader: _Reader,
    field: int,
    count: int,
    label: str,
) -> tuple[int, ...]:
    if count == 0:
        return ()
    table = reader.relative(field, f"{label} table")
    reader.require(table, count * 4, f"{label} table")
    return tuple(
        reader.relative(table + index * 4, f"{label} {index}")
        for index in range(count)
    )


def _safe_resource_name(value: str, source: str) -> str:
    if (
        not SAFE_TERRAIN_NAME.fullmatch(value)
        or value in (".", "..")
        or PurePosixPath(value).name != value
    ):
        raise TerrainError(f"{source} has unsafe terrain resource name {value!r}")
    return value


def _safe_extension(value: str, source: str) -> str:
    if (
        not value
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("."))
    ):
        raise TerrainError(f"{source} has unsafe terrain extension {value!r}")
    return value


def read_tscb(data: bytes, source_path: str) -> TerrainScene:
    """Read the bounds-checked TSCB subset needed for terrain geometry."""
    reader = _Reader(data, source_path)
    reader.require(0, 0x7C, "header")
    if data[:4] != TSCB_MAGIC:
        raise TerrainError(f"{source_path} is not a TSCB scene")
    version = reader.u32(0x04, "version")
    if version != TSCB_VERSION:
        raise TerrainError(
            f"{source_path} uses unsupported TSCB version {version:#x}"
        )

    world_scale = reader.f32(0x10, "world scale")
    height_scale = reader.f32(0x14, "height scale")
    if world_scale <= 0.0 or height_scale <= 0.0:
        raise TerrainError(f"{source_path} has non-positive terrain scales")

    type_count = _bounded_count(
        reader.u32(0x30, "resource type count"),
        MAX_TERRAIN_TYPES,
        source_path,
        "resource types",
    )
    material_count = _bounded_count(
        reader.u32(0x34, "material count"),
        MAX_TERRAIN_MATERIALS,
        source_path,
        "materials",
    )
    area_count = _bounded_count(
        reader.u32(0x38, "area count"),
        MAX_TERRAIN_AREAS,
        source_path,
        "areas",
    )

    type_records = _pointer_array(reader, 0x70, type_count, "resource type")
    resource_types: list[TerrainResourceType] = []
    seen_types: set[int] = set()
    for index, record in enumerate(type_records):
        reader.require(record, 0x68, f"resource type {index}")
        type_id = reader.u32(record, f"resource type {index} id")
        extension = _safe_extension(
            reader.string(
                reader.relative(record + 0x64, f"resource type {index} extension"),
                f"resource type {index} extension",
            ),
            source_path,
        )
        if type_id in seen_types:
            raise TerrainError(f"{source_path} has duplicate resource type {type_id:#x}")
        seen_types.add(type_id)
        resource_types.append(TerrainResourceType(type_id, extension))
    extensions = {resource.type_id: resource.extension for resource in resource_types}
    for required in (HEIGHT_RESOURCE_TYPE, MATERIAL_RESOURCE_TYPE):
        if required not in extensions:
            raise TerrainError(
                f"{source_path} has no terrain resource type {required:#x}"
            )

    material_records = _pointer_array(reader, 0x74, material_count, "material")
    materials: list[TerrainMaterial] = []
    seen_materials: set[int] = set()
    for index, record in enumerate(material_records):
        reader.require(record, 0x3C, f"material {index}")
        material_id = reader.u32(record, f"material {index} id")
        if material_id in seen_materials:
            raise TerrainError(
                f"{source_path} has duplicate terrain material {material_id}"
            )
        seen_materials.add(material_id)
        uv_scale = (
            reader.f32(record + 0x10, f"material {index} U scale"),
            reader.f32(record + 0x14, f"material {index} V scale"),
        )
        color = tuple(
            reader.f32(record + offset, f"material {index} color")
            for offset in (0x30, 0x34, 0x38)
        )
        if any(component < 0.0 or component > 1.0 for component in color):
            raise TerrainError(
                f"{source_path} terrain material {material_id} color is out of range"
            )
        materials.append(TerrainMaterial(material_id, uv_scale, color))

    resource_root = source_path[:-len(".tscb")] if source_path.endswith(".tscb") else source_path
    area_records = _pointer_array(reader, 0x78, area_count, "area")
    areas: list[TerrainArea] = []
    for index, record in enumerate(area_records):
        reader.require(record, 0x68, f"area {index}")
        center_x = reader.f32(record, f"area {index} center X")
        center_z = reader.f32(record + 0x04, f"area {index} center Z")
        size = reader.f32(record + 0x08, f"area {index} size")
        if size <= 0.0:
            raise TerrainError(f"{source_path} area {index} has non-positive size")
        file_count = _bounded_count(
            reader.u32(record + 0x20, f"area {index} file count"),
            MAX_AREA_FILES,
            source_path,
            f"files in area {index}",
        )
        name = _safe_resource_name(
            reader.string(
                reader.relative(record + 0x60, f"area {index} name"),
                f"area {index} name",
            ),
            source_path,
        )
        reader.require(record + 0x68, file_count * 4, f"area {index} file pointers")
        files: list[TerrainFile] = []
        seen_files: set[int] = set()
        for file_index in range(file_count):
            info = reader.relative(
                record + 0x68 + file_index * 4,
                f"area {index} file {file_index}",
            )
            reader.require(info, 0x28, f"area {index} file {file_index}")
            type_id = reader.u32(info, f"area {index} file {file_index} type")
            extension = extensions.get(type_id)
            if extension is None:
                raise TerrainError(
                    f"{source_path} area {index} uses unknown resource type {type_id:#x}"
                )
            if type_id in seen_files:
                raise TerrainError(
                    f"{source_path} area {name!r} repeats resource type {type_id:#x}"
                )
            seen_files.add(type_id)
            minimum = reader.f32(info + 0x20, f"area {index} file minimum")
            maximum = reader.f32(info + 0x24, f"area {index} file maximum")
            if maximum < minimum:
                raise TerrainError(
                    f"{source_path} area {name!r} has an inverted resource range"
                )
            files.append(
                TerrainFile(
                    type_id=type_id,
                    extension=extension,
                    path=f"{resource_root}/{name}.{extension}",
                    minimum=minimum,
                    maximum=maximum,
                )
            )
        areas.append(
            TerrainArea(
                name=name,
                center_x=center_x,
                center_z=center_z,
                size=size,
                files=tuple(files),
            )
        )

    return TerrainScene(
        source_path=source_path,
        world_scale=world_scale,
        height_scale=height_scale,
        resource_types=tuple(resource_types),
        materials=tuple(materials),
        areas=tuple(areas),
    )


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def terrain_scene_paths(
    entries: dict[str, object],
    world: str,
    level_name: str,
) -> tuple[str, ...]:
    """Find TerrainArc scenes belonging to a main level, excluding side rooms."""
    normalized_world = _normalized_name(world)
    normalized_level = _normalized_name(level_name)
    if not normalized_world or not normalized_level.startswith(normalized_world):
        return ()
    candidates: list[str] = []
    for path in entries:
        if not path.startswith("TerrainArc/") or not path.endswith(".tscb"):
            continue
        stem = PurePosixPath(path).name[:-len(".tscb")]
        base = stem[:-len("_sub")] if stem.casefold().endswith("_sub") else stem
        normalized_stem = _normalized_name(base)
        if normalized_stem.startswith(normalized_world):
            candidates.append(path)
    return tuple(sorted(candidates, key=lambda path: (path.casefold().endswith("_sub.tscb"), path)))


def decode_tarb(data: bytes, source: str) -> bytes:
    """Decode a version-1 TARB wrapper containing one Zstandard frame."""
    if len(data) < 20 or data[:4] != TARB_MAGIC:
        raise TerrainError(f"{source} is not a TARB resource")
    version, flags, header_size, output_size, compressed_size = struct.unpack_from(
        "<IHHII", data, 4
    )
    if version != TARB_VERSION or flags != 1 or header_size != 20:
        raise TerrainError(
            f"{source} uses unsupported TARB header "
            f"(version={version}, flags={flags}, size={header_size})"
        )
    if output_size > MAX_TERRAIN_RESOURCE_SIZE:
        raise TerrainError(
            f"{source} expands above the {MAX_TERRAIN_RESOURCE_SIZE:,}-byte limit"
        )
    if compressed_size != len(data) - header_size:
        raise TerrainError(f"{source} has an inconsistent TARB payload size")
    payload = data[header_size:]
    if payload[:4] != b"\x28\xb5\x2f\xfd":
        raise TerrainError(f"{source} TARB payload is not Zstandard")
    try:
        result = zstandard.ZstdDecompressor().decompress(
            payload,
            max_output_size=output_size,
        )
    except zstandard.ZstdError as error:
        raise TerrainError(f"cannot decompress {source}: {error}") from error
    if len(result) != output_size:
        raise TerrainError(
            f"{source} expands to {len(result):,} bytes, expected {output_size:,}"
        )
    return result


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _surface_size(
    width: int,
    height: int,
    bytes_per_pixel: int,
    block_height: int,
) -> int:
    pitch = _align_up(width * bytes_per_pixel, 64)
    rows = _align_up(height, block_height * 8)
    return pitch * rows


def _block_linear_address(
    x: int,
    y: int,
    storage_width: int,
    bytes_per_pixel: int,
    block_height: int,
) -> int:
    gobs_per_row = (storage_width * bytes_per_pixel + 63) // 64
    gob_address = (
        (y // (8 * block_height)) * 512 * block_height * gobs_per_row
        + (x * bytes_per_pixel // 64) * 512 * block_height
        + ((y % (8 * block_height)) // 8) * 512
    )
    byte_x = x * bytes_per_pixel
    return (
        gob_address
        + ((byte_x % 64) // 32) * 256
        + ((y % 8) // 2) * 64
        + ((byte_x % 32) // 16) * 32
        + (y % 2) * 16
        + byte_x % 16
    )


def _deswizzle(
    source: bytes,
    logical_width: int,
    logical_height: int,
    *,
    storage_width: int,
    bytes_per_pixel: int,
    block_height: int,
) -> bytes:
    output = bytearray(logical_width * logical_height * bytes_per_pixel)
    for y in range(logical_height):
        for x in range(logical_width):
            source_offset = _block_linear_address(
                x,
                y,
                storage_width,
                bytes_per_pixel,
                block_height,
            )
            destination = (y * logical_width + x) * bytes_per_pixel
            if source_offset + bytes_per_pixel > len(source):
                raise TerrainError("block-linear terrain surface is truncated")
            output[destination:destination + bytes_per_pixel] = source[
                source_offset:source_offset + bytes_per_pixel
            ]
    return bytes(output)


def read_height_map(data: bytes, source: str) -> HeightMap:
    """Decode a TARB-wrapped 257-by-257 float height surface."""
    inner = decode_tarb(data, source)
    reader = _Reader(inner, source)
    reader.require(0, 0x34, "height header")
    if reader.u32(0, "height format") != 0x01020401:
        raise TerrainError(f"{source} uses an unsupported height-map format")
    if (
        reader.u32(0x08, "height half-size") != 129
        or reader.u32(0x0C, "height size") != HEIGHT_MAP_SIZE
    ):
        raise TerrainError(f"{source} has unsupported height-map dimensions")
    stream = reader.relative(0x2C, "height surface")
    stream_size = reader.u32(0x30, "height surface size")
    expected_size = _surface_size(
        HEIGHT_MAP_SIZE,
        HEIGHT_MAP_SIZE,
        4,
        BLOCK_HEIGHT,
    )
    if stream_size != expected_size:
        raise TerrainError(
            f"{source} height surface is {stream_size:,} bytes, "
            f"expected {expected_size:,}"
        )
    reader.require(stream, stream_size, "height surface")
    linear = _deswizzle(
        inner[stream:stream + stream_size],
        HEIGHT_MAP_SIZE,
        HEIGHT_MAP_SIZE,
        storage_width=HEIGHT_MAP_SIZE,
        bytes_per_pixel=4,
        block_height=BLOCK_HEIGHT,
    )
    values = array("f")
    values.frombytes(linear)
    if sys.byteorder != "little":
        values.byteswap()
    if any(not math.isfinite(value) for value in values):
        raise TerrainError(f"{source} contains a non-finite terrain height")
    return HeightMap(HEIGHT_MAP_SIZE, values)


def read_material_map(data: bytes, source: str) -> TerrainMaterialMap:
    """Decode four material IDs and four blend weights per terrain cell."""
    inner = decode_tarb(data, source)
    reader = _Reader(inner, source)
    reader.require(0, 0x34, "material-map header")
    if reader.u32(0, "material-map format") != 0x02020401:
        raise TerrainError(f"{source} uses an unsupported material-map format")
    if (
        reader.u32(0x08, "material-map layers") != 1
        or reader.u32(0x0C, "material-map size") != MATERIAL_MAP_SIZE
    ):
        raise TerrainError(f"{source} has unsupported material-map dimensions")
    stream = reader.relative(0x2C, "material-map surface")
    stream_size = reader.u32(0x30, "material-map surface size")
    plane_size = _surface_size(
        HEIGHT_MAP_SIZE,
        HEIGHT_MAP_SIZE,
        4,
        BLOCK_HEIGHT,
    )
    if stream_size != plane_size * 2:
        raise TerrainError(
            f"{source} material-map surface is {stream_size:,} bytes, "
            f"expected {plane_size * 2:,}"
        )
    reader.require(stream, stream_size, "material-map surface")
    planes = []
    for index in range(2):
        start = stream + index * plane_size
        planes.append(
            _deswizzle(
                inner[start:start + plane_size],
                MATERIAL_MAP_SIZE,
                MATERIAL_MAP_SIZE,
                storage_width=HEIGHT_MAP_SIZE,
                bytes_per_pixel=4,
                block_height=BLOCK_HEIGHT,
            )
        )
    return TerrainMaterialMap(
        size=MATERIAL_MAP_SIZE,
        material_ids=planes[0],
        weights=planes[1],
    )


def validate_height_range(
    height_map: HeightMap,
    resource: TerrainFile,
    *,
    tolerance: float = 1.0 / 4096.0,
) -> None:
    """Ensure the decoded surface substantially agrees with its TSCB bounds."""
    populated = [value for value in height_map.values if value != 0.0]
    if not populated:
        return
    in_range = [
        value
        for value in populated
        if resource.minimum - tolerance <= value <= resource.maximum + tolerance
    ]
    if not in_range or len(in_range) < len(populated) * 0.9:
        raise TerrainError(
            f"{resource.path} has only {len(in_range):,} of {len(populated):,} "
            "nonzero height samples inside its TSCB range"
        )


def height_is_populated(
    value: float,
    resource: TerrainFile,
    *,
    tolerance: float = 1.0 / 4096.0,
) -> bool:
    """Identify populated samples; unused HGHT padding can contain nonzero values."""
    return (
        value != 0.0
        and resource.minimum - tolerance <= value <= resource.maximum + tolerance
    )
