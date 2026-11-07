"""Unit tests for the Blender importer's format-independent core."""

from __future__ import annotations

import hashlib
import math
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

import zstandard

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from oot_remake_importer.core.animation import (
    AnimationCurve,
    AnimationError,
    BONE_QUATERNION_ROTATION,
    BoneAnimation,
    CURVE_CUBIC,
    ROTATE_X,
    SCALE_X,
    SEGMENT_SCALE_COMPENSATE,
    SkeletalAnimation,
    TRANSLATE_X,
    _evaluate_skeleton,
    evaluate_world_matrices,
    glb_has_meshes,
    inject_skeletal_animation,
    prepare_skeleton_glb,
    read_model_skeletons,
    read_skeletal_animations,
    rotation_modes_compatible,
    sample_bone_poses,
    unsupported_animation_counts,
)
from oot_remake_importer.core.byml import BymlError, read_byml
from oot_remake_importer.core.converter import ConversionError, convert_bfres
from oot_remake_importer.core.levels import (
    LevelError,
    actor_pack_path,
    discover_levels,
    read_placement,
    resolve_actor_models,
)
from oot_remake_importer.core.nup import EXPECTED_TITLE_ID, NO_DIRECTORY, NupArchive
from oot_remake_importer.core.materials import (
    MaterialError,
    MaterialMetadata,
    ShaderParameter,
    classify_vertex_colors,
    describe_sampler_bindings,
    load_material_semantics,
    read_external_strings,
    read_materials,
    texture_coordinate_transform,
)
from oot_remake_importer.core.resources import (
    animation_paths,
    build_texture_index,
    model_paths,
    classify_bfres,
    read_sarc,
    referenced_texture_paths,
    texture_resource_path,
)
from oot_remake_importer.core.terrain import (
    TerrainError,
    height_is_populated,
    read_height_map,
    read_material_map,
    read_tscb,
    terrain_scene_paths,
    validate_height_range,
)


def align(value: int, alignment: int = 16) -> int:
    return (value + alignment - 1) & -alignment


def make_nup(path: Path) -> dict[str, bytes]:
    payloads = {
        "plain.bin": b"plain member",
        "compressed.bin": b"compressed member" * 100,
    }
    stored = {
        "plain.bin": payloads["plain.bin"],
        "compressed.bin": zstandard.ZstdCompressor().compress(payloads["compressed.bin"]),
    }
    codecs = {"plain.bin": 0, "compressed.bin": 1}
    names = b"plain.bincompressed.bin"
    name_offsets = {"plain.bin": 0, "compressed.bin": len("plain.bin")}

    package_count = 1
    directory_count = 1
    file_count = 2
    records_end = 0x30 + package_count * 0x40 + directory_count * 0x18 + file_count * 0x50
    children = struct.pack("<2I", 1, 2)
    pool_offset = records_end + len(children)
    toc_size = pool_offset + len(names)
    toc_offset = 0x80
    blob_offset = align(toc_offset + toc_size)

    offsets: dict[str, int] = {}
    cursor = blob_offset
    for name in payloads:
        cursor = align(cursor)
        offsets[name] = cursor
        cursor += len(stored[name])
    file_size = cursor

    toc = bytearray(toc_size)
    struct.pack_into(
        "<IIQQQQ",
        toc,
        0,
        0x01434F54,
        package_count,
        directory_count,
        file_count,
        pool_offset,
        len(names),
    )
    position = 0x30
    struct.pack_into(
        "<QIIIIBB",
        toc,
        position,
        int(EXPECTED_TITLE_ID, 16),
        0,
        NO_DIRECTORY,
        0,
        131072,
        1,
        1,
    )
    position += 0x40
    struct.pack_into("<IIII", toc, position, 0, 0, 0, 2)
    position += 0x18
    for name in payloads:
        struct.pack_into(
            "<IIQQQ",
            toc,
            position,
            name_offsets[name],
            len(name),
            len(payloads[name]),
            offsets[name],
            len(stored[name]),
        )
        toc[position + 0x20:position + 0x40] = hashlib.sha256(payloads[name]).digest()
        toc[position + 0x40] = codecs[name]
        position += 0x50
    toc[records_end:pool_offset] = children
    toc[pool_offset:] = names

    superblock = bytearray(0x80)
    superblock[:4] = b"NUP\x01"
    struct.pack_into("<HHQQ", superblock, 4, 0x80, 1, toc_offset, toc_size)
    superblock[0x18:0x38] = hashlib.sha256(toc).digest()
    struct.pack_into("<QQII", superblock, 0x38, blob_offset, file_size, package_count, 0)

    image = bytearray(file_size)
    image[:0x80] = superblock
    image[toc_offset:toc_offset + toc_size] = toc
    for name in payloads:
        image[offsets[name]:offsets[name] + len(stored[name])] = stored[name]
    path.write_bytes(image)
    return payloads


def make_sarc(name: str, payload: bytes) -> bytes:
    encoded_name = name.encode("utf-8") + b"\0"
    name_table = encoded_name + bytes((-len(encoded_name)) % 4)
    header_size = 0x14
    sfat_size = 0x0C + 0x10
    sfnt_offset = header_size + sfat_size
    data_start = align(sfnt_offset + 8 + len(name_table), 4)
    file_size = data_start + len(payload)
    data = bytearray(file_size)
    data[:4] = b"SARC"
    struct.pack_into("<H", data, 4, header_size)
    data[6:8] = b"\xff\xfe"
    struct.pack_into("<II", data, 8, file_size, data_start)
    data[header_size:header_size + 4] = b"SFAT"
    struct.pack_into("<HHI", data, header_size + 4, 0x0C, 1, 0x65)
    struct.pack_into("<4I", data, header_size + 0x0C, 0, 0x01000000, 0, len(payload))
    data[sfnt_offset:sfnt_offset + 4] = b"SFNT"
    struct.pack_into("<HH", data, sfnt_offset + 4, 8, 0)
    data[sfnt_offset + 8:sfnt_offset + 8 + len(name_table)] = name_table
    data[data_start:] = payload
    return bytes(data)


def make_byml(document: object) -> bytes:
    keys: set[str] = set()
    strings: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                keys.add(key)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str):
            strings.add(value)

    collect(document)
    key_list = sorted(keys)
    string_list = sorted(strings)
    key_indices = {value: index for index, value in enumerate(key_list)}
    string_indices = {value: index for index, value in enumerate(string_list)}
    data = bytearray(16)

    def append_string_table(values: list[str]) -> int:
        while len(data) % 4:
            data.append(0)
        offset = len(data)
        data.extend(bytes((0xC2,)) + len(values).to_bytes(3, "little"))
        offsets = len(data)
        data.extend(bytes(4 * len(values)))
        for index, value in enumerate(values):
            struct.pack_into("<I", data, offsets + index * 4, len(data) - offset)
            data.extend(value.encode("utf-8") + b"\0")
        return offset

    key_table = append_string_table(key_list)
    string_table = append_string_table(string_list) if string_list else 0

    def append_u64(value: int, signed: bool) -> int:
        while len(data) % 8:
            data.append(0)
        offset = len(data)
        data.extend(value.to_bytes(8, "little", signed=signed))
        return offset

    def encode_value(value: object) -> tuple[int, int]:
        if value is None:
            return 0xFF, 0
        if isinstance(value, bool):
            return 0xD0, int(value)
        if isinstance(value, str):
            return 0xA0, string_indices[value]
        if isinstance(value, float):
            return 0xD2, struct.unpack("<I", struct.pack("<f", value))[0]
        if isinstance(value, int):
            if 0 <= value <= 0xFFFFFFFF:
                return 0xD3, value
            if -(1 << 31) <= value < (1 << 31):
                return 0xD1, value & 0xFFFFFFFF
            return (0xD4, append_u64(value, True)) if value < 0 else (0xD5, append_u64(value, False))
        if isinstance(value, (list, dict)):
            return (0xC0 if isinstance(value, list) else 0xC1), append_node(value)
        raise TypeError(type(value))

    def append_node(value: object) -> int:
        while len(data) % 4:
            data.append(0)
        offset = len(data)
        if isinstance(value, list):
            count = len(value)
            data.extend(bytes((0xC0,)) + count.to_bytes(3, "little"))
            types = len(data)
            data.extend(bytes(count))
            while len(data) % 4:
                data.append(0)
            values = len(data)
            data.extend(bytes(4 * count))
            for index, child in enumerate(value):
                node_type, encoded = encode_value(child)
                data[types + index] = node_type
                struct.pack_into("<I", data, values + index * 4, encoded)
            return offset
        if isinstance(value, dict):
            items = sorted(value.items())
            data.extend(bytes((0xC1,)) + len(items).to_bytes(3, "little"))
            entries = len(data)
            data.extend(bytes(8 * len(items)))
            for index, (key, child) in enumerate(items):
                entry = entries + index * 8
                data[entry:entry + 3] = key_indices[key].to_bytes(3, "little")
                node_type, encoded = encode_value(child)
                data[entry + 3] = node_type
                struct.pack_into("<I", data, entry + 4, encoded)
            return offset
        raise TypeError(type(value))

    root = append_node(document)
    data[:4] = b"YB\x07\0"
    struct.pack_into("<III", data, 4, key_table, string_table, root)
    return bytes(data)


def _relative_pointer(data: bytearray, field: int, target: int) -> None:
    struct.pack_into("<i", data, field, target - field)


def make_tscb() -> bytes:
    data = bytearray(0x500)
    data[:4] = b"TSCB"
    struct.pack_into("<I", data, 0x04, 0x10D)
    struct.pack_into("<2f", data, 0x10, 500.0, 800.0)
    struct.pack_into("<3I", data, 0x30, 2, 1, 1)

    type_table = 0x80
    type_records = (0x100, 0x180)
    _relative_pointer(data, 0x70, type_table)
    for index, record in enumerate(type_records):
        _relative_pointer(data, type_table + index * 4, record)
    strings = {"hght": 0x400, "mate": 0x408, "1200000003": 0x410}
    for value, offset in strings.items():
        data[offset:offset + len(value) + 1] = value.encode("ascii") + b"\0"
    for record, type_id, extension in zip(
        type_records,
        (0x0000, 0x2000),
        ("hght", "mate"),
    ):
        struct.pack_into("<I", data, record, type_id)
        _relative_pointer(data, record + 0x64, strings[extension])

    material_table = 0x200
    material_record = 0x220
    _relative_pointer(data, 0x74, material_table)
    _relative_pointer(data, material_table, material_record)
    struct.pack_into("<I", data, material_record, 3)
    struct.pack_into("<2f", data, material_record + 0x10, 0.25, 0.25)
    struct.pack_into("<3f", data, material_record + 0x30, 0.1, 0.2, 0.3)

    area_table = 0x260
    area_record = 0x280
    _relative_pointer(data, 0x78, area_table)
    _relative_pointer(data, area_table, area_record)
    struct.pack_into("<3f", data, area_record, -0.25, 0.5, 0.5)
    struct.pack_into("<I", data, area_record + 0x20, 2)
    _relative_pointer(data, area_record + 0x60, strings["1200000003"])
    for index, (info, type_id) in enumerate(((0x300, 0x0000), (0x340, 0x2000))):
        _relative_pointer(data, area_record + 0x68 + index * 4, info)
        struct.pack_into("<I", data, info, type_id)
        struct.pack_into("<2f", data, info + 0x20, 0.125, 0.25)
    return bytes(data)


def _terrain_block_address(x: int, y: int) -> int:
    bytes_per_pixel = 4
    block_height = 16
    storage_width = 257
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


def make_tarb(inner: bytes) -> bytes:
    payload = zstandard.ZstdCompressor().compress(inner)
    return struct.pack(
        "<4sIHHII",
        b"TARB",
        1,
        1,
        20,
        len(inner),
        len(payload),
    ) + payload


def make_height_resource() -> bytes:
    surface_offset = 0x42074
    surface_size = 0x66000
    data = bytearray(surface_offset + surface_size)
    struct.pack_into("<I", data, 0, 0x01020401)
    struct.pack_into("<2I", data, 0x08, 129, 257)
    _relative_pointer(data, 0x2C, surface_offset)
    struct.pack_into("<I", data, 0x30, surface_size)
    for y in range(257):
        for x in range(257):
            value = 0.125 + (x + y) / 512.0 * 0.125
            struct.pack_into(
                "<f",
                data,
                surface_offset + _terrain_block_address(x, y),
                value,
            )
    return make_tarb(bytes(data))


def make_material_map_resource() -> bytes:
    surface_offset = 0x34
    plane_size = 0x66000
    data = bytearray(surface_offset + plane_size * 2)
    struct.pack_into("<I", data, 0, 0x02020401)
    struct.pack_into("<2I", data, 0x08, 1, 256)
    _relative_pointer(data, 0x2C, surface_offset)
    struct.pack_into("<I", data, 0x30, plane_size * 2)
    for y in range(256):
        for x in range(256):
            offset = _terrain_block_address(x, y)
            struct.pack_into("<4B", data, surface_offset + offset, 3, 7, 127, 127)
            struct.pack_into(
                "<4B",
                data,
                surface_offset + plane_size + offset,
                192,
                63,
                0,
                0,
            )
    return make_tarb(bytes(data))


def put_string(data: bytearray, offset: int, value: str) -> None:
    encoded = value.encode("utf-8")
    struct.pack_into("<H", data, offset, len(encoded))
    data[offset + 2:offset + 2 + len(encoded)] = encoded


def put_dictionary(data: bytearray, offset: int, keys: tuple[int, ...]) -> None:
    struct.pack_into("<i", data, offset + 4, len(keys))
    for index, key in enumerate(keys):
        struct.pack_into("<Q", data, offset + 8 + (index + 1) * 16 + 8, key)


MATERIAL_HASHES = {
    "_a0": 0xA100000000000001,
    "_tgt0": 0xA100000000000002,
    "ns_param_color0": 0xA100000000000003,
}


def make_external_strings() -> bytes:
    data = bytearray(0x300)
    data[:8] = b"FRES    "
    data[10] = 10
    hash_array = 0x100
    dictionary = 0x140
    struct.pack_into("<QQ", data, 0xB8, hash_array, dictionary)
    string_offsets = (0x200, 0x220, 0x240)
    for index, (name, hash_value) in enumerate(MATERIAL_HASHES.items()):
        struct.pack_into("<Q", data, hash_array + index * 8, hash_value)
        put_string(data, string_offsets[index], name)
    put_dictionary(data, dictionary, string_offsets)
    return bytes(data)


def make_material_bfres() -> bytes:
    data = bytearray(0x1000)
    data[:8] = b"FRES    "
    data[10] = 10
    model_dictionary = 0x100
    model = 0x200
    material = 0x300
    shader_info = 0x700
    shader_assign = 0x800
    sampler_dictionary = 0x900
    parameter_dictionary = 0x980
    texture_names = 0xA00
    parameter_list = 0xB00
    parameter_source = 0xC00
    strings = {
        "material": 0xD00,
        "shader_archive": 0xD40,
        "shader_model": 0xD80,
        "albedo": 0xDC0,
        "target": 0xE00,
    }

    struct.pack_into("<QQ", data, 0x28, model, model_dictionary)
    put_dictionary(data, model_dictionary, (strings["material"],))
    data[model:model + 4] = b"FMDL"
    struct.pack_into("<QH", data, model + 0x38, material, 0)
    struct.pack_into("<H", data, model + 0x6C, 1)

    data[material:material + 4] = b"FMAT"
    struct.pack_into("<Q", data, material + 8, strings["material"])
    struct.pack_into("<Q", data, material + 0x10, shader_info)
    struct.pack_into("<Q", data, material + 0x20, texture_names)
    struct.pack_into("<Q", data, material + 0x38, sampler_dictionary)
    struct.pack_into("<Q", data, material + 0x58, parameter_source)
    data[material + 0xA2] = 2
    data[material + 0xA3] = 2

    struct.pack_into("<QQ", data, texture_names, strings["albedo"], strings["target"])
    put_dictionary(
        data,
        sampler_dictionary,
        (MATERIAL_HASHES["_a0"], MATERIAL_HASHES["_tgt0"]),
    )
    struct.pack_into("<Q", data, shader_info, shader_assign)
    struct.pack_into("<QQ", data, shader_assign, strings["shader_archive"], strings["shader_model"])
    struct.pack_into("<QQ", data, shader_assign + 0x20, parameter_list, parameter_dictionary)
    put_dictionary(data, parameter_dictionary, (MATERIAL_HASHES["ns_param_color0"],))
    struct.pack_into("<QHH", data, parameter_list + 8, MATERIAL_HASHES["ns_param_color0"], 0, 15)
    struct.pack_into("<4f", data, parameter_source, 1.0, 0.35, 0.36, 1.0)

    put_string(data, strings["material"], "Mt_Face")
    put_string(data, strings["shader_archive"], "Model.Product")
    put_string(data, strings["shader_model"], "Model")
    put_string(data, strings["albedo"], "Mt_Face_Alb")
    put_string(data, strings["target"], "Mt_Face_Tgt")
    return bytes(data)


def make_animation_bfres(
    *,
    curve_flags: int = 0x4805,
    animation_frame_count: int = 2,
    curve_start: float = 0.0,
    curve_end: float = 2.0,
    interval_scale: float = 0.0,
    value_scale: float = 0.1,
    value_addend: float = 1.0,
    tangent_scale: float = 0.01,
    tangent_addend: float = 0.0,
    frame_data: bytes = struct.pack("<2h", 0, 64),
    key_data: bytes = struct.pack("<HHHH", 0, 50, 10, 50),
    animation_flags: int = 0x1204,
    bone_flags: int = 0x2038,
    curve_target: int = TRANSLATE_X,
    tangent_prefix: bool = True,
) -> bytes:
    data = bytearray(0x800)
    animation_array = 0x100
    animation_dictionary = 0x180
    bone_array = 0x200
    base_data = 0x250
    curve_array = 0x280
    frames = 0x380
    keys = 0x390
    animation_name = 0x700
    bone_name = 0x720

    data[:8] = b"FRES    "
    data[8:12] = bytes((0, 4, 10, 0))
    struct.pack_into("<QQ", data, 0x58, animation_array, animation_dictionary)

    struct.pack_into("<Ii", data, animation_dictionary, 0, 1)
    struct.pack_into("<Q", data, animation_dictionary + 8 + 16 + 8, animation_name)

    data[animation_array:animation_array + 4] = b"FSKA"
    struct.pack_into("<I", data, animation_array + 4, animation_flags)
    struct.pack_into("<Q", data, animation_array + 8, animation_name)
    struct.pack_into("<Q", data, animation_array + 40, bone_array)
    struct.pack_into(
        "<iiiHH",
        data,
        animation_array + 64,
        animation_frame_count,
        1,
        0,
        1,
        0,
    )

    struct.pack_into("<QQQ", data, bone_array, bone_name, curve_array, base_data)
    struct.pack_into("<I4Bi", data, bone_array + 40, bone_flags, 3, 6, 1, 7, 0)
    struct.pack_into("<3f4f3f", data, base_data, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0)

    struct.pack_into(
        "<QQHHI",
        data,
        curve_array,
        frames,
        keys,
        curve_flags,
        2,
        curve_target,
    )
    struct.pack_into(
        "<6f",
        data,
        curve_array + 24,
        curve_start,
        curve_end,
        interval_scale,
        value_scale,
        value_addend,
        0.0,
    )
    data[frames:frames + len(frame_data)] = frame_data
    if tangent_prefix:
        struct.pack_into("<2f", data, keys, tangent_scale, tangent_addend)
        keys += 8
    data[keys:keys + len(key_data)] = key_data
    put_string(data, animation_name, "Walk")
    put_string(data, bone_name, "Root")
    return bytes(data)


def make_model_bfres(*, include_child: bool = False, compensate_child: bool = True) -> bytes:
    data = bytearray(0x800)
    model_array = 0x100
    model_dictionary = 0x200
    skeleton = 0x300
    bone_array = 0x380
    model_name = 0x700
    root_name = 0x720
    child_name = 0x740

    data[:8] = b"FRES    "
    data[8:12] = bytes((0, 4, 10, 0))
    struct.pack_into("<QQ", data, 0x28, model_array, model_dictionary)
    struct.pack_into("<Ii", data, model_dictionary, 0, 1)

    data[model_array:model_array + 4] = b"FMDL"
    struct.pack_into("<Q", data, model_array + 8, model_name)
    struct.pack_into("<Q", data, model_array + 0x18, skeleton)

    data[skeleton:skeleton + 4] = b"FSKL"
    struct.pack_into("<I", data, skeleton + 4, 0x1200)
    struct.pack_into("<Q", data, skeleton + 0x10, bone_array)
    struct.pack_into("<H", data, skeleton + 0x38, 2 if include_child else 1)

    struct.pack_into("<Q", data, bone_array, root_name)
    struct.pack_into("<HH", data, bone_array + 0x20, 0, 0xFFFF)
    struct.pack_into("<I", data, bone_array + 0x2C, 0)
    struct.pack_into("<3f4f3f", data, bone_array + 0x30, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0)
    if include_child:
        child = bone_array + 0x58
        struct.pack_into("<Q", data, child, child_name)
        struct.pack_into("<HH", data, child + 0x20, 1, 0)
        struct.pack_into(
            "<I",
            data,
            child + 0x2C,
            SEGMENT_SCALE_COMPENSATE if compensate_child else 0,
        )
        struct.pack_into(
            "<3f4f3f",
            data,
            child + 0x30,
            1, 1, 1,
            0, 0, 0, 1,
            1, 1, 0,
        )

    put_string(data, model_name, "Hero")
    put_string(data, root_name, "Root")
    put_string(data, child_name, "Child")
    return bytes(data)


def make_scale_animation() -> SkeletalAnimation:
    curve = AnimationCurve(
        target=SCALE_X,
        curve_type=CURVE_CUBIC,
        frames=(0.0, 2.0),
        keys=(
            (1.0, 1.0, 0.0, 0.0),
            (2.0, 0.0, 0.0, 0.0),
        ),
        scale=1.0,
        offset=0.0,
    )
    return SkeletalAnimation(
        name="Scale",
        frame_count=2,
        loop=False,
        baked=False,
        scale_mode="maya",
        rotation_mode="euler_xyz",
        bones=(
            BoneAnimation(
                name="Root",
                flags=0,
                base_scale=(1.0, 1.0, 1.0),
                base_rotation=None,
                base_translation=None,
                curves=(curve,),
            ),
        ),
    )


def make_glb(document: dict, binary: bytes | None) -> bytes:
    json_data = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_data += b" " * (-len(json_data) % 4)
    chunks = struct.pack("<I4s", len(json_data), b"JSON") + json_data
    if binary is not None:
        binary += b"\0" * (-len(binary) % 4)
        chunks += struct.pack("<I4s", len(binary), b"BIN\0") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def read_glb(data: bytes) -> tuple[dict, bytes]:
    json_size = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20:20 + json_size].rstrip(b" \0").decode("utf-8"))
    binary_header = 20 + json_size
    binary_size, binary_type = struct.unpack_from("<I4s", data, binary_header)
    if binary_type != b"BIN\0":
        raise AssertionError("test GLB has no binary chunk")
    binary_start = binary_header + 8
    return document, data[binary_start:binary_start + binary_size]


def accessor_floats(document: dict, binary: bytes, accessor_index: int) -> tuple[float, ...]:
    accessor = document["accessors"][accessor_index]
    view = document["bufferViews"][accessor["bufferView"]]
    widths = {"SCALAR": 1, "VEC3": 3, "VEC4": 4, "MAT4": 16}
    count = accessor["count"] * widths[accessor["type"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    return struct.unpack_from("<" + "f" * count, binary, offset)


class NupArchiveTests(unittest.TestCase):
    def test_reads_raw_and_compressed_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.nup"
            payloads = make_nup(path)
            archive = NupArchive(path)
            package = archive.default_package()
            self.assertEqual(package.title_id, EXPECTED_TITLE_ID)
            self.assertEqual(set(archive.package_files(package)), set(payloads))
            for name, expected in payloads.items():
                self.assertEqual(archive.read_path(name, package), expected)


class BymlTests(unittest.TestCase):
    def test_reads_nested_placement_values(self):
        expected = {
            "Actors": [
                {
                    "Gyaml": "Land_Test_A_01",
                    "Hash": 0xFEDCBA9876543210,
                    "Rotate": [0.0, -1.25, 3.0],
                    "Scale": [1.0, 2.0, 1.0],
                    "Translate": [-4.5, 120.25, 8.0],
                }
            ],
            "Enabled": True,
            "Optional": None,
            "Signed": -42,
        }

        actual = read_byml(make_byml(expected))

        self.assertEqual(actual, expected)

    def test_rejects_unsupported_version(self):
        data = bytearray(make_byml({"Value": 1}))
        struct.pack_into("<H", data, 2, 6)

        with self.assertRaisesRegex(BymlError, "unsupported BYML version"):
            read_byml(bytes(data))

    def test_rejects_out_of_bounds_root(self):
        data = bytearray(make_byml({"Value": 1}))
        struct.pack_into("<I", data, 12, len(data) + 4)

        with self.assertRaisesRegex(BymlError, "container is out of bounds"):
            read_byml(bytes(data))


class LevelTests(unittest.TestCase):
    def test_discovers_static_and_dynamic_level_pairs(self):
        entries = {
            "Banc/KokiriForest/KokiriForest_Child_Static.bcett.byml.zs": object(),
            "Banc/KokiriForest/KokiriForest_Child_Dynamic.bcett.byml.zs": object(),
            "Banc/WaterTemple/WaterTemple_Static.bcett.byml.zs": object(),
            "Banc/BancBasedResource/MapParam.byml.zs": object(),
        }

        levels = discover_levels(entries)

        self.assertEqual([level.identifier for level in levels], ["KokiriForest/KokiriForest_Child", "WaterTemple/WaterTemple"])
        self.assertEqual(
            levels[0].paths,
            (
                "Banc/KokiriForest/KokiriForest_Child_Static.bcett.byml.zs",
                "Banc/KokiriForest/KokiriForest_Child_Dynamic.bcett.byml.zs",
            ),
        )
        self.assertIsNone(levels[1].dynamic_path)

    def test_reads_placement_transforms_and_skips_missing_translation(self):
        source = "Banc/Test/Test_Static.bcett.byml.zs"
        data = make_byml(
            {
                "Actors": [
                    {
                        "Gyaml": "Land_Test_A_01",
                        "Hash": 0xFEDCBA9876543210,
                        "SRTHash": 123,
                        "Translate": [1.0, 2.0, 3.0],
                        "Rotate": [0.0, 0.5, 0.0],
                        "Scale": [2.0, 2.0, 2.0],
                    },
                    {"Gyaml": "LogicOnly", "Hash": 1},
                ]
            }
        )

        placement = read_placement(data, source, dynamic=False)

        self.assertEqual(len(placement.actors), 1)
        self.assertEqual(placement.skipped_without_transform, 1)
        self.assertEqual(placement.actors[0].translate, (1.0, 2.0, 3.0))
        self.assertEqual(placement.actors[0].scale, (2.0, 2.0, 2.0))
        self.assertFalse(placement.actors[0].dynamic)

    def test_rejects_invalid_placement_vector(self):
        data = make_byml(
            {
                "Actors": [
                    {
                        "Gyaml": "Land_Test_A_01",
                        "Hash": 1,
                        "Translate": [1.0, 2.0],
                    }
                ]
            }
        )

        with self.assertRaisesRegex(LevelError, "three-component vector"):
            read_placement(data, "Banc/Test.bcett.byml.zs", dynamic=True)

    def test_resolves_primary_and_submodel_resources(self):
        gyaml = "Obj_Test_A_01"
        actor_param = f"Actor/{gyaml}.engine__actor__ActorParam.bgyml"
        model_info = f"Component/ModelInfo/{gyaml}.engine__component__ModelInfo.bgyml"
        members = {
            actor_param: make_byml({"Components": {"ModelInfoRef": f"?{model_info}"}}),
            model_info: make_byml(
                {
                    "FmdbName": "Obj_Test_A_01",
                    "ModelProjectName": "Obj_Test_A",
                    "SubModels": [
                        {
                            "FmdbName": "Obj_Test_Part_A_01",
                            "ModelProjectName": "Obj_Test_Part_A",
                        }
                    ],
                }
            ),
        }
        entries = {
            "Model/Obj_Test_A.Obj_Test_A_01.bfres.zs": object(),
            "Model/Obj_Test_Part_A.Obj_Test_Part_A_01.bfres.zs": object(),
        }

        resolution = resolve_actor_models(gyaml, members, entries)

        self.assertIsNone(resolution.reason)
        self.assertEqual(
            [model.resource_path for model in resolution.models],
            [
                "Model/Obj_Test_A.Obj_Test_A_01.bfres.zs",
                "Model/Obj_Test_Part_A.Obj_Test_Part_A_01.bfres.zs",
            ],
        )

    def test_classifies_actor_without_model_component(self):
        gyaml = "Area"
        members = {
            f"Actor/{gyaml}.engine__actor__ActorParam.bgyml": make_byml(
                {"Components": {"PhysicsRef": "?Component/Physics/Area.bgyml"}}
            )
        }

        resolution = resolve_actor_models(gyaml, members, {})

        self.assertEqual(resolution.models, ())
        self.assertEqual(resolution.reason, "actor has no model component")

    def test_rejects_unsafe_actor_pack_name(self):
        with self.assertRaisesRegex(LevelError, "unsafe actor name"):
            actor_pack_path("../Actor")


class TerrainTests(unittest.TestCase):
    def test_reads_scene_areas_materials_and_resource_paths(self):
        scene = read_tscb(
            make_tscb(),
            "TerrainArc/TestWorld_01.tscb",
        )

        self.assertEqual(scene.world_scale, 500.0)
        self.assertEqual(scene.height_scale, 800.0)
        self.assertEqual(
            [(resource.type_id, resource.extension) for resource in scene.resource_types],
            [(0x0000, "hght"), (0x2000, "mate")],
        )
        self.assertEqual(scene.materials[0].material_id, 3)
        for actual, expected in zip(scene.materials[0].color, (0.1, 0.2, 0.3)):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(len(scene.height_areas), 1)
        area = scene.height_areas[0]
        self.assertEqual((area.center_x, area.center_z, area.size), (-0.25, 0.5, 0.5))
        self.assertEqual(
            [resource.path for resource in area.files],
            [
                "TerrainArc/TestWorld_01/1200000003.hght",
                "TerrainArc/TestWorld_01/1200000003.mate",
            ],
        )

    def test_rejects_out_of_bounds_scene_pointer(self):
        data = bytearray(make_tscb())
        struct.pack_into("<i", data, 0x78, 0x7FFFFFFF)

        with self.assertRaisesRegex(TerrainError, "area table is out of bounds"):
            read_tscb(bytes(data), "TerrainArc/Broken.tscb")

    def test_discovers_main_level_terrain_and_subscene(self):
        entries = {
            "TerrainArc/KokiriForest_01_sub.tscb": object(),
            "TerrainArc/KokiriForest_01.tscb": object(),
            "TerrainArc/HyruleField_02.tscb": object(),
        }

        self.assertEqual(
            terrain_scene_paths(entries, "KokiriForest", "KokiriForest_Child"),
            (
                "TerrainArc/KokiriForest_01.tscb",
                "TerrainArc/KokiriForest_01_sub.tscb",
            ),
        )
        self.assertEqual(
            terrain_scene_paths(
                entries,
                "KokiriForest",
                "Demo_ShootingLocation_Tapestry_ED",
            ),
            (),
        )

    def test_decodes_block_linear_height_surface(self):
        scene = read_tscb(make_tscb(), "TerrainArc/TestWorld_01.tscb")
        resource = scene.height_areas[0].file(0x0000)
        height_map = read_height_map(
            make_height_resource(),
            "TerrainArc/TestWorld_01/1200000003.hght",
        )

        self.assertEqual(height_map.size, 257)
        self.assertAlmostEqual(height_map.values[0], 0.125)
        self.assertAlmostEqual(height_map.values[128 * 257 + 64], 0.171875)
        self.assertAlmostEqual(height_map.values[-1], 0.25)
        validate_height_range(height_map, resource)
        height_map.values[0] = 0.05
        validate_height_range(height_map, resource)
        self.assertFalse(height_is_populated(height_map.values[0], resource))
        self.assertTrue(height_is_populated(height_map.values[1], resource))

    def test_decodes_material_ids_and_blend_weights(self):
        material_map = read_material_map(
            make_material_map_resource(),
            "TerrainArc/TestWorld_01/1200000003.mate",
        )

        self.assertEqual(material_map.size, 256)
        self.assertEqual(len(material_map.material_ids), 256 * 256 * 4)
        self.assertEqual(material_map.material_ids[:4], bytes((3, 7, 127, 127)))
        self.assertEqual(material_map.material_ids[-4:], bytes((3, 7, 127, 127)))
        self.assertEqual(material_map.weights[:4], bytes((192, 63, 0, 0)))
        self.assertEqual(material_map.weights[-4:], bytes((192, 63, 0, 0)))


class ConverterTests(unittest.TestCase):
    def test_selects_one_named_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bfrass.exe"
            model = root / "model.bfres"
            output = root / "model.glb"
            executable.write_bytes(b"converter")
            model.write_bytes(b"FRES    ")
            commands: list[list[str]] = []

            def run(command, **_kwargs):
                commands.append(command)
                output.write_bytes(b"glTF")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch(
                "oot_remake_importer.core.converter.subprocess.run",
                side_effect=run,
            ):
                convert_bfres(
                    executable,
                    model,
                    output,
                    model_name="Land_Test_A_01",
                )

        self.assertEqual(len(commands), 1)
        selection = commands[0].index("--model")
        self.assertEqual(commands[0][selection + 1], "Land_Test_A_01")

    def test_rejects_empty_model_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bfrass.exe"
            model = root / "model.bfres"
            executable.write_bytes(b"converter")
            model.write_bytes(b"FRES    ")

            with self.assertRaisesRegex(ConversionError, "cannot be empty"):
                convert_bfres(
                    executable,
                    model,
                    root / "model.glb",
                    model_name=" ",
                )


class ResourceTests(unittest.TestCase):
    def test_reads_sarc_member(self):
        self.assertEqual(read_sarc(make_sarc("zs.zsdic", b"dictionary")), {"zs.zsdic": b"dictionary"})

    def test_matches_exact_texture_references(self):
        entries = {
            "Tex/Hero_Alb.bntx.zs": object(),
            "Tex/Hero_Nrm.bntx.zs": object(),
            "Other/Hero_Alb.bntx.zs": object(),
            "Tex/Unused.bntx.zs": object(),
        }
        index = build_texture_index(entries)
        model = b"prefix\0Hero_Alb\0Hero_Nrm.bntx\0Hero\0"
        self.assertEqual(
            referenced_texture_paths(model, index),
            ("Tex/Hero_Alb.bntx.zs", "Tex/Hero_Nrm.bntx.zs"),
        )
        self.assertEqual(
            texture_resource_path("Hero_Alb", index),
            "Tex/Hero_Alb.bntx.zs",
        )
        self.assertIsNone(texture_resource_path("Missing", index))

    def test_classifies_bfres_resources(self):
        self.assertEqual(classify_bfres("Model/Hero.Hero.bfres.zs"), "model")
        self.assertEqual(classify_bfres("Model/Hero.anim.bfres.zs"), "animation")
        self.assertEqual(classify_bfres("Model/Hero.Skeleton.bfres.zs"), "skeleton")
        self.assertEqual(classify_bfres("CameraAnimation/Test.camera.bfres.zs"), "camera")

    def test_lists_models_and_animation_reference_rigs(self):
        entries = {
            "Model/Hero.Hero.bfres.zs": object(),
            "Model/Hero_Animation.Skeleton.bfres.zs": object(),
            "Model/Hero_Animation.anim.bfres.zs": object(),
            "Model/Hero.Hero.bfres.mc": object(),
            "CameraAnimation/Hero.camera.bfres.zs": object(),
        }
        self.assertEqual(
            model_paths(entries),
            ("Model/Hero.Hero.bfres.zs", "Model/Hero_Animation.Skeleton.bfres.zs"),
        )

    def test_lists_only_standalone_model_animations(self):
        entries = {
            "Model/Hero.Hero.bfres.zs": object(),
            "Model/Hero.anim.bfres.zs": object(),
            "Model/Hero.skeleton.bfres.zs": object(),
            "CameraAnimation/Hero.anim.bfres.zs": object(),
        }
        self.assertEqual(animation_paths(entries), ("Model/Hero.anim.bfres.zs",))


class AnimationTests(unittest.TestCase):
    def test_reads_bfres_10_skeletal_animation(self):
        animations = read_skeletal_animations(make_animation_bfres())
        self.assertEqual(len(animations), 1)
        animation = animations[0]
        self.assertEqual(animation.name, "Walk")
        self.assertEqual(animation.frame_count, 2)
        self.assertTrue(animation.loop)
        self.assertEqual(animation.rotation_mode, "euler_xyz")
        self.assertEqual(animation.scale_mode, "maya")
        self.assertEqual(animation.bones[0].name, "Root")
        curve = animation.bones[0].curves[0]
        self.assertEqual(curve.target, TRANSLATE_X)
        self.assertAlmostEqual(curve.sample(0), 1.0)
        self.assertAlmostEqual(curve.sample(1), 1.5)
        self.assertAlmostEqual(curve.sample(2), 2.0)

    def test_decodes_full_range_unsigned_int16_points_and_negative_tangents(self):
        animation = read_skeletal_animations(
            make_animation_bfres(
                value_scale=0.001,
                value_addend=-32.768,
                tangent_scale=0.01,
                tangent_addend=-1.0,
                key_data=struct.pack("<HHHH", 0, 0, 65535, 0),
            )
        )[0]
        curve = animation.bones[0].curves[0]
        self.assertAlmostEqual(curve.sample(0.0), -32.768, places=5)
        self.assertAlmostEqual(curve.sample(2.0), 32.767, places=5)
        self.assertLess(curve.sample(0.0001), curve.sample(0.0))

    def test_decodes_interval_scaled_int8_hermite_curve(self):
        animation = read_skeletal_animations(
            make_animation_bfres(
                curve_flags=0x4C0A,
                animation_frame_count=14,
                curve_start=10.0,
                curve_end=14.0,
                interval_scale=2.0,
                value_scale=0.5,
                value_addend=-64.0,
                tangent_scale=0.25,
                tangent_addend=-1.0,
                frame_data=bytes((0, 2)),
                key_data=struct.pack("<BBBB", 0, 0, 255, 0),
            )
        )[0]
        curve = animation.bones[0].curves[0]
        self.assertEqual(curve.frames, (10.0, 14.0))
        self.assertAlmostEqual(curve.sample(10.0), -64.0)
        self.assertAlmostEqual(curve.sample(14.0), 63.5)
        self.assertLess(curve.sample(10.0001), curve.sample(10.0))

    def test_decodes_unsigned_tangent_lane(self):
        animation = read_skeletal_animations(
            make_animation_bfres(
                value_scale=0.1,
                value_addend=1.0,
                tangent_scale=1.0 / 65535.0,
                tangent_addend=-1.0,
                key_data=struct.pack("<HHHH", 0, 65535, 0, 65535),
            )
        )[0]
        curve = animation.bones[0].curves[0]
        self.assertEqual(curve.keys[0][1], 65535.0)
        self.assertAlmostEqual(curve.sample(0.0), 1.0)
        self.assertAlmostEqual(curve.sample(1.0), 1.0)
        self.assertAlmostEqual(curve.sample(2.0), 1.0)

    def test_reads_int16_points_above_half_range_without_wrapping(self):
        # A rest rotation of 0 stored as 50469 * 0.00014380543 - 7.2577662,
        # the layout of Player_Adult's Ph_Wrist_Assist_A_R rotate-X curve.
        # A signed read gives -3*pi, a 180-degree flip.
        animation = read_skeletal_animations(
            make_animation_bfres(
                value_scale=0.00014380543,
                value_addend=-7.2577662,
                tangent_scale=6.103515625e-05,
                tangent_addend=-1.26019287109375,
                key_data=struct.pack("<HHHH", 50469, 20647, 50469, 20647),
            )
        )[0]
        curve = animation.bones[0].curves[0]
        self.assertAlmostEqual(curve.sample(0.0), 0.0, places=3)
        self.assertAlmostEqual(curve.sample(1.0), 0.0, places=3)

    def test_preserves_duplicate_frame_discontinuities(self):
        curve = AnimationCurve(
            target=TRANSLATE_X,
            curve_type=CURVE_CUBIC,
            frames=(0.0, 10.0, 10.0, 20.0),
            keys=(
                (0.0, 0.0),
                (0.0, 0.0),
                (100.0, 0.0),
                (100.0, 0.0),
            ),
            scale=1.0,
            offset=0.0,
            extended=True,
        )
        self.assertAlmostEqual(curve.sample(5.0), 0.0)
        self.assertAlmostEqual(curve.sample(9.999), 0.0)
        self.assertAlmostEqual(curve.sample(10.0), 100.0)
        self.assertAlmostEqual(curve.sample(15.0), 100.0)

    def test_reads_interval_scaled_uint16_frames_above_half_range(self):
        # Animal_Cow_Animation's translate curves end at frame 1100 with a
        # 1/32 interval: the last frame is stored as 35200, which a signed
        # read turns into -30336.
        animation = read_skeletal_animations(
            make_animation_bfres(
                curve_flags=0x4C05,
                animation_frame_count=1100,
                curve_end=1100.0,
                interval_scale=0.03125,
                frame_data=struct.pack("<2H", 0, 35200),
            )
        )[0]
        self.assertEqual(animation.bones[0].curves[0].frames, (0.0, 1100.0))

    def test_reads_quaternion_bone_in_mixed_rotation_clip(self):
        half = math.sqrt(0.5)
        animation = read_skeletal_animations(
            make_animation_bfres(
                curve_flags=0x4082,
                animation_flags=0x2204,
                bone_flags=BONE_QUATERNION_ROTATION | 0x1038,
                curve_target=ROTATE_X,
                frame_data=bytes((0, 2)),
                key_data=struct.pack("<8f", 0, 0, 0, 1, 0, 0, half, half),
                tangent_prefix=False,
            )
        )[0]
        self.assertEqual(animation.rotation_mode, "mixed")
        bone = animation.bones[0]
        self.assertTrue(bone.quaternion_rotation)
        curve = bone.curves[0]
        self.assertTrue(curve.is_quaternion)
        expected = (0.0, 0.0, math.sin(math.pi / 8), math.cos(math.pi / 8))
        for actual, wanted in zip(curve.sample_quaternion(1.0), expected):
            self.assertAlmostEqual(actual, wanted)

        skeleton = read_model_skeletons(make_model_bfres())[0]
        self.assertTrue(rotation_modes_compatible(animation, skeleton))
        world = evaluate_world_matrices(skeleton, {bone.name: bone}, 1.0)[0]
        angle = math.pi / 4
        self.assertAlmostEqual(world[0], math.cos(angle))
        self.assertAlmostEqual(world[1], -math.sin(angle))
        self.assertAlmostEqual(world[4], math.sin(angle))

    def test_rejects_scalar_rotation_curve_on_quaternion_bone(self):
        with self.assertRaises(AnimationError):
            read_skeletal_animations(
                make_animation_bfres(
                    bone_flags=BONE_QUATERNION_ROTATION | 0x0238,
                    curve_target=ROTATE_X,
                )
            )

    def test_reports_animation_kinds_without_skeletal_clips(self):
        data = bytearray(make_animation_bfres())
        struct.pack_into("<QQ", data, 0x58, 0, 0)
        struct.pack_into("<Q", data, 0x70, 0x600)
        struct.pack_into("<Ii", data, 0x600, 0, 3)
        self.assertEqual(read_skeletal_animations(bytes(data)), ())
        self.assertEqual(unsupported_animation_counts(bytes(data)), {"material": 3})

    def test_evaluates_zero_scale_parent_without_inverting_it(self):
        # The title hides parts by scaling bones to zero; world matrices must
        # not depend on inverting a parent.
        skeleton = read_model_skeletons(make_model_bfres(include_child=True, compensate_child=False))[0]
        hidden = BoneAnimation(
            name="Root",
            flags=0,
            base_scale=(0.0, 0.0, 0.0),
            base_rotation=None,
            base_translation=None,
            curves=(),
        )
        world = evaluate_world_matrices(skeleton, {"Root": hidden}, 0.0)
        self.assertEqual(world[1][3], 0.0)
        with self.assertRaises(AnimationError):
            _evaluate_skeleton(skeleton, {"Root": hidden}, 0.0)

    def test_marks_segment_scale_compensating_bones(self):
        skeleton = read_model_skeletons(make_model_bfres(include_child=True))[0]
        poses = sample_bone_poses(skeleton, {}, 0.0)
        self.assertFalse(poses[0].compensates_parent_scale)
        self.assertTrue(poses[1].compensates_parent_scale)

    def test_builds_armature_skin_for_meshless_glb(self):
        skeleton = read_model_skeletons(make_model_bfres(include_child=True))[0]
        document = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "Root", "children": [1]},
                {"name": "Child"},
            ],
            "buffers": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.glb"
            output = Path(temporary) / "rig.glb"
            model.write_bytes(make_glb(document, None))
            self.assertFalse(glb_has_meshes(model))
            report = prepare_skeleton_glb(model, output, skeleton)
            rigged, _ = read_glb(output.read_bytes())
        self.assertEqual(report.total_joints, 2)
        self.assertEqual(rigged["skins"][0]["joints"], [0, 1])

    def test_rejects_non_bfres_animation(self):
        with self.assertRaises(AnimationError):
            read_skeletal_animations(bytes(0x100))

    def test_reads_bfres_10_model_skeleton(self):
        skeletons = read_model_skeletons(make_model_bfres(include_child=True))
        self.assertEqual(len(skeletons), 1)
        skeleton = skeletons[0]
        self.assertEqual(skeleton.name, "Hero")
        self.assertEqual(skeleton.scale_mode, "maya")
        self.assertEqual(skeleton.rotation_mode, "euler_xyz")
        self.assertEqual(tuple(bone.name for bone in skeleton.bones), ("Root", "Child"))
        self.assertEqual(skeleton.bones[1].parent_index, 0)
        self.assertTrue(skeleton.bones[1].flags & SEGMENT_SCALE_COMPENSATE)

    def test_injects_sampled_animation_into_glb(self):
        animation = read_skeletal_animations(make_animation_bfres())[0]
        skeleton = read_model_skeletons(make_model_bfres())[0]
        document = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {
                    "name": "Root",
                    "matrix": [
                        1, 0, 0, 0,
                        0, 1, 0, 0,
                        0, 0, 1, 0,
                        0, 0, 0, 1,
                    ],
                }
            ],
            "buffers": [{"byteLength": 4}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.glb"
            output = Path(temporary) / "animated.glb"
            model.write_bytes(make_glb(document, bytes(4)))
            report = inject_skeletal_animation(model, output, animation, skeleton, fps=2)
            animated, binary = read_glb(output.read_bytes())

        self.assertEqual(report.animation, "Walk")
        self.assertEqual(report.matched_bones, 1)
        self.assertNotIn("matrix", animated["nodes"][0])
        self.assertEqual(animated["nodes"][0]["translation"], [0.0, 0.0, 0.0])
        self.assertEqual(len(animated["animations"]), 1)
        gltf_animation = animated["animations"][0]
        self.assertEqual(gltf_animation["name"], "Walk")
        self.assertEqual(len(gltf_animation["channels"]), 3)
        time_accessor = gltf_animation["samplers"][0]["input"]
        self.assertEqual(accessor_floats(animated, binary, time_accessor), (0.0, 0.5, 1.0))
        translation_channel = next(
            channel
            for channel in gltf_animation["channels"]
            if channel["target"]["path"] == "translation"
        )
        translation_accessor = gltf_animation["samplers"][translation_channel["sampler"]]["output"]
        self.assertEqual(
            accessor_floats(animated, binary, translation_accessor),
            (1.0, 0.0, 0.0, 1.5, 0.0, 0.0, 2.0, 0.0, 0.0),
        )

    def test_promotes_complete_skeleton_into_glb_skin(self):
        skeleton = read_model_skeletons(make_model_bfres(include_child=True))[0]
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        document = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "Root", "children": [1]},
                {"name": "Child"},
            ],
            "skins": [{"joints": [0], "inverseBindMatrices": 0}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 64}],
            "accessors": [
                {
                    "bufferView": 0,
                    "componentType": 5126,
                    "count": 1,
                    "type": "MAT4",
                }
            ],
            "buffers": [{"byteLength": 64}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.glb"
            output = Path(temporary) / "rig.glb"
            model.write_bytes(make_glb(document, struct.pack("<16f", *identity)))
            report = prepare_skeleton_glb(model, output, skeleton)
            rigged, binary = read_glb(output.read_bytes())

        self.assertEqual(report.original_joints, 1)
        self.assertEqual(report.promoted_joints, 1)
        self.assertEqual(report.total_joints, 2)
        self.assertEqual(rigged["skins"][0]["joints"], [0, 1])
        inverse_accessor = rigged["skins"][0]["inverseBindMatrices"]
        self.assertEqual(rigged["accessors"][inverse_accessor]["count"], 2)
        self.assertEqual(
            accessor_floats(rigged, binary, inverse_accessor),
            identity + identity,
        )

    def test_emits_scale_compensation_for_unanimated_child(self):
        animation = make_scale_animation()
        skeleton = read_model_skeletons(make_model_bfres(include_child=True))[0]
        identity = [
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1,
        ]
        child_matrix = identity.copy()
        child_matrix[12:15] = [1, 1, 0]
        document = {
            "asset": {"version": "2.0"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [
                {"name": "Root", "matrix": identity, "children": [1]},
                {"name": "Child", "matrix": child_matrix},
            ],
            "buffers": [{"byteLength": 4}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.glb"
            output = Path(temporary) / "animated.glb"
            model.write_bytes(make_glb(document, bytes(4)))
            inject_skeletal_animation(model, output, animation, skeleton, fps=2)
            animated, binary = read_glb(output.read_bytes())

        child_scale_channel = next(
            channel
            for channel in animated["animations"][0]["channels"]
            if channel["target"] == {"node": 1, "path": "scale"}
        )
        accessor = animated["animations"][0]["samplers"][child_scale_channel["sampler"]]["output"]
        child_scales = accessor_floats(animated, binary, accessor)
        expected = (1.0, 1.0, 1.0, 2.0 / 3.0, 1.0, 1.0, 0.5, 1.0, 1.0)
        for actual, wanted in zip(child_scales, expected):
            self.assertAlmostEqual(actual, wanted)


class MaterialTests(unittest.TestCase):
    def test_accepts_model_without_materials(self):
        # Dm_Locator and the cubemap-probe models have a skeleton only.
        self.assertEqual(read_materials(make_model_bfres(), {}), {})

    def test_reads_external_string_hashes(self):
        strings = read_external_strings(make_external_strings())
        self.assertEqual(
            strings[MATERIAL_HASHES["ns_param_color0"]],
            "ns_param_color0",
        )

    def test_reads_bfres_10_material_metadata(self):
        strings = read_external_strings(make_external_strings())
        materials = read_materials(make_material_bfres(), strings)
        material = materials["Mt_Face"]
        self.assertEqual(
            material.samplers,
            {"_a0": "Mt_Face_Alb", "_tgt0": "Mt_Face_Tgt"},
        )
        self.assertEqual(material.shader_archive, "Model.Product")
        self.assertEqual(material.shader_model, "Model")
        color = material.parameters["ns_param_color0"]
        self.assertEqual(color.type_id, 15)
        self.assertAlmostEqual(color.values[1], 0.35)
        self.assertAlmostEqual(color.values[2], 0.36)

    def test_rejects_external_string_range_outside_file(self):
        data = bytearray(make_external_strings())
        struct.pack_into("<Q", data, 0xB8, len(data) - 4)
        with self.assertRaises(MaterialError):
            read_external_strings(bytes(data))

    def test_rejects_shader_parameter_source_outside_file(self):
        data = bytearray(make_material_bfres())
        struct.pack_into("<Q", data, 0x300 + 0x58, len(data) - 4)
        with self.assertRaises(MaterialError):
            read_materials(bytes(data), read_external_strings(make_external_strings()))

    def test_loads_packaged_material_semantics(self):
        semantics = load_material_semantics()
        self.assertEqual(semantics.schema_version, 1)
        self.assertIn("_tgt0", semantics.control_sampler_hints)
        self.assertEqual(semantics.samplers["_a0"].role, "base_color")
        layer_blend = semantics.samplers["_a1"].layer_blend
        self.assertIsNotNone(layer_blend)
        self.assertEqual(layer_blend.attribute, "_c0")
        self.assertEqual(layer_blend.channel, "red")

    def test_rejects_invalid_material_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "material_semantics.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            with self.assertRaises(MaterialError):
                load_material_semantics(path)

    def test_classifies_control_sampler_vertex_colors(self):
        metadata = MaterialMetadata(
            name="Mt_Body",
            shader_archive="Model.Product",
            shader_model="Model",
            samplers={"_a0": "Body_Alb", "_tgt0": "Body_Tgt"},
            parameters={},
            options={},
        )
        decision = classify_vertex_colors(
            metadata,
            grayscale=False,
            control_profile=False,
        )
        self.assertEqual(decision.mode, "control_mask_bypassed")
        self.assertIn("_tgt0", decision.reason)

    def test_classifies_two_color_tint_before_control_hint(self):
        metadata = MaterialMetadata(
            name="Mt_MultiColor",
            shader_archive="Model.Product",
            shader_model="Model",
            samplers={"_a0": "White_Alb", "_a1": "Layer_Alb"},
            parameters={
                "ns_param_color0": ShaderParameter(15, (0.56, 0.54, 0.51, 1.0)),
                "ns_param_color1": ShaderParameter(15, (0.21, 0.16, 0.10, 1.0)),
            },
            options={},
        )
        decision = classify_vertex_colors(
            metadata,
            grayscale=True,
            control_profile=False,
        )
        self.assertEqual(decision.mode, "material_tint_mask")
        self.assertEqual(decision.colors[0][0], 0.56)

    def test_preserves_visible_vertex_tint_and_describes_unknown_sampler(self):
        metadata = MaterialMetadata(
            name="Mt_VertexColor",
            shader_archive="Model.Product",
            shader_model="Model",
            samplers={"_custom0": "Custom"},
            parameters={},
            options={},
        )
        semantics = load_material_semantics()
        decision = classify_vertex_colors(
            metadata,
            grayscale=False,
            control_profile=False,
            semantics=semantics,
        )
        self.assertEqual(decision.mode, "visible_tint")
        description = describe_sampler_bindings(metadata, semantics)
        self.assertEqual(description["_custom0"]["role"], "unknown")
        self.assertEqual(description["_custom0"]["confidence"], "unclassified")

    def test_resolves_generated_texture_coordinate_transform(self):
        metadata = MaterialMetadata(
            name="Mt_Layered",
            shader_archive="Model.Product",
            shader_model="Model",
            samplers={"_a0": "Bark_Alb", "_a1": "Moss_Alb"},
            parameters={
                "ns_tex_mtx1": ShaderParameter(
                    30,
                    (0, 5.0, 3.0, 0.25, 0.1, -0.2),
                ),
            },
            options={
                "ns_texture0_sampler": "0",
                "ns_texture1_sampler": "1",
                "ns_texture1_texcoord": "1",
                "ns_texcoord1_srt": "1",
            },
        )
        transform = texture_coordinate_transform(metadata, "_a1")
        self.assertIsNotNone(transform)
        self.assertEqual(transform.texture_index, 1)
        self.assertEqual(transform.coordinate_index, 1)
        self.assertEqual(transform.srt_index, 1)
        self.assertEqual(transform.scale, (5.0, 3.0))
        self.assertEqual(transform.rotation, 0.25)
        self.assertEqual(transform.translation, (0.1, -0.2))

    def test_rejects_malformed_texture_coordinate_transform(self):
        metadata = MaterialMetadata(
            name="Mt_Broken",
            shader_archive="Model.Product",
            shader_model="Model",
            samplers={"_a0": "Broken_Alb"},
            parameters={"ns_tex_mtx0": ShaderParameter(30, (0, 1.0))},
            options={},
        )
        with self.assertRaises(MaterialError):
            texture_coordinate_transform(metadata, "_a0")


if __name__ == "__main__":
    unittest.main()
