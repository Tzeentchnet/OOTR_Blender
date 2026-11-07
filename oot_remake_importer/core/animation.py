"""Read BFRES 10 skeletal animations and add one clip to a BfrAss GLB.

The FSKA and curve layouts were cross-checked against BfresLibrary at commit
e2c5debca60e9b95e0fd220d32886102d65166bf, specifically
Shared/SkeletalAnim/SkeletalAnim.cs::IResData.Load,
Shared/SkeletalAnim/BoneAnim.cs::IResData.Load, and
Shared/Common/AnimCurve.cs::IResData.Load. The BFRES 10 extended Hermite
layout was cross-checked as a behavioral reference against Watertoon/Awn
commit c3c090bb24bc786e6ff94fe3ae68778054f130a9,
libraries/lib_vp/include/vp/res/res_bfresanimcurve.hpp::
EvaluateFloatExtCubicTangentScaleImpl. This implementation was written
independently from the observed binary layout.
"""

from __future__ import annotations

import bisect
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path


FRES_MAGIC = b"FRES    "
GLB_MAGIC = b"glTF"
JSON_CHUNK = b"JSON"
BIN_CHUNK = b"BIN\0"
FSKA_SIZE = 0x50
BONE_ANIM_SIZE = 0x38
CURVE_SIZE = 0x30
MAX_ANIMATIONS = 10_000
MAX_BONES = 65_535
MAX_KEYS = 1_000_000
MAX_BONE_FRAME_SAMPLES = 1_000_000

SCALE_X = 0x04
SCALE_Y = 0x08
SCALE_Z = 0x0C
TRANSLATE_X = 0x10
TRANSLATE_Y = 0x14
TRANSLATE_Z = 0x18
ROTATE_X = 0x20
ROTATE_Y = 0x24
ROTATE_Z = 0x28
ROTATE_W = 0x2C
SUPPORTED_TARGETS = {
    SCALE_X,
    SCALE_Y,
    SCALE_Z,
    TRANSLATE_X,
    TRANSLATE_Y,
    TRANSLATE_Z,
    ROTATE_X,
    ROTATE_Y,
    ROTATE_Z,
    ROTATE_W,
}

CURVE_CUBIC = 0x00
CURVE_LINEAR = 0x10
CURVE_BAKED_FLOAT = 0x20
CURVE_STEP_INT = 0x40
CURVE_BAKED_INT = 0x50
CURVE_STEP_BOOL = 0x60
CURVE_BAKED_BOOL = 0x70
# Whole-quaternion curves: one float4 per key. 0x80 and 0xA0 interpolate
# spherically, 0x90 linearly then normalizes (Watertoon/Awn,
# res_bfresanimcurve.hpp, BfresAnimCurveCurveType, read for behaviour only).
CURVE_QUATERNION_SLERP = 0x80
CURVE_QUATERNION_NLERP = 0x90
CURVE_QUATERNION_SLERP2 = 0xA0
QUATERNION_CURVES = (CURVE_QUATERNION_SLERP, CURVE_QUATERNION_NLERP, CURVE_QUATERNION_SLERP2)

CURVE_TYPE_MASK = 0xF0
CURVE_INTERVAL_SCALE = 1 << 10
CURVE_TANGENT_SCALE = 1 << 11
CURVE_EXTENDED = 1 << 14

# Bone-animation flag bit 20 marks a bone whose rotation is a quaternion:
# its base rotation is (x, y, z, w) and its only rotation curve is a
# whole-quaternion curve. Every other bone of the clip keeps the clip's
# rotation mode. Observed on all 12,661 such bones in the title, 12,660 of
# them in clips whose rotation mode is 0x2000.
BONE_QUATERNION_ROTATION = 1 << 20
SEGMENT_SCALE_COMPENSATE = 1 << 23
SCALE_UNIFORM = 1 << 24
SCALE_VOLUME_ONE = 1 << 25
ROTATE_ZERO = 1 << 26
TRANSLATE_ZERO = 1 << 27
SCALE_ONE = SCALE_UNIFORM | SCALE_VOLUME_ONE


class AnimationError(ValueError):
    """A BFRES animation or generated GLB is malformed or incompatible."""


@dataclass(frozen=True, slots=True)
class AnimationCurve:
    target: int
    curve_type: int
    frames: tuple[float, ...]
    keys: tuple[tuple[float, ...], ...]
    scale: float
    offset: float
    extended: bool = False
    tangent_scale: float = 1.0
    tangent_offset: float = 0.0

    @property
    def is_quaternion(self) -> bool:
        return self.curve_type in QUATERNION_CURVES

    def sample_quaternion(self, frame: float) -> tuple[float, float, float, float]:
        if not self.is_quaternion:
            raise AnimationError("scalar animation curve sampled as a quaternion")
        if not self.frames:
            raise AnimationError("animation curve has no keys")
        index = max(0, bisect.bisect_right(self.frames, frame) - 1)
        current = _normalize_quaternion(self.keys[index])
        if index + 1 >= len(self.frames) or frame <= self.frames[index]:
            return current
        next_frame = self.frames[index + 1]
        following = _normalize_quaternion(self.keys[index + 1])
        if next_frame <= self.frames[index]:
            return following
        amount = min(1.0, max(0.0, (frame - self.frames[index]) / (next_frame - self.frames[index])))
        return _interpolate_quaternion(
            current,
            following,
            amount,
            spherical=self.curve_type != CURVE_QUATERNION_NLERP,
        )

    def sample(self, frame: float) -> float:
        if not self.frames:
            raise AnimationError("animation curve has no keys")
        if self.is_quaternion:
            raise AnimationError("quaternion animation curve sampled as a scalar")

        index = bisect.bisect_right(self.frames, frame) - 1
        if index < 0:
            index = 0

        current = self._value(index)
        if index + 1 >= len(self.frames) or frame <= self.frames[index]:
            return current

        next_index = index + 1
        next_frame = self.frames[next_index]
        if next_frame <= self.frames[index]:
            return self._value(next_index)

        amount = min(1.0, max(0.0, (frame - self.frames[index]) / (next_frame - self.frames[index])))
        if self.curve_type == CURVE_CUBIC:
            if self.extended:
                frame_range = next_frame - self.frames[index]
                point0 = current
                point1 = self._value(next_index)
                tangent0 = self._tangent(index)
                tangent1 = self._tangent(next_index)
                coefficient1 = frame_range * tangent0
                difference = point1 - point0
                coefficient2 = (
                    3.0 * difference
                    - frame_range * (2.0 * tangent0 + tangent1)
                )
                coefficient3 = (
                    -2.0 * difference
                    + frame_range * (tangent0 + tangent1)
                )
                return (
                    ((coefficient3 * amount + coefficient2) * amount + coefficient1)
                    * amount
                    + point0
                )
            coefficients = self.keys[index]
            raw = (
                ((coefficients[3] * amount + coefficients[2]) * amount + coefficients[1])
                * amount
                + coefficients[0]
            )
            return raw * self._value_scale() + self.offset
        if self.curve_type in (CURVE_LINEAR, CURVE_BAKED_FLOAT):
            return current + (self._value(next_index) - current) * amount
        return current

    def _value_scale(self) -> float:
        if self.extended:
            return self.scale
        return self.scale if self.scale != 0.0 else 1.0

    def _value(self, index: int) -> float:
        return self.keys[index][0] * self._value_scale() + self.offset

    def _tangent(self, index: int) -> float:
        return (
            self.keys[index][1] * self.tangent_scale
            + self.tangent_offset
        )


@dataclass(frozen=True, slots=True)
class BoneAnimation:
    name: str
    flags: int
    base_scale: tuple[float, float, float] | None
    base_rotation: tuple[float, float, float, float] | None
    base_translation: tuple[float, float, float] | None
    curves: tuple[AnimationCurve, ...]

    @property
    def quaternion_rotation(self) -> bool:
        return bool(self.flags & BONE_QUATERNION_ROTATION)


@dataclass(frozen=True, slots=True)
class SkeletalAnimation:
    name: str
    frame_count: int
    loop: bool
    baked: bool
    scale_mode: str
    rotation_mode: str
    bones: tuple[BoneAnimation, ...]


@dataclass(frozen=True, slots=True)
class SkeletonBone:
    name: str
    parent_index: int | None
    flags: int
    scale: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    translation: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ModelSkeleton:
    name: str
    scale_mode: str
    rotation_mode: str
    bones: tuple[SkeletonBone, ...]


@dataclass(frozen=True, slots=True)
class GlbAnimationReport:
    output: Path
    animation: str
    frame_count: int
    matched_bones: int


@dataclass(frozen=True, slots=True)
class GlbRigReport:
    output: Path
    original_joints: int
    promoted_joints: int
    total_joints: int


class _Reader:
    def __init__(self, data: bytes):
        self.data = data

    def checked(self, offset: int, size: int, label: str) -> int:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise AnimationError(f"{label} range is outside the BFRES file")
        return offset

    def unpack(self, fmt: str, offset: int, label: str):
        size = struct.calcsize(fmt)
        self.checked(offset, size, label)
        return struct.unpack_from(fmt, self.data, offset)

    def u8(self, offset: int, label: str) -> int:
        self.checked(offset, 1, label)
        return self.data[offset]

    def u16(self, offset: int, label: str) -> int:
        return self.unpack("<H", offset, label)[0]

    def i16(self, offset: int, label: str) -> int:
        return self.unpack("<h", offset, label)[0]

    def u32(self, offset: int, label: str) -> int:
        return self.unpack("<I", offset, label)[0]

    def i32(self, offset: int, label: str) -> int:
        return self.unpack("<i", offset, label)[0]

    def u64(self, offset: int, label: str) -> int:
        return self.unpack("<Q", offset, label)[0]

    def f32(self, offset: int, label: str) -> float:
        return self.unpack("<f", offset, label)[0]

    def string(self, pointer: int, label: str) -> str:
        if pointer == 0:
            return ""
        length = self.u16(pointer, f"{label} length")
        start = self.checked(pointer + 2, length, label)
        try:
            return self.data[start:start + length].decode("utf-8")
        except UnicodeDecodeError as error:
            raise AnimationError(f"{label} is not valid UTF-8") from error


def _finite(values: tuple[float, ...], label: str) -> tuple[float, ...]:
    if not all(math.isfinite(value) for value in values):
        raise AnimationError(f"{label} contains a non-finite value")
    return values


def _read_vector(reader: _Reader, offset: int, count: int, label: str) -> tuple[float, ...]:
    return _finite(reader.unpack("<" + "f" * count, offset, label), label)


def _read_curve(reader: _Reader, offset: int, major: int) -> AnimationCurve:
    reader.checked(offset, CURVE_SIZE, "animation curve")
    frame_pointer = reader.u64(offset, "curve frame pointer")
    key_pointer = reader.u64(offset + 8, "curve key pointer")
    flags = reader.u16(offset + 16, "curve flags")
    key_count = reader.u16(offset + 18, "curve key count")
    target = reader.u32(offset + 20, "curve target")
    if target not in SUPPORTED_TARGETS:
        raise AnimationError(f"unsupported skeletal animation target {target:#x}")
    if key_count == 0 or key_count > MAX_KEYS:
        raise AnimationError(f"invalid animation curve key count {key_count}")
    if frame_pointer == 0 or key_pointer == 0:
        raise AnimationError("animation curve has a null frame or key pointer")

    curve_type = flags & CURVE_TYPE_MASK
    if curve_type not in (CURVE_CUBIC, CURVE_LINEAR, CURVE_BAKED_FLOAT, *QUATERNION_CURVES):
        raise AnimationError(f"unsupported skeletal animation curve type {curve_type:#x}")
    if curve_type in QUATERNION_CURVES and target != ROTATE_X:
        raise AnimationError(f"quaternion animation curve targets {target:#x}, expected {ROTATE_X:#x}")
    extended = bool(flags & CURVE_EXTENDED)
    interval_scaled = bool(flags & CURVE_INTERVAL_SCALE)
    separate_tangent_scale = bool(flags & CURVE_TANGENT_SCALE)
    start_frame, end_frame = _finite(
        reader.unpack("<2f", offset + 24, "curve frame range"),
        "curve frame range",
    )
    if start_frame > end_frame:
        raise AnimationError("animation curve has an invalid frame range")

    frame_type = flags & 0x03
    if frame_type == 0:
        raw_frames = reader.unpack("<" + "f" * key_count, frame_pointer, "curve frames")
    elif frame_type == 1 and extended and interval_scaled:
        # Interval-scaled 16-bit frames are unsigned offsets from the start
        # frame. Read as signed, every frame past half the range wraps
        # negative: a cow clip ending at frame 1100 with a 1/32 interval
        # stores 35200, which signed reads as -30336. Unsigned is the only
        # reading under which the last key lands on the curve's end frame.
        raw_frames = reader.unpack("<" + "H" * key_count, frame_pointer, "curve interval frames")
    elif frame_type == 1:
        raw_frames = tuple(
            reader.i16(frame_pointer + index * 2, "curve fixed-point frame")
            for index in range(key_count)
        )
    elif frame_type == 2:
        reader.checked(frame_pointer, key_count, "curve byte frames")
        raw_frames = tuple(reader.data[frame_pointer:frame_pointer + key_count])
    else:
        raise AnimationError(f"unsupported animation frame type {frame_type}")

    if extended and interval_scaled and frame_type != 0:
        interval_scale = reader.f32(offset + 32, "curve frame interval scale")
        if not math.isfinite(interval_scale) or interval_scale <= 0.0:
            raise AnimationError("animation curve has an invalid frame interval scale")
        frames = tuple(
            float(value) * interval_scale + start_frame
            for value in raw_frames
        )
    elif frame_type == 1:
        frames = tuple(float(value) / 32.0 for value in raw_frames)
    else:
        frames = tuple(float(value) for value in raw_frames)
    frames = _finite(tuple(float(value) for value in frames), "curve frames")
    if any(left > right for left, right in zip(frames, frames[1:])):
        raise AnimationError("animation curve frames are not ordered")

    key_type = flags & 0x0C
    if curve_type in QUATERNION_CURVES:
        # Every key is one complete float quaternion (x, y, z, w). The title
        # stores no quantized quaternion curves, so other key types are
        # rejected rather than guessed.
        if key_type != 0 or flags & CURVE_TANGENT_SCALE:
            raise AnimationError(f"unsupported quaternion animation curve flags {flags:#x}")
        keys = tuple(
            _finite(
                reader.unpack("<4f", key_pointer + index * 16, "curve quaternion key"),
                "curve quaternion keys",
            )
            for index in range(key_count)
        )
        for key in keys:
            _normalize_quaternion(key)
        return AnimationCurve(
            target=target,
            curve_type=curve_type,
            frames=frames,
            keys=keys,
            scale=1.0,
            offset=0.0,
            extended=extended,
        )
    elements = (
        2 if extended and curve_type == CURVE_CUBIC
        else 1 if extended
        else 4 if curve_type == CURVE_CUBIC
        else 2 if curve_type == CURVE_LINEAR
        else 1
    )
    tangent_scale = 1.0
    tangent_offset = 0.0
    if separate_tangent_scale:
        if not extended or curve_type != CURVE_CUBIC or key_type == 0:
            raise AnimationError("animation curve has an invalid separate tangent scale")
        tangent_scale, tangent_offset = _finite(
            reader.unpack("<2f", key_pointer, "curve tangent transform"),
            "curve tangent transform",
        )
        key_pointer += 8

    if extended and curve_type == CURVE_CUBIC and key_type in (4, 8):
        # Both lanes of a quantized extended Hermite key are unsigned: the
        # curve's offset is the minimum of the range, not its midpoint. Read
        # as signed, every point above half the range wraps by
        # 65536 * scale (256 * scale for int8), which turned rest-pose
        # rotations of 0 into -3*pi in the title's clips. Across the Link,
        # Epona, Stalfos and Zelda clips, unsigned points are the only
        # reading whose decoded tangents agree with the points' finite
        # differences.
        record_format = "<HH" if key_type == 4 else "<BB"
        record_size = struct.calcsize(record_format)
        keys = tuple(
            _finite(
                tuple(
                    float(value)
                    for value in reader.unpack(
                        record_format,
                        key_pointer + index * record_size,
                        "curve Hermite key",
                    )
                ),
                "curve keys",
            )
            for index in range(key_count)
        )
    else:
        value_count = key_count * elements
        if key_type == 0:
            flat_keys = reader.unpack(
                "<" + "f" * value_count,
                key_pointer,
                "curve float keys",
            )
        elif key_type == 4:
            flat_keys = reader.unpack(
                "<" + "h" * value_count,
                key_pointer,
                "curve int16 keys",
            )
        elif key_type == 8:
            reader.checked(key_pointer, value_count, "curve int8 keys")
            flat_keys = struct.unpack_from(
                "<" + "b" * value_count,
                reader.data,
                key_pointer,
            )
        else:
            raise AnimationError(f"unsupported animation key type {key_type:#x}")
        keys = tuple(
            _finite(
                tuple(float(value) for value in flat_keys[index:index + elements]),
                "curve keys",
            )
            for index in range(0, value_count, elements)
        )

    value_offset = 36 if major >= 10 else 32
    scale, value_bias, _delta = _finite(
        reader.unpack("<3f", offset + value_offset, "curve value transform"),
        "curve value transform",
    )
    if extended and key_type == 0:
        scale = 1.0
        value_bias = 0.0
    if extended and not separate_tangent_scale:
        tangent_scale = scale
        tangent_offset = value_bias
    return AnimationCurve(
        target=target,
        curve_type=curve_type,
        frames=frames,
        keys=keys,
        scale=scale,
        offset=value_bias,
        extended=extended,
        tangent_scale=tangent_scale,
        tangent_offset=tangent_offset,
    )


def _read_bone_animation(reader: _Reader, offset: int, major: int) -> BoneAnimation:
    reader.checked(offset, BONE_ANIM_SIZE, "bone animation")
    name = reader.string(reader.u64(offset, "bone animation name pointer"), "bone animation name")
    if not name:
        raise AnimationError("bone animation has an empty name")
    curve_pointer = reader.u64(offset + 8, "bone curve pointer")
    base_pointer = reader.u64(offset + 16, "bone base-data pointer")
    flags = reader.u32(offset + 40, "bone animation flags")
    curve_count = reader.u8(offset + 46, "bone curve count")

    cursor = base_pointer
    base_scale = None
    base_rotation = None
    base_translation = None
    if flags & (1 << 3):
        base_scale = _read_vector(reader, cursor, 3, f"{name} base scale")
        cursor += 12
    if flags & (1 << 4):
        base_rotation = _read_vector(reader, cursor, 4, f"{name} base rotation")
        cursor += 16
    if flags & (1 << 5):
        base_translation = _read_vector(reader, cursor, 3, f"{name} base translation")

    if curve_count and curve_pointer == 0:
        raise AnimationError(f"{name} has curves but no curve array")
    curves = tuple(
        _read_curve(reader, curve_pointer + index * CURVE_SIZE, major)
        for index in range(curve_count)
    )
    targets = [curve.target for curve in curves]
    if len(set(targets)) != len(targets):
        raise AnimationError(f"{name} animates the same transform component more than once")
    quaternion_bone = bool(flags & BONE_QUATERNION_ROTATION)
    for curve in curves:
        if curve.is_quaternion and not quaternion_bone:
            raise AnimationError(f"{name} has a quaternion curve but is not a quaternion bone")
        if (
            quaternion_bone
            and not curve.is_quaternion
            and ROTATE_X <= curve.target <= ROTATE_W
        ):
            raise AnimationError(f"quaternion bone {name} has a scalar rotation curve")
    return BoneAnimation(
        name=name,
        flags=flags,
        base_scale=base_scale,
        base_rotation=base_rotation,
        base_translation=base_translation,
        curves=curves,
    )


def read_skeletal_animations(data: bytes) -> tuple[SkeletalAnimation, ...]:
    """Parse all FSKA clips from an Ocarina of Time Remake BFRES resource."""
    reader = _Reader(data)
    reader.checked(0, 0x68, "BFRES header")
    if data[:8] != FRES_MAGIC:
        raise AnimationError("expected a BFRES resource")
    major = reader.u8(10, "BFRES major version")
    minor = reader.u8(9, "BFRES minor version")
    if major not in (9, 10):
        raise AnimationError(f"unsupported BFRES animation version {major}.{minor}")

    table = 0x58
    animation_array = reader.u64(table, "skeletal animation array")
    dictionary = reader.u64(table + 8, "skeletal animation dictionary")
    if animation_array == 0 and dictionary == 0:
        return ()
    if animation_array == 0 or dictionary == 0:
        raise AnimationError("skeletal animation array and dictionary do not agree")
    reader.checked(dictionary, 8, "skeletal animation dictionary")
    if reader.u32(dictionary, "skeletal animation dictionary header") != 0:
        raise AnimationError("invalid skeletal animation dictionary header")
    animation_count = reader.i32(dictionary + 4, "skeletal animation count")
    if not 0 <= animation_count <= MAX_ANIMATIONS:
        raise AnimationError(f"invalid skeletal animation count {animation_count}")
    reader.checked(dictionary + 8, (animation_count + 1) * 16, "skeletal animation dictionary")
    reader.checked(animation_array, animation_count * FSKA_SIZE, "skeletal animation array")

    animations: list[SkeletalAnimation] = []
    for animation_index in range(animation_count):
        offset = animation_array + animation_index * FSKA_SIZE
        if data[offset:offset + 4] != b"FSKA":
            raise AnimationError(f"expected FSKA at animation {animation_index}")
        flags = reader.u32(offset + 4, "skeletal animation flags")
        name = reader.string(
            reader.u64(offset + 8, "skeletal animation name pointer"),
            "skeletal animation name",
        )
        if not name:
            raise AnimationError(f"skeletal animation {animation_index} has an empty name")
        bone_array = reader.u64(offset + 40, "bone animation array")
        frame_count = reader.i32(offset + 64, "skeletal animation frame count")
        bone_count = reader.u16(offset + 76, "bone animation count")
        if not 0 <= frame_count <= MAX_KEYS:
            raise AnimationError(f"{name} has invalid frame count {frame_count}")
        if bone_count > MAX_BONES:
            raise AnimationError(f"{name} has invalid bone count {bone_count}")
        if bone_count and bone_array == 0:
            raise AnimationError(f"{name} has bones but no bone animation array")
        reader.checked(bone_array, bone_count * BONE_ANIM_SIZE, f"{name} bone animations")

        rotate_flag = flags & 0x7000
        if rotate_flag == 0:
            rotation_mode = "quaternion"
        elif rotate_flag == 0x1000:
            rotation_mode = "euler_xyz"
        elif rotate_flag == 0x2000:
            # Euler XYZ bones mixed with quaternion bones, which are marked
            # per bone by BONE_QUATERNION_ROTATION.
            rotation_mode = "mixed"
        else:
            raise AnimationError(f"{name} uses unsupported rotation mode {rotate_flag:#x}")
        scale_modes = {0x000: "none", 0x100: "standard", 0x200: "maya", 0x300: "softimage"}
        scale_mode = scale_modes.get(flags & 0x300)
        if scale_mode is None:
            raise AnimationError(f"{name} uses an unsupported scale mode")

        bones = tuple(
            _read_bone_animation(reader, bone_array + index * BONE_ANIM_SIZE, major)
            for index in range(bone_count)
        )
        bone_names = [bone.name for bone in bones]
        if len(set(bone_names)) != len(bone_names):
            raise AnimationError(f"{name} contains duplicate bone animation names")
        animations.append(
            SkeletalAnimation(
                name=name,
                frame_count=frame_count,
                loop=bool(flags & (1 << 2)),
                baked=bool(flags & 1),
                scale_mode=scale_mode,
                rotation_mode=rotation_mode,
                bones=bones,
            )
        )
    return tuple(animations)


# BFRES 10.4 top-level dictionaries of the animation kinds the importer does
# not convert (BfresLibrary, Switch/ResFileParser.cs, ResFileParser.Load,
# with the 32 reserved bytes after the model table that version 9 also has).
UNSUPPORTED_ANIMATION_DICTIONARIES = (
    (0x70, "material"),
    (0x80, "bone-visibility"),
    (0x90, "shape"),
    (0xA0, "scene"),
)


def unsupported_animation_counts(data: bytes) -> dict[str, int]:
    """Count the animations of each kind this importer cannot convert."""
    reader = _Reader(data)
    reader.checked(0, 0xA8, "BFRES header")
    if data[:8] != FRES_MAGIC:
        raise AnimationError("expected a BFRES resource")
    counts = {}
    for offset, kind in UNSUPPORTED_ANIMATION_DICTIONARIES:
        dictionary = reader.u64(offset, f"{kind} animation dictionary")
        if dictionary:
            count = reader.i32(dictionary + 4, f"{kind} animation count")
            if count > 0:
                counts[kind] = count
    return counts


def read_model_skeletons(data: bytes) -> tuple[ModelSkeleton, ...]:
    """Parse the skeletons required to evaluate BFRES scale compensation."""
    reader = _Reader(data)
    reader.checked(0, 0x38, "BFRES model header")
    if data[:8] != FRES_MAGIC:
        raise AnimationError("expected a BFRES model resource")
    major = reader.u8(10, "BFRES major version")
    minor = reader.u8(9, "BFRES minor version")
    if major != 10:
        raise AnimationError(f"unsupported BFRES model version {major}.{minor}")

    model_array = reader.u64(0x28, "model array")
    model_dictionary = reader.u64(0x30, "model dictionary")
    if model_array == 0 or model_dictionary == 0:
        raise AnimationError("BFRES resource contains no models")
    reader.checked(model_dictionary, 8, "model dictionary")
    if reader.u32(model_dictionary, "model dictionary header") != 0:
        raise AnimationError("invalid model dictionary header")
    model_count = reader.i32(model_dictionary + 4, "model count")
    if not 0 < model_count <= MAX_ANIMATIONS:
        raise AnimationError(f"invalid model count {model_count}")
    reader.checked(model_dictionary + 8, (model_count + 1) * 16, "model dictionary")
    reader.checked(model_array, model_count * 0x78, "model array")

    skeletons: list[ModelSkeleton] = []
    for model_index in range(model_count):
        model = model_array + model_index * 0x78
        if data[model:model + 4] != b"FMDL":
            raise AnimationError(f"expected FMDL at model {model_index}")
        model_name = reader.string(
            reader.u64(model + 8, "model name pointer"),
            "model name",
        )
        skeleton = reader.u64(model + 0x18, "model skeleton pointer")
        reader.checked(skeleton, 0x40, f"{model_name} skeleton")
        if data[skeleton:skeleton + 4] != b"FSKL":
            raise AnimationError(f"expected FSKL for model {model_name!r}")
        skeleton_flags = reader.u32(skeleton + 4, "skeleton flags")
        bone_array = reader.u64(skeleton + 0x10, "skeleton bone array")
        bone_count = reader.u16(skeleton + 0x38, "skeleton bone count")
        if not 0 < bone_count <= MAX_BONES:
            raise AnimationError(f"{model_name} has invalid skeleton bone count {bone_count}")
        bone_size = 0x58
        field_offset = 0x20
        reader.checked(bone_array, bone_count * bone_size, f"{model_name} bones")

        rotate_flag = skeleton_flags & 0x7000
        if rotate_flag == 0:
            rotation_mode = "quaternion"
        elif rotate_flag == 0x1000:
            rotation_mode = "euler_xyz"
        else:
            raise AnimationError(
                f"{model_name} uses unsupported skeleton rotation mode {rotate_flag:#x}"
            )
        scale_modes = {0x000: "none", 0x100: "standard", 0x200: "maya", 0x300: "softimage"}
        scale_mode = scale_modes.get(skeleton_flags & 0x300)
        if scale_mode is None:
            raise AnimationError(f"{model_name} uses an unsupported skeleton scale mode")

        bones: list[SkeletonBone] = []
        for bone_index in range(bone_count):
            bone = bone_array + bone_index * bone_size
            fields = bone + field_offset
            name = reader.string(reader.u64(bone, "bone name pointer"), "bone name")
            stored_index = reader.u16(fields, f"{name} bone index")
            parent = reader.u16(fields + 2, f"{name} parent index")
            if stored_index != bone_index:
                raise AnimationError(
                    f"{model_name} bone {name!r} has index {stored_index}, expected {bone_index}"
                )
            parent_index = None if parent == 0xFFFF else parent
            if parent_index is not None and parent_index >= bone_index:
                raise AnimationError(f"{model_name} bone {name!r} has an invalid parent")
            flags = reader.u32(fields + 12, f"{name} bone flags")
            scale = _read_vector(reader, fields + 16, 3, f"{name} scale")
            rotation = _read_vector(reader, fields + 28, 4, f"{name} rotation")
            translation = _read_vector(reader, fields + 44, 3, f"{name} translation")
            bones.append(
                SkeletonBone(
                    name=name,
                    parent_index=parent_index,
                    flags=flags,
                    scale=scale,
                    rotation=rotation,
                    translation=translation,
                )
            )
        names = [bone.name for bone in bones]
        if len(set(names)) != len(names):
            raise AnimationError(f"{model_name} contains duplicate skeleton bone names")
        skeletons.append(
            ModelSkeleton(
                name=model_name,
                scale_mode=scale_mode,
                rotation_mode=rotation_mode,
                bones=tuple(bones),
            )
        )
    return tuple(skeletons)


def _normalize_quaternion(values: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    length = math.sqrt(sum(value * value for value in values))
    if length <= 1e-12:
        raise AnimationError("animation contains a zero-length quaternion")
    return tuple(value / length for value in values)


def _interpolate_quaternion(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    amount: float,
    *,
    spherical: bool,
) -> tuple[float, float, float, float]:
    """Interpolate along the shorter arc between two unit quaternions."""
    dot = sum(a * b for a, b in zip(left, right))
    if dot < 0.0:
        right = tuple(-value for value in right)
        dot = -dot
    if spherical and dot < 0.9995:
        angle = math.acos(min(1.0, dot))
        sine = math.sin(angle)
        left_weight = math.sin((1.0 - amount) * angle) / sine
        right_weight = math.sin(amount * angle) / sine
    else:
        left_weight = 1.0 - amount
        right_weight = amount
    return _normalize_quaternion(
        tuple(left_weight * a + right_weight * b for a, b in zip(left, right))
    )


def _euler_xyz_rows(x: float, y: float, z: float) -> tuple[tuple[float, float, float], ...]:
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    return (
        (cy * cz, sx * sy * cz - cx * sz, cx * sy * cz + sx * sz),
        (cy * sz, sx * sy * sz + cx * cz, cx * sy * sz - sx * cz),
        (-sy, sx * cy, cx * cy),
    )


def _quaternion_rows(
    quaternion: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float], ...]:
    x, y, z, w = _normalize_quaternion(quaternion)
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
        (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
        (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
    )


def _matrix_quaternion(rows: tuple[tuple[float, float, float], ...]) -> tuple[float, float, float, float]:
    m00, m01, m02 = rows[0]
    m10, m11, m12 = rows[1]
    m20, m21, m22 = rows[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        root = math.sqrt(trace + 1.0) * 2.0
        quaternion = ((m21 - m12) / root, (m02 - m20) / root, (m10 - m01) / root, 0.25 * root)
    elif m00 > m11 and m00 > m22:
        root = math.sqrt(max(0.0, 1.0 + m00 - m11 - m22)) * 2.0
        quaternion = (0.25 * root, (m01 + m10) / root, (m02 + m20) / root, (m21 - m12) / root)
    elif m11 > m22:
        root = math.sqrt(max(0.0, 1.0 + m11 - m00 - m22)) * 2.0
        quaternion = ((m01 + m10) / root, 0.25 * root, (m12 + m21) / root, (m02 - m20) / root)
    else:
        root = math.sqrt(max(0.0, 1.0 + m22 - m00 - m11)) * 2.0
        quaternion = ((m02 + m20) / root, (m12 + m21) / root, 0.25 * root, (m10 - m01) / root)
    return _normalize_quaternion(quaternion)


def _euler_xyz_quaternion(x: float, y: float, z: float) -> tuple[float, float, float, float]:
    return _matrix_quaternion(_euler_xyz_rows(x, y, z))


def _quaternion_euler_xyz(quaternion: tuple[float, float, float, float]) -> tuple[float, float, float]:
    rows = _quaternion_rows(quaternion)
    angle_y = math.asin(min(1.0, max(-1.0, -rows[2][0])))
    if abs(math.cos(angle_y)) > 1e-7:
        angle_x = math.atan2(rows[2][1], rows[2][2])
        angle_z = math.atan2(rows[1][0], rows[0][0])
    else:
        angle_x = math.atan2(-rows[1][2], rows[1][1])
        angle_z = 0.0
    return angle_x, angle_y, angle_z


def _matrix_multiply(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        sum(left[row * 4 + inner] * right[inner * 4 + column] for inner in range(4))
        for row in range(4)
        for column in range(4)
    )


def _scale_matrix(scale: tuple[float, float, float]) -> tuple[float, ...]:
    return (
        scale[0], 0.0, 0.0, 0.0,
        0.0, scale[1], 0.0, 0.0,
        0.0, 0.0, scale[2], 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def _rotation_translation_matrix(
    rotation_mode: str,
    rotation: tuple[float, float, float, float],
    translation: tuple[float, float, float],
) -> tuple[float, ...]:
    rows = (
        _euler_xyz_rows(rotation[0], rotation[1], rotation[2])
        if rotation_mode == "euler_xyz"
        else _quaternion_rows(rotation)
    )
    return (
        rows[0][0], rows[0][1], rows[0][2], translation[0],
        rows[1][0], rows[1][1], rows[1][2], translation[1],
        rows[2][0], rows[2][1], rows[2][2], translation[2],
        0.0, 0.0, 0.0, 1.0,
    )


def _matrix_translation(matrix: tuple[float, ...]) -> tuple[float, float, float]:
    return matrix[3], matrix[7], matrix[11]


def _matrix_with_translation(
    matrix: tuple[float, ...],
    translation: tuple[float, float, float],
) -> tuple[float, ...]:
    values = list(matrix)
    values[3], values[7], values[11] = translation
    return tuple(values)


def _affine_inverse(matrix: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c = matrix[0], matrix[1], matrix[2]
    d, e, f = matrix[4], matrix[5], matrix[6]
    g, h, i = matrix[8], matrix[9], matrix[10]
    cofactor_a = e * i - f * h
    cofactor_b = -(d * i - f * g)
    cofactor_c = d * h - e * g
    determinant = a * cofactor_a + b * cofactor_b + c * cofactor_c
    if abs(determinant) < 1e-20:
        raise AnimationError("skeleton animation produced a singular transform")
    inverse = 1.0 / determinant
    linear = (
        cofactor_a * inverse,
        -(b * i - c * h) * inverse,
        (b * f - c * e) * inverse,
        cofactor_b * inverse,
        (a * i - c * g) * inverse,
        -(a * f - c * d) * inverse,
        cofactor_c * inverse,
        -(a * h - b * g) * inverse,
        (a * e - b * d) * inverse,
    )
    translation = _matrix_translation(matrix)
    inverse_translation = tuple(
        -sum(linear[row * 3 + column] * translation[column] for column in range(3))
        for row in range(3)
    )
    return (
        linear[0], linear[1], linear[2], inverse_translation[0],
        linear[3], linear[4], linear[5], inverse_translation[1],
        linear[6], linear[7], linear[8], inverse_translation[2],
        0.0, 0.0, 0.0, 1.0,
    )


def _matrix_trs(
    matrix: tuple[float, ...],
    name: str,
) -> tuple[list[float], list[float], list[float]]:
    columns = [
        [matrix[0], matrix[4], matrix[8]],
        [matrix[1], matrix[5], matrix[9]],
        [matrix[2], matrix[6], matrix[10]],
    ]
    scale = [math.sqrt(sum(value * value for value in column)) for column in columns]
    if any(value <= 1e-12 for value in scale):
        raise AnimationError(f"bone {name!r} has a singular animation transform")
    normalized = [
        [component / scale[index] for component in column]
        for index, column in enumerate(columns)
    ]
    determinant = (
        normalized[0][0] * (normalized[1][1] * normalized[2][2] - normalized[1][2] * normalized[2][1])
        - normalized[1][0] * (normalized[0][1] * normalized[2][2] - normalized[0][2] * normalized[2][1])
        + normalized[2][0] * (normalized[0][1] * normalized[1][2] - normalized[0][2] * normalized[1][1])
    )
    if determinant < 0.0:
        scale[0] = -scale[0]
        normalized[0] = [-value for value in normalized[0]]
    for left in range(3):
        for right in range(left + 1, 3):
            dot = sum(normalized[left][axis] * normalized[right][axis] for axis in range(3))
            if abs(dot) > 2e-4:
                raise AnimationError(
                    f"bone {name!r} has scale-compensation shear that glTF TRS cannot represent"
                )
    rows = tuple(
        tuple(normalized[column][row] for column in range(3))
        for row in range(3)
    )
    rotation = list(_matrix_quaternion(rows))
    translation = [matrix[3], matrix[7], matrix[11]]
    return translation, rotation, scale


def _node_trs(node: dict, name: str) -> tuple[list[float], list[float], list[float]]:
    if "matrix" not in node:
        return (
            list(node.get("translation", (0.0, 0.0, 0.0))),
            list(node.get("rotation", (0.0, 0.0, 0.0, 1.0))),
            list(node.get("scale", (1.0, 1.0, 1.0))),
        )
    if any(key in node for key in ("translation", "rotation", "scale")):
        raise AnimationError(f"glTF node {name!r} has both matrix and TRS transforms")
    matrix = node["matrix"]
    if not isinstance(matrix, list) or len(matrix) != 16:
        raise AnimationError(f"glTF node {name!r} has an invalid matrix")
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in matrix):
        raise AnimationError(f"glTF node {name!r} has a non-finite matrix")
    row_major = (
        float(matrix[0]), float(matrix[4]), float(matrix[8]), float(matrix[12]),
        float(matrix[1]), float(matrix[5]), float(matrix[9]), float(matrix[13]),
        float(matrix[2]), float(matrix[6]), float(matrix[10]), float(matrix[14]),
        float(matrix[3]), float(matrix[7]), float(matrix[11]), float(matrix[15]),
    )
    return _matrix_trs(row_major, name)


def _read_glb(path: Path, *, allow_missing_binary: bool = False) -> tuple[dict, bytearray]:
    data = path.read_bytes()
    if len(data) < 12:
        raise AnimationError("converted glTF is too small")
    magic, version, declared_size = struct.unpack_from("<4sII", data, 0)
    if magic != GLB_MAGIC or version != 2 or declared_size != len(data):
        raise AnimationError("converted model is not a valid glTF 2 GLB")

    json_data = None
    binary_data = None
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise AnimationError("truncated GLB chunk header")
        chunk_size, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if offset + chunk_size > len(data):
            raise AnimationError("truncated GLB chunk")
        chunk = data[offset:offset + chunk_size]
        offset += chunk_size
        if chunk_type == JSON_CHUNK:
            if json_data is not None:
                raise AnimationError("GLB contains multiple JSON chunks")
            json_data = chunk
        elif chunk_type == BIN_CHUNK:
            if binary_data is not None:
                raise AnimationError("GLB contains multiple binary chunks")
            binary_data = chunk
        else:
            raise AnimationError(f"GLB contains unsupported chunk {chunk_type!r}")
    if binary_data is None and allow_missing_binary:
        binary_data = b""
    if json_data is None or binary_data is None:
        raise AnimationError("GLB is missing its JSON or binary chunk")
    try:
        document = json.loads(json_data.rstrip(b" \0").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AnimationError("GLB JSON is invalid") from error
    return document, bytearray(binary_data)


def _write_glb(path: Path, document: dict, binary: bytearray) -> None:
    json_data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_data += b" " * (-len(json_data) % 4)
    binary += b"\0" * (-len(binary) % 4)
    total_size = 12 + 8 + len(json_data) + 8 + len(binary)
    output = bytearray(struct.pack("<4sII", GLB_MAGIC, 2, total_size))
    output.extend(struct.pack("<I4s", len(json_data), JSON_CHUNK))
    output.extend(json_data)
    output.extend(struct.pack("<I4s", len(binary), BIN_CHUNK))
    output.extend(binary)
    path.write_bytes(output)


def _append_float_accessor(
    document: dict,
    binary: bytearray,
    values: list[tuple[float, ...]],
    accessor_type: str,
    *,
    bounds: bool = False,
) -> int:
    if not values:
        raise AnimationError("cannot create an empty glTF accessor")
    width = len(values[0])
    if any(len(value) != width for value in values):
        raise AnimationError("glTF animation values have inconsistent widths")
    if not all(math.isfinite(component) for value in values for component in value):
        raise AnimationError("glTF animation values contain a non-finite component")

    binary.extend(b"\0" * (-len(binary) % 4))
    start = len(binary)
    flat = [component for value in values for component in value]
    binary.extend(struct.pack("<" + "f" * len(flat), *flat))
    buffer_views = document.setdefault("bufferViews", [])
    view_index = len(buffer_views)
    buffer_views.append(
        {
            "buffer": 0,
            "byteOffset": start,
            "byteLength": len(flat) * 4,
        }
    )
    accessors = document.setdefault("accessors", [])
    accessor: dict = {
        "bufferView": view_index,
        "componentType": 5126,
        "count": len(values),
        "type": accessor_type,
    }
    if bounds:
        accessor["min"] = [min(value[index] for value in values) for index in range(width)]
        accessor["max"] = [max(value[index] for value in values) for index in range(width)]
    accessor_index = len(accessors)
    accessors.append(accessor)
    return accessor_index


def _read_float_accessor(
    document: dict,
    binary: bytearray,
    accessor_index: int,
    expected_type: str,
    width: int,
) -> list[tuple[float, ...]]:
    accessors = document.get("accessors")
    views = document.get("bufferViews")
    if not isinstance(accessors, list) or not 0 <= accessor_index < len(accessors):
        raise AnimationError("glTF skin references an invalid accessor")
    accessor = accessors[accessor_index]
    if (
        accessor.get("componentType") != 5126
        or accessor.get("type") != expected_type
        or accessor.get("normalized")
        or "sparse" in accessor
    ):
        raise AnimationError(f"glTF skin requires a plain float {expected_type} accessor")
    view_index = accessor.get("bufferView")
    if not isinstance(views, list) or not isinstance(view_index, int) or not 0 <= view_index < len(views):
        raise AnimationError("glTF skin accessor references an invalid buffer view")
    view = views[view_index]
    if view.get("buffer", 0) != 0:
        raise AnimationError("converted GLB skin accessor must use buffer 0")
    count = accessor.get("count")
    if not isinstance(count, int) or count < 0:
        raise AnimationError("glTF skin accessor has an invalid count")
    element_size = width * 4
    stride = view.get("byteStride", element_size)
    if not isinstance(stride, int) or stride < element_size:
        raise AnimationError("glTF skin accessor has an invalid byte stride")
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    if not isinstance(start, int) or start < 0:
        raise AnimationError("glTF skin accessor has an invalid byte offset")
    values = []
    for index in range(count):
        offset = start + index * stride
        if offset + element_size > len(binary):
            raise AnimationError("glTF skin accessor is out of bounds")
        values.append(struct.unpack_from("<" + "f" * width, binary, offset))
    return values


def glb_has_meshes(path: str | Path) -> bool:
    document, _ = _read_glb(Path(path), allow_missing_binary=True)
    return bool(document.get("meshes"))


def prepare_skeleton_glb(
    model: str | Path,
    output: str | Path,
    skeleton: ModelSkeleton,
) -> GlbRigReport:
    """Promote every FSKL node into the GLB skin without adding animation."""
    model = Path(model)
    output = Path(output)
    # A model without meshes (Dm_Locator, cubemap probes) converts to a GLB
    # with no buffer and no skin; it still gets an armature here.
    document, binary = _read_glb(model, allow_missing_binary=True)
    if not document.get("buffers") and not binary:
        document["buffers"] = [{"byteLength": 0}]
    if len(document.get("buffers", [])) != 1:
        raise AnimationError("converted GLB must contain exactly one buffer")
    declared_binary_size = document["buffers"][0].get("byteLength")
    if not isinstance(declared_binary_size, int) or not 0 <= declared_binary_size <= len(binary):
        raise AnimationError("converted GLB declares an invalid binary buffer size")
    del binary[declared_binary_size:]

    nodes = document.get("nodes")
    skins = document.setdefault("skins", [])
    if not isinstance(nodes, list) or not isinstance(skins, list):
        raise AnimationError("converted GLB contains no skeleton nodes")
    node_indices: dict[str, int] = {}
    for index, node in enumerate(nodes):
        name = node.get("name")
        if not isinstance(name, str):
            continue
        if name in node_indices:
            raise AnimationError(f"converted GLB contains duplicate node name {name!r}")
        node_indices[name] = index
    missing_nodes = tuple(bone.name for bone in skeleton.bones if bone.name not in node_indices)
    if missing_nodes:
        detail = ", ".join(missing_nodes[:5])
        if len(missing_nodes) > 5:
            detail += f", and {len(missing_nodes) - 5} more"
        raise AnimationError(
            f"converted GLB is missing bones from model {skeleton.name!r}: {detail}"
        )

    skeleton_names = {bone.name for bone in skeleton.bones}
    candidates = []
    for skin in skins:
        joints = skin.get("joints")
        if not isinstance(joints, list) or not joints:
            continue
        joint_names = []
        valid = True
        for joint in joints:
            if not isinstance(joint, int) or not 0 <= joint < len(nodes):
                valid = False
                break
            joint_names.append(nodes[joint].get("name"))
        if valid and all(name in skeleton_names for name in joint_names):
            candidates.append(skin)
    if not skins:
        # No mesh is skinned: a skin over the root bone alone becomes the
        # armature, and every other FSKL node is promoted below. Its inverse
        # bind matrix is never used for deformation.
        root = node_indices[skeleton.bones[0].name]
        skins.append({"name": skeleton.name, "joints": [root]})
        candidates = [skins[0]]
    if len(candidates) != 1:
        raise AnimationError(
            f"expected one GLB skin for model {skeleton.name!r}, found {len(candidates)}"
        )
    skin = candidates[0]
    original_joints = list(skin["joints"])
    joint_set = set(original_joints)
    promoted = [
        node_indices[bone.name]
        for bone in skeleton.bones
        if node_indices[bone.name] not in joint_set
    ]

    accessor_index = skin.get("inverseBindMatrices")
    if accessor_index is None:
        inverse_binds = [
            (1.0, 0.0, 0.0, 0.0,
             0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             0.0, 0.0, 0.0, 1.0)
            for _ in original_joints
        ]
    elif isinstance(accessor_index, int):
        inverse_binds = _read_float_accessor(
            document,
            binary,
            accessor_index,
            "MAT4",
            16,
        )
    else:
        raise AnimationError("glTF skin has an invalid inverse-bind accessor")
    if len(inverse_binds) != len(original_joints):
        raise AnimationError("glTF skin joint and inverse-bind counts do not match")
    identity = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    inverse_binds.extend(identity for _ in promoted)
    skin["joints"] = original_joints + promoted
    skin["inverseBindMatrices"] = _append_float_accessor(
        document,
        binary,
        inverse_binds,
        "MAT4",
    )
    document["buffers"][0]["byteLength"] = len(binary)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_glb(output, document, binary)
    return GlbRigReport(
        output=output,
        original_joints=len(original_joints),
        promoted_joints=len(promoted),
        total_joints=len(skin["joints"]),
    )


def rotation_modes_compatible(animation: SkeletalAnimation, skeleton: ModelSkeleton) -> bool:
    """Whether a clip's rotation values can be read in the skeleton's mode.

    A mixed clip's non-quaternion bones are Euler XYZ, so it needs an Euler
    skeleton; quaternion bones carry their own mode in any clip.
    """
    if animation.rotation_mode == "mixed":
        return skeleton.rotation_mode == "euler_xyz"
    return animation.rotation_mode == skeleton.rotation_mode


def _sample_authored_bone(
    skeleton_bone: SkeletonBone,
    bone_animation: BoneAnimation | None,
    rotation_mode: str,
    frame: float,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float],
    str,
]:
    """Return the bone's scale, rotation, translation and rotation mode.

    The mode is the skeleton's unless the animation marks the bone as a
    quaternion bone, whose rotation then comes from its own quaternion base
    value and curve.
    """
    scale = (
        [1.0, 1.0, 1.0]
        if skeleton_bone.flags & SCALE_ONE == SCALE_ONE
        else list(skeleton_bone.scale)
    )
    rotation = (
        [0.0, 0.0, 0.0, 1.0]
        if skeleton_bone.flags & ROTATE_ZERO
        else list(skeleton_bone.rotation)
    )
    translation = (
        [0.0, 0.0, 0.0]
        if skeleton_bone.flags & TRANSLATE_ZERO
        else list(skeleton_bone.translation)
    )
    if bone_animation is None:
        return tuple(scale), tuple(rotation), tuple(translation), rotation_mode

    curves = {curve.target: curve for curve in bone_animation.curves}
    if bone_animation.base_scale is not None:
        scale = list(bone_animation.base_scale)
    elif bone_animation.flags & SCALE_ONE == SCALE_ONE:
        scale = [1.0, 1.0, 1.0]
    if bone_animation.quaternion_rotation:
        if bone_animation.base_rotation is not None:
            quaternion = bone_animation.base_rotation
        elif bone_animation.flags & ROTATE_ZERO:
            quaternion = (0.0, 0.0, 0.0, 1.0)
        elif rotation_mode == "euler_xyz":
            quaternion = _euler_xyz_quaternion(*rotation[:3])
        else:
            quaternion = tuple(rotation)
        rotation_curve = curves.get(ROTATE_X)
        if rotation_curve is not None:
            quaternion = rotation_curve.sample_quaternion(frame)
        rotation = list(_normalize_quaternion(tuple(quaternion)))
        rotation_mode = "quaternion"
    elif bone_animation.base_rotation is not None:
        rotation = list(bone_animation.base_rotation)
    elif bone_animation.flags & ROTATE_ZERO:
        rotation = [0.0, 0.0, 0.0, 1.0]
    if bone_animation.base_translation is not None:
        translation = list(bone_animation.base_translation)
    elif bone_animation.flags & TRANSLATE_ZERO:
        translation = [0.0, 0.0, 0.0]

    for target, curve in curves.items():
        if curve.is_quaternion:
            continue
        value = curve.sample(frame)
        if target in (SCALE_X, SCALE_Y, SCALE_Z):
            scale[(target - SCALE_X) // 4] = value
        elif target in (TRANSLATE_X, TRANSLATE_Y, TRANSLATE_Z):
            translation[(target - TRANSLATE_X) // 4] = value
        elif target in (ROTATE_X, ROTATE_Y, ROTATE_Z):
            rotation[(target - ROTATE_X) // 4] = value
        elif target == ROTATE_W:
            if rotation_mode != "quaternion":
                raise AnimationError(f"{bone_animation.name} animates quaternion W in Euler mode")
            rotation[3] = value
    return tuple(scale), tuple(rotation), tuple(translation), rotation_mode


def _compensates_segment_scale(bone: SkeletonBone, bone_animation: BoneAnimation | None) -> bool:
    flags = bone_animation.flags if bone_animation is not None else bone.flags
    return bool(flags & SEGMENT_SCALE_COMPENSATE)


@dataclass(frozen=True, slots=True)
class BonePose:
    """One bone's authored local transform at one frame.

    `rotation_translation` is the row-major 4x4 rotation and unscaled
    translation; `scale` is applied after it. In Maya mode a compensating
    bone ignores its parent's scale except on its translation.
    """

    rotation_translation: tuple[float, ...]
    scale: tuple[float, float, float]
    compensates_parent_scale: bool


def sample_bone_poses(
    skeleton: ModelSkeleton,
    animation_bones: dict[str, BoneAnimation],
    frame: float,
) -> list[BonePose]:
    """Sample every skeleton bone's authored local transform at one frame."""
    poses = []
    for bone in skeleton.bones:
        bone_animation = animation_bones.get(bone.name)
        scale, rotation, translation, rotation_mode = _sample_authored_bone(
            bone,
            bone_animation,
            skeleton.rotation_mode,
            frame,
        )
        poses.append(
            BonePose(
                rotation_translation=_rotation_translation_matrix(rotation_mode, rotation, translation),
                scale=scale,
                compensates_parent_scale=(
                    skeleton.scale_mode == "maya"
                    and bone.parent_index is not None
                    and _compensates_segment_scale(bone, bone_animation)
                ),
            )
        )
    return poses


def evaluate_world_matrices(
    skeleton: ModelSkeleton,
    animation_bones: dict[str, BoneAnimation],
    frame: float,
) -> list[tuple[float, ...]]:
    """Evaluate every bone's model-space matrix with the skeleton's scale rules."""
    authored = [
        _sample_authored_bone(
            bone,
            animation_bones.get(bone.name),
            skeleton.rotation_mode,
            frame,
        )
        for bone in skeleton.bones
    ]
    without_scale: list[tuple[float, ...]] = []
    world: list[tuple[float, ...]] = []
    accumulated: list[tuple[float, float, float]] = []

    for index, bone in enumerate(skeleton.bones):
        scale, rotation, translation, rotation_mode = authored[index]
        rotation_translation = _rotation_translation_matrix(
            rotation_mode,
            rotation,
            translation,
        )
        parent = bone.parent_index
        if parent is None:
            bone_without_scale = rotation_translation
            accumulated_scale = scale
        elif skeleton.scale_mode == "none":
            bone_without_scale = _matrix_multiply(without_scale[parent], rotation_translation)
            accumulated_scale = scale
        elif skeleton.scale_mode == "maya":
            if _compensates_segment_scale(bone, animation_bones.get(bone.name)):
                parent_scale = authored[parent][0]
                adjusted_translation = tuple(
                    translation[axis] * parent_scale[axis] for axis in range(3)
                )
                adjusted = _matrix_with_translation(rotation_translation, adjusted_translation)
                bone_without_scale = _matrix_multiply(without_scale[parent], adjusted)
            else:
                bone_without_scale = _matrix_multiply(
                    _matrix_multiply(without_scale[parent], _scale_matrix(authored[parent][0])),
                    rotation_translation,
                )
            accumulated_scale = scale
        elif skeleton.scale_mode == "softimage":
            parent_scale = accumulated[parent]
            adjusted_translation = tuple(
                translation[axis] * parent_scale[axis] for axis in range(3)
            )
            adjusted = _matrix_with_translation(rotation_translation, adjusted_translation)
            bone_without_scale = _matrix_multiply(without_scale[parent], adjusted)
            accumulated_scale = tuple(
                parent_scale[axis] * scale[axis] for axis in range(3)
            )
        else:
            bone_without_scale = _matrix_multiply(
                _matrix_multiply(without_scale[parent], _scale_matrix(authored[parent][0])),
                rotation_translation,
            )
            accumulated_scale = scale

        if skeleton.scale_mode == "none":
            bone_world = bone_without_scale
        elif skeleton.scale_mode == "softimage":
            bone_world = _matrix_multiply(bone_without_scale, _scale_matrix(accumulated_scale))
        else:
            bone_world = _matrix_multiply(bone_without_scale, _scale_matrix(scale))

        without_scale.append(bone_without_scale)
        accumulated.append(accumulated_scale)
        world.append(bone_world)
    return world


def _evaluate_skeleton(
    skeleton: ModelSkeleton,
    animation_bones: dict[str, BoneAnimation],
    frame: float,
) -> list[tuple[float, ...]]:
    """Each bone's matrix relative to its parent's complete world matrix."""
    world = evaluate_world_matrices(skeleton, animation_bones, frame)
    return [
        matrix
        if bone.parent_index is None
        else _matrix_multiply(_affine_inverse(world[bone.parent_index]), matrix)
        for bone, matrix in zip(skeleton.bones, world)
    ]


def _sample_skeleton(
    skeleton: ModelSkeleton,
    animation: SkeletalAnimation,
) -> dict[
    str,
    tuple[list[tuple[float, ...]], list[tuple[float, ...]], list[tuple[float, ...]]],
]:
    sample_count = (animation.frame_count + 1) * len(skeleton.bones)
    if sample_count > MAX_BONE_FRAME_SAMPLES:
        raise AnimationError(
            f"animation requires {sample_count:,} bone-frame samples, above the "
            f"{MAX_BONE_FRAME_SAMPLES:,} safety limit"
        )
    animation_bones = {bone.name: bone for bone in animation.bones}
    sampled = {
        bone.name: ([], [], [])
        for bone in skeleton.bones
    }
    previous_rotations: dict[str, tuple[float, float, float, float]] = {}
    for frame in range(animation.frame_count + 1):
        matrices = _evaluate_skeleton(skeleton, animation_bones, float(frame))
        for bone, matrix in zip(skeleton.bones, matrices):
            translation, rotation, scale = _matrix_trs(
                matrix,
                f"{bone.name} at frame {frame}",
            )
            quaternion = tuple(rotation)
            previous = previous_rotations.get(bone.name)
            if previous is not None and sum(
                left * right for left, right in zip(previous, quaternion)
            ) < 0.0:
                quaternion = tuple(-value for value in quaternion)
            previous_rotations[bone.name] = quaternion
            translations, rotations, scales = sampled[bone.name]
            translations.append(tuple(translation))
            rotations.append(quaternion)
            scales.append(tuple(scale))
    return sampled


def inject_skeletal_animation(
    model: str | Path,
    output: str | Path,
    animation: SkeletalAnimation,
    skeleton: ModelSkeleton,
    *,
    fps: int = 60,
) -> GlbAnimationReport:
    """Inject a sampled FSKA clip into a BfrAss-generated GLB."""
    model = Path(model)
    output = Path(output)
    if fps <= 0:
        raise AnimationError("animation FPS must be positive")
    document, binary = _read_glb(model)
    if len(document.get("buffers", [])) != 1:
        raise AnimationError("converted GLB must contain exactly one buffer")
    declared_binary_size = document["buffers"][0].get("byteLength")
    if not isinstance(declared_binary_size, int) or not 0 <= declared_binary_size <= len(binary):
        raise AnimationError("converted GLB declares an invalid binary buffer size")
    del binary[declared_binary_size:]

    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise AnimationError("converted GLB contains no nodes")
    node_indices: dict[str, int] = {}
    for index, node in enumerate(nodes):
        name = node.get("name")
        if not isinstance(name, str):
            continue
        if name in node_indices:
            raise AnimationError(f"converted GLB contains duplicate node name {name!r}")
        node_indices[name] = index

    if not animation.bones:
        raise AnimationError(f"animation {animation.name!r} contains no bones")
    skeleton_names = {bone.name for bone in skeleton.bones}
    missing = tuple(bone.name for bone in animation.bones if bone.name not in skeleton_names)
    if missing:
        detail = ", ".join(missing[:5])
        if len(missing) > 5:
            detail += f", and {len(missing) - 5} more"
        raise AnimationError(
            f"animation {animation.name!r} does not match the selected model; "
            f"missing bones: {detail}"
        )
    if not rotation_modes_compatible(animation, skeleton):
        raise AnimationError(
            f"animation {animation.name!r} uses {animation.rotation_mode} rotation, "
            f"but model {skeleton.name!r} uses {skeleton.rotation_mode}"
        )
    if animation.scale_mode != skeleton.scale_mode:
        raise AnimationError(
            f"animation {animation.name!r} uses {animation.scale_mode} scale mode, "
            f"but model {skeleton.name!r} uses {skeleton.scale_mode}"
        )
    missing_nodes = tuple(bone.name for bone in skeleton.bones if bone.name not in node_indices)
    if missing_nodes:
        detail = ", ".join(missing_nodes[:5])
        if len(missing_nodes) > 5:
            detail += f", and {len(missing_nodes) - 5} more"
        raise AnimationError(
            f"converted GLB is missing bones from model {skeleton.name!r}: {detail}"
        )

    time_values = [(frame / fps,) for frame in range(animation.frame_count + 1)]
    time_accessor = _append_float_accessor(
        document,
        binary,
        time_values,
        "SCALAR",
        bounds=True,
    )
    samplers: list[dict] = []
    channels: list[dict] = []
    sampled = _sample_skeleton(skeleton, animation)
    for bone in skeleton.bones:
        node_index = node_indices[bone.name]
        node = nodes[node_index]
        rest_translation, rest_rotation, rest_scale = _node_trs(node, bone.name)
        node.pop("matrix", None)
        node["translation"] = rest_translation
        node["rotation"] = rest_rotation
        node["scale"] = rest_scale
        translations, rotations, scales = sampled[bone.name]
        for path_name, accessor_type, values in (
            ("translation", "VEC3", translations),
            ("rotation", "VEC4", rotations),
            ("scale", "VEC3", scales),
        ):
            output_accessor = _append_float_accessor(
                document,
                binary,
                values,
                accessor_type,
            )
            sampler_index = len(samplers)
            samplers.append(
                {
                    "input": time_accessor,
                    "interpolation": "LINEAR",
                    "output": output_accessor,
                }
            )
            channels.append(
                {
                    "sampler": sampler_index,
                    "target": {"node": node_index, "path": path_name},
                }
            )

    animations = document.setdefault("animations", [])
    animations.append(
        {
            "name": animation.name,
            "samplers": samplers,
            "channels": channels,
            "extras": {
                "oot_remake_frame_count": animation.frame_count,
                "oot_remake_fps": fps,
                "oot_remake_loop": animation.loop,
            },
        }
    )
    document["buffers"][0]["byteLength"] = len(binary)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_glb(output, document, binary)
    return GlbAnimationReport(
        output=output,
        animation=animation.name,
        frame_count=animation.frame_count,
        matched_bones=len(animation.bones),
    )
