"""Read BFRES 10 material metadata used to reconstruct Blender materials."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


EXTERNAL_STRINGS_PATH = "Shader/ExternalBinaryString.bfres.zs"
FRES_MAGIC = b"FRES    "
MAX_DICTIONARY_ITEMS = 65_536
MATERIAL_SEMANTICS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "material_semantics.json"
)
MATERIAL_SEMANTICS_SCHEMA = 1


class MaterialError(ValueError):
    """BFRES material metadata is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class ShaderParameter:
    type_id: int
    values: tuple[bool | int | float, ...]


@dataclass(frozen=True, slots=True)
class MaterialMetadata:
    name: str
    shader_archive: str
    shader_model: str
    samplers: dict[str, str]
    parameters: dict[str, ShaderParameter]
    options: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class VertexColorRules:
    tint_parameters: tuple[str, str]
    grayscale_tolerance: float
    color_difference_min: float
    boundary_span_max: float
    boundary_low_max: float
    boundary_high_min: float
    varying_span_min: float


@dataclass(frozen=True, slots=True)
class LayerBlendSemantic:
    attribute: str
    channel: str
    confidence: str


@dataclass(frozen=True, slots=True)
class SamplerSemantic:
    role: str
    mapping: str
    vertex_color: str
    confidence: str
    layer_blend: LayerBlendSemantic | None = None


@dataclass(frozen=True, slots=True)
class MaterialSemantics:
    schema_version: int
    catalog_version: str
    minimum_bfres_major: int
    observed_bfres_version: str
    vertex_colors: VertexColorRules
    samplers: dict[str, SamplerSemantic]

    @property
    def control_sampler_hints(self) -> frozenset[str]:
        return frozenset(
            name
            for name, semantic in self.samplers.items()
            if semantic.vertex_color == "control_hint"
        )


Color4 = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class MaterialDecision:
    mode: str
    reason: str
    colors: tuple[Color4, Color4] | None = None


@dataclass(frozen=True, slots=True)
class TextureCoordinateTransform:
    texture_index: int
    coordinate_index: int
    srt_index: int
    mode: int
    scale: tuple[float, float]
    rotation: float
    translation: tuple[float, float]


def _object(value, location: str) -> dict:
    if not isinstance(value, dict):
        raise MaterialError(f"material semantics {location} must be an object")
    return value


def _string(value, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise MaterialError(f"material semantics {location} must be a non-empty string")
    return value


def _number(value, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterialError(f"material semantics {location} must be a number")
    result = float(value)
    if result < 0:
        raise MaterialError(f"material semantics {location} must not be negative")
    return result


def _read_material_semantics(path: Path) -> MaterialSemantics:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MaterialError(f"cannot read material semantics catalog {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise MaterialError(f"material semantics catalog is invalid JSON: {error}") from error

    root = _object(raw, "root")
    schema_version = root.get("schema_version")
    if schema_version != MATERIAL_SEMANTICS_SCHEMA:
        raise MaterialError(
            f"material semantics schema {schema_version!r} is unsupported; "
            f"expected {MATERIAL_SEMANTICS_SCHEMA}"
        )
    catalog_version = _string(root.get("catalog_version"), "catalog_version")

    bfres = _object(root.get("bfres"), "bfres")
    minimum_major = bfres.get("minimum_major")
    if isinstance(minimum_major, bool) or not isinstance(minimum_major, int):
        raise MaterialError("material semantics bfres.minimum_major must be an integer")
    observed_version = _string(
        bfres.get("observed_version"),
        "bfres.observed_version",
    )

    vertex = _object(root.get("vertex_colors"), "vertex_colors")
    tint_parameters = vertex.get("tint_parameters")
    if (
        not isinstance(tint_parameters, list)
        or len(tint_parameters) != 2
        or not all(isinstance(value, str) and value for value in tint_parameters)
    ):
        raise MaterialError(
            "material semantics vertex_colors.tint_parameters must contain two names"
        )
    vertex_colors = VertexColorRules(
        tint_parameters=(tint_parameters[0], tint_parameters[1]),
        grayscale_tolerance=_number(
            vertex.get("grayscale_tolerance"),
            "vertex_colors.grayscale_tolerance",
        ),
        color_difference_min=_number(
            vertex.get("color_difference_min"),
            "vertex_colors.color_difference_min",
        ),
        boundary_span_max=_number(
            vertex.get("boundary_span_max"),
            "vertex_colors.boundary_span_max",
        ),
        boundary_low_max=_number(
            vertex.get("boundary_low_max"),
            "vertex_colors.boundary_low_max",
        ),
        boundary_high_min=_number(
            vertex.get("boundary_high_min"),
            "vertex_colors.boundary_high_min",
        ),
        varying_span_min=_number(
            vertex.get("varying_span_min"),
            "vertex_colors.varying_span_min",
        ),
    )

    raw_samplers = _object(root.get("samplers"), "samplers")
    if not raw_samplers:
        raise MaterialError("material semantics catalog contains no samplers")
    samplers: dict[str, SamplerSemantic] = {}
    for name, raw_semantic in raw_samplers.items():
        name = _string(name, "sampler name")
        semantic = _object(raw_semantic, f"samplers.{name}")
        mapping = _string(semantic.get("mapping"), f"samplers.{name}.mapping")
        if mapping not in {"bfrass_gltf", "blender_reconstructed", "metadata_only"}:
            raise MaterialError(
                f"material semantics samplers.{name}.mapping {mapping!r} is unsupported"
            )
        vertex_color = _string(
            semantic.get("vertex_color"),
            f"samplers.{name}.vertex_color",
        )
        if vertex_color not in {"none", "control_hint"}:
            raise MaterialError(
                f"material semantics samplers.{name}.vertex_color "
                f"{vertex_color!r} is unsupported"
            )
        confidence = _string(
            semantic.get("confidence"),
            f"samplers.{name}.confidence",
        )
        if confidence not in {"verified", "observed", "upstream"}:
            raise MaterialError(
                f"material semantics samplers.{name}.confidence "
                f"{confidence!r} is unsupported"
            )
        raw_layer_blend = semantic.get("layer_blend")
        layer_blend = None
        if raw_layer_blend is not None:
            blend = _object(raw_layer_blend, f"samplers.{name}.layer_blend")
            attribute = _string(
                blend.get("attribute"),
                f"samplers.{name}.layer_blend.attribute",
            )
            if (
                len(attribute) != 3
                or not attribute.startswith("_c")
                or not attribute[2].isdigit()
                or int(attribute[2]) > 7
            ):
                raise MaterialError(
                    f"material semantics samplers.{name}.layer_blend.attribute "
                    "must be _c0 through _c7"
                )
            channel = _string(
                blend.get("channel"),
                f"samplers.{name}.layer_blend.channel",
            )
            if channel not in {"red", "green", "blue", "alpha"}:
                raise MaterialError(
                    f"material semantics samplers.{name}.layer_blend.channel "
                    f"{channel!r} is unsupported"
                )
            blend_confidence = _string(
                blend.get("confidence"),
                f"samplers.{name}.layer_blend.confidence",
            )
            if blend_confidence not in {"verified", "observed"}:
                raise MaterialError(
                    f"material semantics samplers.{name}.layer_blend.confidence "
                    f"{blend_confidence!r} is unsupported"
                )
            layer_blend = LayerBlendSemantic(
                attribute=attribute,
                channel=channel,
                confidence=blend_confidence,
            )
        samplers[name] = SamplerSemantic(
            role=_string(semantic.get("role"), f"samplers.{name}.role"),
            mapping=mapping,
            vertex_color=vertex_color,
            confidence=confidence,
            layer_blend=layer_blend,
        )

    return MaterialSemantics(
        schema_version=schema_version,
        catalog_version=catalog_version,
        minimum_bfres_major=minimum_major,
        observed_bfres_version=observed_version,
        vertex_colors=vertex_colors,
        samplers=samplers,
    )


@lru_cache(maxsize=1)
def _default_material_semantics() -> MaterialSemantics:
    return _read_material_semantics(MATERIAL_SEMANTICS_PATH)


def load_material_semantics(path: str | Path | None = None) -> MaterialSemantics:
    """Load and validate the packaged BFRES material semantics catalog."""
    return (
        _default_material_semantics()
        if path is None
        else _read_material_semantics(Path(path))
    )


def parameter_color(metadata: MaterialMetadata, name: str) -> Color4 | None:
    parameter = metadata.parameters.get(name)
    if parameter is None or parameter.type_id != 15 or len(parameter.values) != 4:
        return None
    return tuple(float(value) for value in parameter.values)


def _option_index(
    metadata: MaterialMetadata,
    name: str,
    default: int,
) -> int:
    value = metadata.options.get(name)
    if value is None:
        return default
    try:
        result = int(value)
    except ValueError as error:
        raise MaterialError(
            f"material {metadata.name!r} option {name} is not an integer: {value!r}"
        ) from error
    if result < 0:
        raise MaterialError(
            f"material {metadata.name!r} option {name} is negative: {result}"
        )
    return result


def texture_coordinate_transform(
    metadata: MaterialMetadata,
    sampler: str,
) -> TextureCoordinateTransform | None:
    """Resolve one sampler's generated texture coordinate and Nintendo TexSrt."""
    sampler_names = tuple(metadata.samplers)
    try:
        sampler_index = sampler_names.index(sampler)
    except ValueError:
        return None

    texture_index = sampler_index
    for candidate in range(8):
        value = metadata.options.get(f"ns_texture{candidate}_sampler")
        if value is None:
            continue
        if _option_index(
            metadata,
            f"ns_texture{candidate}_sampler",
            candidate,
        ) == sampler_index:
            texture_index = candidate
            break

    coordinate_index = _option_index(
        metadata,
        f"ns_texture{texture_index}_texcoord",
        0,
    )
    srt_index = _option_index(
        metadata,
        f"ns_texcoord{coordinate_index}_srt",
        coordinate_index,
    )
    parameter_name = f"ns_tex_mtx{srt_index}"
    parameter = metadata.parameters.get(parameter_name)
    if parameter is None:
        return TextureCoordinateTransform(
            texture_index=texture_index,
            coordinate_index=coordinate_index,
            srt_index=srt_index,
            mode=0,
            scale=(1.0, 1.0),
            rotation=0.0,
            translation=(0.0, 0.0),
        )
    if len(parameter.values) not in {6, 7}:
        raise MaterialError(
            f"material {metadata.name!r} parameter {parameter_name} "
            f"has {len(parameter.values)} values; expected 6 or 7"
        )
    mode_value = parameter.values[0]
    if isinstance(mode_value, bool) or int(mode_value) != mode_value:
        raise MaterialError(
            f"material {metadata.name!r} parameter {parameter_name} "
            f"has invalid mode {mode_value!r}"
        )
    values = tuple(float(value) for value in parameter.values[1:6])
    if not all(math.isfinite(value) for value in values):
        raise MaterialError(
            f"material {metadata.name!r} parameter {parameter_name} "
            "contains a non-finite transform"
        )
    return TextureCoordinateTransform(
        texture_index=texture_index,
        coordinate_index=coordinate_index,
        srt_index=srt_index,
        mode=int(mode_value),
        scale=(values[0], values[1]),
        rotation=values[2],
        translation=(values[3], values[4]),
    )


def material_tint_colors(
    metadata: MaterialMetadata,
    semantics: MaterialSemantics,
) -> tuple[Color4, Color4] | None:
    color0 = parameter_color(metadata, semantics.vertex_colors.tint_parameters[0])
    color1 = parameter_color(metadata, semantics.vertex_colors.tint_parameters[1])
    if color0 is None or color1 is None:
        return None
    difference = max(abs(color0[index] - color1[index]) for index in range(3))
    return (
        (color0, color1)
        if difference >= semantics.vertex_colors.color_difference_min
        else None
    )


def classify_vertex_colors(
    metadata: MaterialMetadata,
    *,
    grayscale: bool,
    control_profile: bool,
    semantics: MaterialSemantics | None = None,
) -> MaterialDecision:
    """Choose how Blender should interpret BFRES `_c0` for one material."""
    semantics = semantics or load_material_semantics()
    colors = material_tint_colors(metadata, semantics)
    if grayscale and colors is not None:
        first, second = semantics.vertex_colors.tint_parameters
        return MaterialDecision(
            mode="material_tint_mask",
            reason=f"grayscale vertex weights interpolate {first} and {second}",
            colors=colors,
        )

    control_samplers = sorted(
        set(metadata.samplers).intersection(semantics.control_sampler_hints)
    )
    if control_profile:
        return MaterialDecision(
            mode="control_mask_bypassed",
            reason="vertex channels have a packed control-mask distribution",
        )
    if control_samplers:
        return MaterialDecision(
            mode="control_mask_bypassed",
            reason=(
                "material uses control sampler hint"
                if len(control_samplers) == 1
                else "material uses control sampler hints"
            )
            + f": {', '.join(control_samplers)}",
        )
    return MaterialDecision(
        mode="visible_tint",
        reason="no packed-control signature was detected",
    )


def describe_sampler_bindings(
    metadata: MaterialMetadata,
    semantics: MaterialSemantics,
) -> dict[str, dict[str, object]]:
    """Pair source textures with cataloged sampler roles for Blender metadata."""
    result: dict[str, dict[str, str]] = {}
    for name, texture in metadata.samplers.items():
        semantic = semantics.samplers.get(name)
        result[name] = {
            "texture": texture,
            "role": semantic.role if semantic is not None else "unknown",
            "mapping": semantic.mapping if semantic is not None else "metadata_only",
            "vertex_color": (
                semantic.vertex_color if semantic is not None else "none"
            ),
            "confidence": (
                semantic.confidence if semantic is not None else "unclassified"
            ),
        }
        if semantic is not None and semantic.layer_blend is not None:
            result[name]["layer_blend"] = {
                "attribute": semantic.layer_blend.attribute,
                "channel": semantic.layer_blend.channel,
                "confidence": semantic.layer_blend.confidence,
            }
    return result


class _Reader:
    def __init__(self, data: bytes, source: str):
        self.data = data
        self.source = source

    def _checked(self, offset: int, size: int) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise MaterialError(f"{self.source} range is out of bounds")

    def unpack(self, format_: str, offset: int):
        size = struct.calcsize(format_)
        self._checked(offset, size)
        return struct.unpack_from(format_, self.data, offset)

    def u8(self, offset: int) -> int:
        self._checked(offset, 1)
        return self.data[offset]

    def s8(self, offset: int) -> int:
        return self.unpack("<b", offset)[0]

    def u16(self, offset: int) -> int:
        return self.unpack("<H", offset)[0]

    def s16(self, offset: int) -> int:
        return self.unpack("<h", offset)[0]

    def s32(self, offset: int) -> int:
        return self.unpack("<i", offset)[0]

    def u32(self, offset: int) -> int:
        return self.unpack("<I", offset)[0]

    def u64(self, offset: int) -> int:
        return self.unpack("<Q", offset)[0]

    def f32(self, offset: int) -> float:
        return self.unpack("<f", offset)[0]

    def fixed(self, offset: int, size: int) -> bytes:
        self._checked(offset, size)
        return self.data[offset:offset + size]

    def string_at(self, offset: int) -> str:
        if offset == 0:
            return ""
        length = self.u16(offset)
        raw = self.fixed(offset + 2, length)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MaterialError(f"{self.source} contains an invalid UTF-8 string") from error


def _validate_fres(reader: _Reader, *, minimum_major: int = 10) -> None:
    if reader.fixed(0, len(FRES_MAGIC)) != FRES_MAGIC:
        raise MaterialError(f"{reader.source} is not a BFRES resource")
    if reader.u8(10) < minimum_major:
        raise MaterialError(
            f"{reader.source} uses BFRES {reader.u8(10)}; version {minimum_major} or newer is required"
        )


def _dict_count(reader: _Reader, offset: int) -> int:
    if offset == 0:
        return 0
    count = reader.s32(offset + 4)
    if count < 0 or count > MAX_DICTIONARY_ITEMS:
        raise MaterialError(f"{reader.source} dictionary count {count} is invalid")
    reader._checked(offset + 8, (count + 1) * 16)
    return count


def _dict_raw_keys(reader: _Reader, offset: int) -> tuple[int, ...]:
    count = _dict_count(reader, offset)
    return tuple(reader.u64(offset + 8 + (index + 1) * 16 + 8) for index in range(count))


def read_external_strings(data: bytes) -> dict[int, str]:
    """Resolve the 64-bit string hashes stored in the title's BFRES 10 files."""
    reader = _Reader(data, EXTERNAL_STRINGS_PATH)
    _validate_fres(reader)
    hash_array = reader.u64(0xB8)
    dictionary = reader.u64(0xC0)
    count = _dict_count(reader, dictionary)
    reader._checked(hash_array, count * 8)

    result: dict[int, str] = {}
    for index, string_offset in enumerate(_dict_raw_keys(reader, dictionary)):
        hash_value = reader.u64(hash_array + index * 8)
        value = reader.string_at(string_offset)
        if not value:
            raise MaterialError(f"{EXTERNAL_STRINGS_PATH} contains an empty string")
        previous = result.setdefault(hash_value, value)
        if previous != value:
            raise MaterialError(f"{EXTERNAL_STRINGS_PATH} contains a duplicate hash")
    if not result:
        raise MaterialError(f"{EXTERNAL_STRINGS_PATH} contains no external strings")
    return result


def _resolved_string(reader: _Reader, value: int, external_strings: dict[int, str]) -> str:
    if value == 0:
        return ""
    if value + 2 <= len(reader.data):
        return reader.string_at(value)
    try:
        return external_strings[value]
    except KeyError as error:
        raise MaterialError(f"{reader.source} references unknown external string {value:#018x}") from error


def _string_field(reader: _Reader, field: int, external_strings: dict[int, str]) -> str:
    return _resolved_string(reader, reader.u64(field), external_strings)


def _dict_names(
    reader: _Reader, offset: int, external_strings: dict[int, str]
) -> tuple[str, ...]:
    return tuple(
        _resolved_string(reader, value, external_strings)
        for value in _dict_raw_keys(reader, offset)
    )


def _string_array(
    reader: _Reader, offset: int, count: int, external_strings: dict[int, str]
) -> tuple[str, ...]:
    if count == 0:
        return ()
    if offset == 0:
        raise MaterialError(f"{reader.source} has a missing string array")
    reader._checked(offset, count * 8)
    return tuple(
        _resolved_string(reader, reader.u64(offset + index * 8), external_strings)
        for index in range(count)
    )


_PARAMETER_WORDS = {
    0: 1,
    1: 2,
    2: 3,
    3: 4,
    4: 1,
    5: 2,
    6: 3,
    7: 4,
    8: 1,
    9: 2,
    10: 3,
    11: 4,
    12: 1,
    13: 2,
    14: 3,
    15: 4,
    16: 4,
    17: 4,
    18: 6,
    19: 8,
    20: 3,
    21: 6,
    22: 9,
    23: 12,
    24: 4,
    25: 8,
    26: 12,
    27: 16,
}


def _parameter_values(reader: _Reader, source: int, offset: int, type_id: int):
    if source == 0:
        raise MaterialError(f"{reader.source} has a missing shader parameter source")
    if type_id in _PARAMETER_WORDS:
        count = _PARAMETER_WORDS[type_id]
        reader._checked(source + offset, count * 4)
        if type_id <= 3:
            return tuple(bool(reader.u32(source + offset + index * 4)) for index in range(count))
        if type_id <= 7:
            return tuple(reader.s32(source + offset + index * 4) for index in range(count))
        if type_id <= 11:
            return tuple(reader.u32(source + offset + index * 4) for index in range(count))
        return tuple(reader.f32(source + offset + index * 4) for index in range(count))
    if type_id == 28:
        return tuple(reader.f32(source + offset + index * 4) for index in range(5))
    if type_id == 29:
        return tuple(reader.f32(source + offset + index * 4) for index in range(9))
    if type_id in (30, 31):
        words = 6 if type_id == 30 else 7
        reader._checked(source + offset, words * 4)
        values: list[int | float] = [reader.u32(source + offset)]
        values.extend(reader.f32(source + offset + index * 4) for index in range(1, 6))
        if type_id == 31:
            values.append(reader.u32(source + offset + 24))
        return tuple(values)
    raise MaterialError(f"{reader.source} uses unknown shader parameter type {type_id}")


def _read_parameters(
    reader: _Reader,
    material: int,
    shader_assign: int,
    external_strings: dict[int, str],
) -> dict[str, ShaderParameter]:
    parameter_list = reader.u64(shader_assign + 0x20)
    parameter_dictionary = reader.u64(shader_assign + 0x28)
    names = _dict_names(reader, parameter_dictionary, external_strings)
    source = reader.u64(material + 0x58)
    if names and parameter_list == 0:
        raise MaterialError(f"{reader.source} has a missing shader parameter list")

    result: dict[str, ShaderParameter] = {}
    for index, fallback_name in enumerate(names):
        descriptor = parameter_list + index * 24
        reader._checked(descriptor, 24)
        name_value = reader.u64(descriptor + 8)
        name = _resolved_string(reader, name_value, external_strings) or fallback_name
        type_id = reader.u16(descriptor + 18)
        values = _parameter_values(reader, source, reader.u16(descriptor + 16), type_id)
        if name in result:
            raise MaterialError(f"{reader.source} contains duplicate shader parameter {name!r}")
        result[name] = ShaderParameter(type_id=type_id, values=values)
    return result


def _read_options(
    reader: _Reader,
    shader_info: int,
    shader_assign: int,
    external_strings: dict[int, str],
) -> dict[str, str | None]:
    names = _dict_names(reader, reader.u64(shader_assign + 0x40), external_strings)
    if not names:
        return {}

    toggle_array = reader.u64(shader_info + 0x28)
    string_array = reader.u64(shader_info + 0x30)
    index_array = reader.u64(shader_info + 0x38)
    boolean_count = reader.u16(shader_info + 0x46)
    choice_count = reader.u16(shader_info + 0x48)
    if choice_count < boolean_count:
        raise MaterialError(f"{reader.source} has invalid shader option counts")

    choices: list[str] = []
    for index in range(boolean_count):
        word = reader.u64(toggle_array + 8 * (index // 64)) if toggle_array else 0
        choices.append("True" if (word >> (index % 64)) & 1 else "False")
    choices.extend(
        _string_array(
            reader,
            string_array,
            choice_count - boolean_count,
            external_strings,
        )
    )

    result: dict[str, str | None] = {}
    for index, name in enumerate(names):
        choice_index = (
            reader.s16(index_array + 2 * (choice_count + index))
            if index_array
            else index
        )
        result[name] = choices[choice_index] if 0 <= choice_index < len(choices) else None
    return result


def _read_material(
    reader: _Reader,
    offset: int,
    external_strings: dict[int, str],
) -> MaterialMetadata:
    if reader.fixed(offset, 4) != b"FMAT":
        raise MaterialError(f"{reader.source} expected FMAT at {offset:#x}")
    name = _string_field(reader, offset + 8, external_strings)
    if not name:
        raise MaterialError(f"{reader.source} contains an unnamed material")

    sampler_count = reader.u8(offset + 0xA2)
    texture_count = reader.u8(offset + 0xA3)
    sampler_names = _dict_names(reader, reader.u64(offset + 0x38), external_strings)
    texture_names = _string_array(
        reader,
        reader.u64(offset + 0x20),
        texture_count,
        external_strings,
    )
    if len(sampler_names) < sampler_count:
        raise MaterialError(f"{reader.source} material {name!r} has missing sampler names")
    binding_count = min(sampler_count, texture_count)
    samplers = {
        sampler_names[index]: texture_names[index]
        for index in range(binding_count)
    }

    shader_info = reader.u64(offset + 0x10)
    shader_assign = reader.u64(shader_info) if shader_info else 0
    if shader_assign == 0:
        return MaterialMetadata(name, "", "", samplers, {}, {})
    return MaterialMetadata(
        name=name,
        shader_archive=_string_field(reader, shader_assign, external_strings),
        shader_model=_string_field(reader, shader_assign + 8, external_strings),
        samplers=samplers,
        parameters=_read_parameters(reader, offset, shader_assign, external_strings),
        options=_read_options(reader, shader_info, shader_assign, external_strings),
    )


def read_materials(
    model_data: bytes, external_strings: dict[int, str]
) -> dict[str, MaterialMetadata]:
    """Read material metadata from a BFRES 10 model."""
    reader = _Reader(model_data, "model BFRES")
    _validate_fres(reader)
    model_array = reader.u64(0x28)
    model_dictionary = reader.u64(0x30)
    model_count = _dict_count(reader, model_dictionary)
    if model_count == 0 or model_array == 0:
        raise MaterialError("model BFRES contains no models")

    result: dict[str, MaterialMetadata] = {}
    for model_index in range(model_count):
        model = model_array + model_index * 0x78
        if reader.fixed(model, 4) != b"FMDL":
            raise MaterialError(f"model BFRES expected FMDL at {model:#x}")
        material_array = reader.u64(model + 0x38)
        material_count = reader.u16(model + 0x6C)
        reader._checked(material_array, material_count * 0xB0)
        for material_index in range(material_count):
            metadata = _read_material(
                reader,
                material_array + material_index * 0xB0,
                external_strings,
            )
            previous = result.setdefault(metadata.name, metadata)
            if previous != metadata:
                raise MaterialError(f"model BFRES contains conflicting material {metadata.name!r}")
    # Locator and cubemap-probe models (Dm_Locator, Obj_WarpPoint_A, the
    # Cubemap_Model set) have a skeleton but no materials; they are valid.
    return result
