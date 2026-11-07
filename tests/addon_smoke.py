"""Optional Blender integration test against a user-owned game dump.

Run with Blender, after building the native converter:

  blender --background --factory-startup --python tests/addon_smoke.py -- \
    --nup <game.nup> --asset Model/Enemy_Stalfos.Enemy_Stalfos.bfres.zs \
    --animation-asset Model/Enemy_Stalfos_Animation.anim.bfres.zs \
    --animation-clip Appear --expect-animation-frames 60 \
    --converter <bfrass.exe> --output <result.blend>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import bpy


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nup", type=Path, required=True)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--animation-asset")
    parser.add_argument("--animation-clip")
    parser.add_argument("--animation-fps", type=int, default=60)
    parser.add_argument("--expect-animation-frames", type=int)
    parser.add_argument("--converter", type=Path)
    parser.add_argument("--native-library", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-import-path", choices=("direct", "gltf"))
    parser.add_argument("--expect-layered-materials", type=int)
    parser.add_argument("--expect-alpha-materials", type=int)
    parser.add_argument(
        "--expect-material-mode",
        action="append",
        default=[],
        metavar="MATERIAL=MODE",
    )
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1:])


def main() -> None:
    args = arguments()
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))
    if args.converter is not None:
        os.environ["OOTR_BFRASS"] = str(args.converter.resolve())
    if args.native_library is not None:
        os.environ["OOTR_NATIVE"] = str(args.native_library.resolve())

    import oot_remake_importer

    oot_remake_importer.register()
    settings = bpy.context.scene.oot_remake_importer
    settings.nup_path = str(args.nup.resolve())
    self_result = bpy.ops.oot_remake.index_nup()
    if "FINISHED" not in self_result:
        raise RuntimeError(f"NUP indexing failed: {self_result}")
    settings.asset_path = args.asset
    if bool(args.animation_asset) != bool(args.animation_clip):
        raise RuntimeError("--animation-asset and --animation-clip must be provided together")
    if args.animation_asset:
        settings.animation_path = args.animation_asset
        settings.animation_clip = args.animation_clip
        settings.animation_fps = args.animation_fps
    import_result = bpy.ops.oot_remake.import_model()
    if "FINISHED" not in import_result:
        raise RuntimeError(f"model import failed: {import_result}")

    imported_collections = [
        collection for collection in bpy.data.collections if collection.get("oot_remake_asset") == args.asset
    ]
    if len(imported_collections) != 1:
        raise RuntimeError(f"expected one imported collection, found {len(imported_collections)}")
    semantics_version = imported_collections[0].get("oot_remake_material_semantics")
    if not semantics_version:
        raise RuntimeError("imported collection does not record material semantics")
    expected_import_path = args.expect_import_path or (
        "gltf" if args.animation_asset else "direct"
    )
    actual_import_path = imported_collections[0].get("oot_remake_import_path")
    if actual_import_path != expected_import_path:
        raise RuntimeError(
            f"import path is {actual_import_path!r}, expected {expected_import_path!r}"
        )
    imported = set(imported_collections[0].objects)
    meshes = [obj for obj in imported if obj.type == "MESH"]
    armatures = [obj for obj in imported if obj.type == "ARMATURE"]
    if not meshes:
        raise RuntimeError("expected at least one imported mesh")
    if args.animation_asset and not armatures:
        raise RuntimeError("animated import did not create an armature")
    if expected_import_path == "direct" and armatures:
        raise RuntimeError("direct static import unexpectedly retained an armature")
    if not bpy.data.materials or not bpy.data.images:
        raise RuntimeError("expected imported materials and images")
    materials = {
        material
        for mesh in meshes
        for material in mesh.data.materials
        if material is not None
    }
    layered = [
        material
        for material in materials
        if material.get("oot_remake_layered_albedo")
    ]
    alpha = [
        material
        for material in materials
        if material.get("oot_remake_alpha_mode")
    ]
    require_layered = args.expect_layered_materials
    if require_layered is not None and len(layered) != require_layered:
        raise RuntimeError(
            f"found {len(layered)} layered materials, expected {require_layered}"
        )
    require_alpha = args.expect_alpha_materials
    if require_alpha is not None and len(alpha) != require_alpha:
        raise RuntimeError(
            f"found {len(alpha)} alpha materials, expected {require_alpha}"
        )
    for material in layered:
        if material.node_tree.nodes.get("BFRES Layered Albedo") is None:
            raise RuntimeError(
                f"layered material {material.name!r} has no layered-albedo node"
            )
        if not material.get("oot_remake_layer_mask"):
            raise RuntimeError(
                f"layered material {material.name!r} has no layer-mask metadata"
            )
    for material in alpha:
        if material.surface_render_method != "DITHERED":
            raise RuntimeError(
                f"alpha material {material.name!r} is not using dithered rendering"
            )
    if args.animation_asset:
        collection = imported_collections[0]
        if collection.get("oot_remake_animation_asset") != args.animation_asset:
            raise RuntimeError("imported collection does not record the animation resource")
        if collection.get("oot_remake_animation") != args.animation_clip:
            raise RuntimeError("imported collection does not record the animation clip")
        if collection.get("oot_remake_animation_fps") != args.animation_fps:
            raise RuntimeError("imported collection does not record the animation frame rate")
        if bpy.context.scene.render.fps != args.animation_fps:
            raise RuntimeError(
                f"scene is {bpy.context.scene.render.fps} FPS, expected {args.animation_fps}"
            )
        if collection.get("oot_remake_animation_source") != "BFRES FSKA direct Action":
            raise RuntimeError("animated import did not create its Action directly from BFRES")
        transfer_error = collection.get("oot_remake_animation_transfer_error")
        if not isinstance(transfer_error, float) or transfer_error >= 2e-4:
            raise RuntimeError(
                f"direct Action transfer error is invalid: {transfer_error!r}"
            )
        if any(obj.type == "EMPTY" for obj in imported):
            raise RuntimeError("animated import retained glTF skeleton helper empties")
        if any(
            pose_bone.custom_shape is not None
            for armature in armatures
            for pose_bone in armature.pose.bones
        ):
            raise RuntimeError("animated import retained glTF custom bone shapes")
        if any(obj.name.startswith("Icosphere") for obj in bpy.data.objects):
            raise RuntimeError("animated import created a glTF bone-shape object")
        keyed_bones = collection.get("oot_remake_animation_keyed_bones")
        armature_bones = sum(len(armature.pose.bones) for armature in armatures)
        if keyed_bones != armature_bones:
            raise RuntimeError(
                f"Action keys {keyed_bones!r} bones, but the armature has {armature_bones}"
            )
        action = bpy.data.actions.get(args.animation_clip)
        if action is None:
            raise RuntimeError(f"expected imported action {args.animation_clip!r}")
        if not any(
            armature.animation_data is not None
            and armature.animation_data.action == action
            for armature in armatures
        ):
            raise RuntimeError(f"action {args.animation_clip!r} is not assigned to the armature")
        if args.expect_animation_frames is not None:
            expected_range = (0.0, float(args.expect_animation_frames))
            if tuple(action.frame_range) != expected_range:
                raise RuntimeError(
                    f"action range is {tuple(action.frame_range)!r}, expected {expected_range!r}"
                )
    for expectation in args.expect_material_mode:
        try:
            material_name, expected_mode = expectation.split("=", 1)
        except ValueError as error:
            raise RuntimeError(
                f"invalid --expect-material-mode value {expectation!r}"
            ) from error
        material = bpy.data.materials.get(material_name)
        if material is None:
            raise RuntimeError(f"expected imported material {material_name!r}")
        actual_mode = material.get("oot_remake_vertex_color_mode")
        if actual_mode != expected_mode:
            raise RuntimeError(
                f"material {material_name!r} mode is {actual_mode!r}, "
                f"expected {expected_mode!r}"
            )
        if material.get("oot_remake_material_semantics") != semantics_version:
            raise RuntimeError(
                f"material {material_name!r} does not use catalog {semantics_version!r}"
            )
        if not material.get("oot_remake_vertex_color_reason"):
            raise RuntimeError(
                f"material {material_name!r} does not record its mapping reason"
            )
        if not material.get("oot_remake_sampler_semantics"):
            raise RuntimeError(
                f"material {material_name!r} does not record sampler semantics"
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output.resolve()))
    print(
        f"OOTR_SMOKE_OK collection={imported_collections[0].name!r} "
        f"objects={len(imported)} meshes={len(meshes)} armatures={len(armatures)} "
        f"materials={len(bpy.data.materials)} images={len(bpy.data.images)} "
        f"path={actual_import_path!r} animation={args.animation_clip!r}"
    )


if __name__ == "__main__":
    main()
