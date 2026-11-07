"""Create Blender Actions directly from evaluated BFRES skeletal animation.

Each pose-bone basis is computed from the bone's authored local transform
rather than from its parent's evaluated world matrix, so no animated matrix is
ever inverted: bones scaled to zero (the title hides parts this way) key like
any other. Maya segment-scale compensation maps to Blender's per-bone
`inherit_scale = 'NONE'`, which keeps the parent's rotation and scaled
location but drops its scale; under a non-uniformly scaled parent a
compensating bone's matrix relative to that parent is sheared, which a pose
basis cannot hold, so it cannot be keyed with full inheritance. Both
reconstructions are exact when every bone's Blender rest orientation is an axis
permutation of its source orientation, since a pose basis scales only along
the bone's own axes. The glTF importer produces that for nearly every bone;
the few others are turned to the nearest permutation before keying. Every
frame is then compared with the evaluated BFRES pose, and an Action that
differs is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

import bpy
from mathutils import Matrix, Quaternion, Vector

from .core.animation import (
    AnimationError,
    ModelSkeleton,
    SkeletalAnimation,
    evaluate_world_matrices,
    rotation_modes_compatible,
    sample_bone_poses,
)

# Largest absolute difference allowed between a keyed Blender pose matrix and
# the evaluated BFRES matrix, in Blender units, on any frame.
TRANSFER_TOLERANCE = 2e-4
AXIS_ALIGNMENT_TOLERANCE = 1e-3
SUPPORTED_SCALE_MODES = ("standard", "maya")


@dataclass(frozen=True, slots=True)
class BlenderAnimationReport:
    animation: str
    frame_count: int
    matched_bones: int
    keyed_bones: int
    compensating_bones: int
    realigned_bones: int
    maximum_transfer_error: float


def _matrix(values: tuple[float, ...]) -> Matrix:
    return Matrix(tuple(values[index:index + 4] for index in range(0, 16, 4)))


def _coordinate_conversion(scene: bpy.types.Scene) -> Matrix:
    unit = 1.0 / scene.unit_settings.scale_length
    return Matrix(
        (
            (unit, 0.0, 0.0, 0.0),
            (0.0, 0.0, -unit, 0.0),
            (0.0, unit, 0.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def _scale_matrix(scale) -> Matrix:
    return Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))


def _normalized_columns(matrix: Matrix) -> Matrix:
    result = matrix.to_3x3()
    for column in range(3):
        axis = Vector(result.col[column])
        length = axis.length
        if length <= 1e-12:
            raise AnimationError("bone rest orientation has a zero-length axis")
        result.col[column] = axis / length
    return result


def _nearest_axis_permutation(matrix: Matrix) -> Matrix:
    """The proper signed permutation closest to a rotation matrix."""
    result = Matrix(((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
    used_rows: set[int] = set()
    weakest = (2.0, 0)
    for column in sorted(range(3), key=lambda column: -max(abs(matrix[row][column]) for row in range(3))):
        row = max(
            (row for row in range(3) if row not in used_rows),
            key=lambda row: abs(matrix[row][column]),
        )
        used_rows.add(row)
        result[row][column] = 1.0 if matrix[row][column] >= 0.0 else -1.0
        weakest = min(weakest, (abs(matrix[row][column]), column))
    if result.determinant() < 0.0:
        column = weakest[1]
        for row in range(3):
            result[row][column] = -result[row][column]
    return result


def _align_rest_orientations(
    context,
    armature,
    skeleton: ModelSkeleton,
    source_rest: list[Matrix],
) -> int:
    """Turn bones whose rest frame is not an axis permutation of the source's.

    The glTF importer orients most bones as an axis permutation of their
    source bone but not all (Saria's face rig, for one). A pose basis scales
    only along the bone's own axes, so such a bone cannot carry the source's
    non-uniform scale. Each one is rotated in place to the nearest
    permutation; its head and length are kept, and the rest-pose mesh does
    not move.
    """
    targets: dict[str, Matrix] = {}
    for index, bone in enumerate(skeleton.bones):
        blender_rest = armature.data.bones[bone.name].matrix_local
        source = _normalized_columns(source_rest[index])
        offset = source.inverted() @ blender_rest.to_3x3()
        if _axis_alignment_error(offset) <= AXIS_ALIGNMENT_TOLERANCE:
            continue
        rotation = (source @ _nearest_axis_permutation(offset)).to_4x4()
        rotation.translation = blender_rest.translation
        targets[bone.name] = rotation
    if not targets:
        return 0

    view_layer = context.view_layer
    previous_active = view_layer.objects.active
    view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = armature.data.edit_bones
        for name, matrix in targets.items():
            edit_bone = edit_bones[name]
            for child in edit_bone.children:
                child.use_connect = False
            length = edit_bone.length
            edit_bone.matrix = matrix
            edit_bone.length = length
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        view_layer.objects.active = previous_active
    for name, matrix in targets.items():
        actual = armature.data.bones[name].matrix_local.to_3x3()
        if any(abs(actual[row][column] - matrix[row][column]) > 1e-4 for row in range(3) for column in range(3)):
            raise AnimationError(f"could not align Blender bone {name!r} with its source bone")
    return len(targets)


def _axis_alignment_error(matrix: Matrix) -> float:
    rows = _normalized_columns(matrix)
    return max(min(abs(abs(value) - 1.0), abs(value)) for row in rows for value in row)


def _check_rig(armature, skeleton: ModelSkeleton) -> None:
    pose_bones = armature.pose.bones
    missing = tuple(bone.name for bone in skeleton.bones if bone.name not in pose_bones)
    if missing:
        detail = ", ".join(missing[:5])
        if len(missing) > 5:
            detail += f", and {len(missing) - 5} more"
        raise AnimationError(f"Blender armature is missing source bones: {detail}")
    for bone in skeleton.bones:
        expected_parent = (
            skeleton.bones[bone.parent_index].name
            if bone.parent_index is not None
            else None
        )
        actual_parent = pose_bones[bone.name].parent
        actual_parent_name = actual_parent.name if actual_parent is not None else None
        if actual_parent_name != expected_parent:
            raise AnimationError(
                f"Blender bone {bone.name!r} has parent {actual_parent_name!r}, "
                f"expected {expected_parent!r}"
            )


def _new_action(armature, name: str):
    """Create an Action assigned to the armature, returning (action, previous)."""
    previous_action = (
        armature.animation_data.action
        if armature.animation_data is not None
        else None
    )
    action = bpy.data.actions.new(name)
    armature.animation_data_create()
    armature.animation_data.action = action
    return action, previous_action


def _write_curves(action, armature, channels: dict[tuple[str, str, int], list[float]], frame_count: int) -> None:
    """Write one linear key per frame for every (bone, property, index) channel."""
    frames = [float(frame) for frame in range(frame_count + 1)]
    linear = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    for (bone_name, property_name, index), values in channels.items():
        data_path = f'pose.bones["{bpy.utils.escape_identifier(bone_name)}"].{property_name}'
        curve = action.fcurve_ensure_for_datablock(
            armature,
            data_path,
            index=index,
            group_name=bone_name,
        )
        points = curve.keyframe_points
        points.add(len(frames))
        coordinates = [value for pair in zip(frames, values) for value in pair]
        points.foreach_set("co", coordinates)
        points.foreach_set("handle_left", coordinates)
        points.foreach_set("handle_right", coordinates)
        points.foreach_set("interpolation", [linear] * len(frames))
        curve.update()


def create_skeletal_action(
    context,
    armature: bpy.types.Object,
    skeleton: ModelSkeleton,
    animation: SkeletalAnimation,
    *,
    fps: int,
) -> BlenderAnimationReport:
    """Evaluate FSKA data and key it directly on a complete Blender armature."""
    if armature.type != "ARMATURE":
        raise AnimationError(f"{armature.name!r} is not an armature")
    if fps <= 0:
        raise AnimationError("animation FPS must be positive")
    if not rotation_modes_compatible(animation, skeleton):
        raise AnimationError(
            f"animation {animation.name!r} and model {skeleton.name!r} use different rotation modes"
        )
    if animation.scale_mode != skeleton.scale_mode:
        raise AnimationError(
            f"animation {animation.name!r} and model {skeleton.name!r} use different scale modes"
        )
    if skeleton.scale_mode not in SUPPORTED_SCALE_MODES:
        raise AnimationError(
            f"model {skeleton.name!r} uses {skeleton.scale_mode} scale inheritance, "
            "which Blender bones cannot reproduce"
        )
    _check_rig(armature, skeleton)

    pose_bones = armature.pose.bones
    rest_bones = armature.data.bones
    conversion = _coordinate_conversion(context.scene)
    conversion_inverse = conversion.inverted()
    conversion_3 = conversion.to_3x3()
    conversion_inverse_3 = conversion_inverse.to_3x3()

    # Rest data. offset maps a source rest frame onto the Blender bone's rest
    # frame; rest_local_inverse undoes the authored rest transform.
    rest_world = [_matrix(matrix) for matrix in evaluate_world_matrices(skeleton, {}, 0.0)]
    realigned = _align_rest_orientations(
        context,
        armature,
        skeleton,
        [conversion @ matrix @ conversion_inverse for matrix in rest_world],
    )
    rest_poses = sample_bone_poses(skeleton, {}, 0.0)
    offsets: list[Matrix] = []
    offsets_inverse: list[Matrix] = []
    offsets_normalized_inverse: list[Matrix] = []
    rest_local_inverse: list[Matrix] = []
    rest_relative_inverse: list[Matrix] = []
    for index, bone in enumerate(skeleton.bones):
        blender_rest = rest_bones[bone.name].matrix_local
        offset = (conversion @ rest_world[index] @ conversion_inverse).inverted_safe() @ blender_rest
        offsets.append(offset)
        offsets_inverse.append(offset.inverted())
        offsets_normalized_inverse.append(_normalized_columns(offset).inverted())
        rest = rest_poses[index]
        rest_local_inverse.append(
            (_matrix(rest.rotation_translation) @ _scale_matrix(rest.scale)).inverted()
        )
        parent = pose_bones[bone.name].parent
        relative = (
            blender_rest
            if parent is None
            else parent.bone.matrix_local.inverted() @ blender_rest
        )
        rest_relative_inverse.append(relative.inverted())

    animation_bones = {bone.name: bone for bone in animation.bones}
    first_poses = sample_bone_poses(skeleton, animation_bones, 0.0)
    compensating = {
        bone.name
        for bone, pose in zip(skeleton.bones, first_poses)
        if pose.compensates_parent_scale
    }
    previous_inherit = {bone.name: rest_bones[bone.name].inherit_scale for bone in skeleton.bones}
    custom_shapes = {
        bone.custom_shape
        for bone in pose_bones
        if bone.custom_shape is not None
    }

    channels: dict[tuple[str, str, int], list[float]] = {}
    for bone in skeleton.bones:
        for property_name, width in (("location", 3), ("rotation_quaternion", 4), ("scale", 3)):
            for component in range(width):
                channels[(bone.name, property_name, component)] = []
    previous_quaternions: dict[str, Quaternion] = {}
    for frame in range(animation.frame_count + 1):
        poses = sample_bone_poses(skeleton, animation_bones, float(frame))
        for index, bone in enumerate(skeleton.bones):
            pose = poses[index]
            if pose.compensates_parent_scale != (bone.name in compensating):
                raise AnimationError(
                    f"{animation.name!r} changes the scale compensation of {bone.name!r}"
                )
            rotation_translation = _matrix(pose.rotation_translation)
            scale = _scale_matrix(pose.scale)
            if pose.compensates_parent_scale:
                # Blender: rotation and scale follow the parent's orthonormal
                # frame; location follows the parent's complete matrix.
                parent = bone.parent_index
                frame_change = (
                    rest_relative_inverse[index].to_3x3()
                    @ offsets_normalized_inverse[parent]
                    @ conversion_3
                )
                basis_scaled = (
                    frame_change
                    @ rotation_translation.to_3x3()
                    @ scale.to_3x3()
                    @ conversion_inverse_3
                    @ offsets[index].to_3x3()
                )
                basis_rotation = (
                    frame_change
                    @ rotation_translation.to_3x3()
                    @ conversion_inverse_3
                    @ offsets[index].to_3x3()
                )
                # The Blender head can sit away from the source joint (the
                # offset has a translation); that point moves with the bone's
                # own rotation and scale, measured in the parent's unscaled
                # frame, hence the division by the parent's scale.
                parent_scale = poses[parent].scale
                if any(abs(value) <= 1e-12 for value in parent_scale):
                    raise AnimationError(
                        f"{bone.name!r} compensates the zero scale of its parent at frame {frame}"
                    )
                head = (
                    _scale_matrix(tuple(1.0 / value for value in parent_scale)).to_3x3()
                    @ rotation_translation.to_3x3()
                    @ scale.to_3x3()
                    @ (conversion_inverse_3 @ offsets[index].translation)
                )
                location = (
                    rest_relative_inverse[index]
                    @ offsets_inverse[parent]
                    @ conversion
                    @ (rotation_translation.translation + head)
                )
            else:
                prefix = offsets_inverse[index] @ conversion @ rest_local_inverse[index]
                suffix = conversion_inverse @ offsets[index]
                basis = prefix @ rotation_translation @ scale @ suffix
                basis_scaled = basis.to_3x3()
                basis_rotation = (prefix @ rotation_translation @ suffix).to_3x3()
                location = basis.translation
            rotation = basis_rotation.to_quaternion()
            # The rotation part above omits the scale; what remains of the
            # basis after removing it must be a pure axis scale.
            residual = rotation.to_matrix().inverted() @ basis_scaled
            scale_values = (residual[0][0], residual[1][1], residual[2][2])
            previous = previous_quaternions.get(bone.name)
            if previous is not None and rotation.dot(previous) < 0.0:
                rotation.negate()
            previous_quaternions[bone.name] = rotation
            for component in range(3):
                channels[(bone.name, "location", component)].append(location[component])
                channels[(bone.name, "scale", component)].append(scale_values[component])
            for component in range(4):
                channels[(bone.name, "rotation_quaternion", component)].append(rotation[component])

    for bone in pose_bones:
        bone.custom_shape = None
        bone.rotation_mode = "QUATERNION"
    for custom_shape in custom_shapes:
        bpy.data.objects.remove(custom_shape, do_unlink=True)
    armature.show_in_front = False
    for bone in skeleton.bones:
        rest_bones[bone.name].inherit_scale = "NONE" if bone.name in compensating else "FULL"

    action, previous_action = _new_action(armature, animation.name)

    def discard() -> None:
        armature.animation_data.action = previous_action
        bpy.data.actions.remove(action)
        for name, mode in previous_inherit.items():
            rest_bones[name].inherit_scale = mode

    try:
        _write_curves(action, armature, channels, animation.frame_count)
        scene = context.scene
        maximum_error = 0.0
        worst = ("", 0)
        for frame in range(animation.frame_count + 1):
            scene.frame_set(frame)
            world = evaluate_world_matrices(skeleton, animation_bones, float(frame))
            for index, bone in enumerate(skeleton.bones):
                target = conversion @ _matrix(world[index]) @ conversion_inverse @ offsets[index]
                actual = pose_bones[bone.name].matrix
                error = max(
                    abs(actual[row][column] - target[row][column])
                    for row in range(3)
                    for column in range(4)
                )
                if error > maximum_error:
                    maximum_error = error
                    worst = (bone.name, frame)
        if maximum_error > TRANSFER_TOLERANCE:
            raise AnimationError(
                "direct Blender Action differs from the evaluated BFRES pose by "
                f"{maximum_error:.6g} at bone {worst[0]!r}, frame {worst[1]}"
            )
    except Exception:
        discard()
        raise

    action["oot_remake_source"] = "BFRES FSKA"
    action["oot_remake_frame_count"] = animation.frame_count
    action["oot_remake_fps"] = fps
    action["oot_remake_loop"] = animation.loop
    scene.render.fps = fps
    scene.render.fps_base = 1.0
    scene.frame_start = 0
    scene.frame_end = animation.frame_count
    scene.frame_set(0)
    return BlenderAnimationReport(
        animation=animation.name,
        frame_count=animation.frame_count,
        matched_bones=len(animation.bones),
        keyed_bones=len(skeleton.bones),
        compensating_bones=len(compensating),
        realigned_bones=realigned,
        maximum_transfer_error=maximum_error,
    )
