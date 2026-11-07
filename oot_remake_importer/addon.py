"""Blender UI and import orchestration."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ImportHelper
from mathutils import Euler, Matrix, Vector

from .core.animation import (
    FRES_MAGIC,
    AnimationError,
    ModelSkeleton,
    SkeletalAnimation,
    glb_has_meshes,
    prepare_skeleton_glb,
    read_model_skeletons,
    read_skeletal_animations,
    rotation_modes_compatible,
    unsupported_animation_counts,
)
from .blender_animation import BlenderAnimationReport, create_skeletal_action
from .core.converter import ConversionError, ConversionReport, convert_bfres
from .core.levels import (
    ActorModel,
    ActorModelResolution,
    LevelDefinition,
    LevelError,
    PlacementActor,
    actor_pack_path,
    discover_levels,
    read_placement,
    resolve_actor_models,
)
from .core.materials import (
    EXTERNAL_STRINGS_PATH,
    MaterialError,
    MaterialMetadata,
    read_external_strings,
    read_materials,
)
from .core.nup import EXPECTED_TITLE_ID, NupArchive, NupError, NupPackage
from .core.native_bridge import NativeBridgeError, decode_bntx_png
from .core.resources import (
    ResourceDecoder,
    ResourceError,
    animation_paths,
    build_texture_index,
    model_paths,
    read_sarc,
    referenced_texture_paths,
    texture_resource_path,
)
from .core.terrain import TerrainError, TerrainScene, read_tscb, terrain_scene_paths
from .material_mapping import MaterialMappingStats, map_imported_materials
from .native_import import import_bfres_direct, load_texture_image
from .terrain_import import TerrainImportStats, import_terrain_scene


LOGGER = logging.getLogger(__name__)
ADDON_ID = __package__
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
EXPECTED_PLATFORM_WARNING = "targets platform 'Ounc', decoding as NX"


@dataclass(slots=True)
class IndexedArchive:
    archive: NupArchive
    package: NupPackage
    entries: dict
    decoder: ResourceDecoder
    models: tuple[str, ...]
    animations: tuple[str, ...]
    levels: tuple[LevelDefinition, ...]
    textures: dict[str, tuple[str, ...]]
    external_strings: dict[int, str] | None = None
    animation_sets: dict[str, tuple[SkeletalAnimation, ...]] | None = None
    actor_models: dict[str, ActorModelResolution] | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    object_count: int
    texture_count: int
    warnings: tuple[str, ...]
    animation: str | None = None
    animation_frames: int = 0


@dataclass(frozen=True, slots=True)
class LevelImportResult:
    placement_count: int
    visual_count: int
    nonvisual_count: int
    source_model_count: int
    texture_count: int
    skipped_count: int
    failed_source_model_count: int
    failed_placement_count: int
    terrain_scene_count: int
    terrain_tile_count: int
    terrain_vertex_count: int
    terrain_face_count: int
    warnings: tuple[str, ...]


_INDEXED_ARCHIVE: IndexedArchive | None = None
_ASSET_ITEMS: list[tuple[str, str, str, str, int]] = []
_ASSET_PATHS: dict[str, str] = {}
_LEVEL_ITEMS: list[tuple[str, str, str, str, int]] = []
_LEVEL_IDS: dict[str, str] = {}
_ANIMATION_ITEMS: list[tuple[str, str, str, str, int]] = []
_ANIMATION_PATHS: dict[str, str] = {}
_CLIP_ITEMS: list[tuple[str, str, str, str, int]] = []
_CLIP_NAMES: dict[str, str] = {}


def _resolved_path(value: str) -> Path:
    return Path(bpy.path.abspath(value)).expanduser().resolve()


def _index_archive(nup_path: str) -> IndexedArchive:
    global _INDEXED_ARCHIVE

    path = _resolved_path(nup_path)
    if not path.is_file():
        raise NupError(f"NUP does not exist: {path}")
    stat = path.stat()
    identity = str(path), stat.st_size, stat.st_mtime_ns
    if _INDEXED_ARCHIVE is not None and _INDEXED_ARCHIVE.archive.identity == identity:
        return _INDEXED_ARCHIVE

    archive = NupArchive(path)
    package = archive.default_package()
    if package.title_id != EXPECTED_TITLE_ID:
        raise NupError(
            f"NUP title id is {package.title_id}; expected Ocarina of Time Remake {EXPECTED_TITLE_ID}"
        )
    entries = archive.package_files(package)
    models = model_paths(entries)
    if not models:
        raise NupError("the selected NUP contains no importable Model/*.bfres.zs resources")
    animations = animation_paths(entries)
    levels = discover_levels(entries)
    indexed = IndexedArchive(
        archive=archive,
        package=package,
        entries=entries,
        decoder=ResourceDecoder(archive, package),
        models=models,
        animations=animations,
        levels=levels,
        textures=build_texture_index(entries),
        animation_sets={},
        actor_models={},
    )
    _INDEXED_ARCHIVE = indexed
    _set_asset_items(indexed.models)
    _set_level_items(indexed.levels)
    _set_animation_items(indexed.animations)
    _set_clip_items(())
    return indexed


def _set_asset_items(paths: tuple[str, ...]) -> None:
    global _ASSET_ITEMS, _ASSET_PATHS

    items: list[tuple[str, str, str, str, int]] = []
    values: dict[str, str] = {}
    for index, path in enumerate(paths):
        identifier = f"MODEL_{index:05d}"
        filename = PurePosixPath(path).name
        label = filename[:-len(".bfres.zs")]
        items.append((identifier, label, path, "MESH_DATA", index))
        values[identifier] = path
    _ASSET_ITEMS = items
    _ASSET_PATHS = values


def _asset_enum_items(_operator, _context):
    if _ASSET_ITEMS:
        return _ASSET_ITEMS
    return [("NONE", "Index a NUP first", "Select and index the game package", "INFO", 0)]


def _set_level_items(levels: tuple[LevelDefinition, ...]) -> None:
    global _LEVEL_ITEMS, _LEVEL_IDS

    items: list[tuple[str, str, str, str, int]] = []
    values: dict[str, str] = {}
    for index, level in enumerate(levels):
        identifier = f"LEVEL_{index:04d}"
        kinds = []
        if level.static_path is not None:
            kinds.append("static")
        if level.dynamic_path is not None:
            kinds.append("dynamic")
        label = level.name.replace("_", " ")
        description = f"{level.world}: {' and '.join(kinds)} placements"
        items.append((identifier, label, description, "OUTLINER_COLLECTION", index))
        values[identifier] = level.identifier
    _LEVEL_ITEMS = items
    _LEVEL_IDS = values


def _level_enum_items(_operator, _context):
    if _LEVEL_ITEMS:
        return _LEVEL_ITEMS
    return [("NONE", "Index a NUP first", "Select and index the game package", "INFO", 0)]


def _set_animation_items(paths: tuple[str, ...]) -> None:
    global _ANIMATION_ITEMS, _ANIMATION_PATHS

    items: list[tuple[str, str, str, str, int]] = []
    values: dict[str, str] = {}
    suffix = ".anim.bfres.zs"
    for index, path in enumerate(paths):
        identifier = f"ANIMATION_{index:05d}"
        filename = PurePosixPath(path).name
        label = filename[:-len(suffix)] if filename.lower().endswith(suffix) else filename
        items.append((identifier, label, path, "ACTION", index))
        values[identifier] = path
    _ANIMATION_ITEMS = items
    _ANIMATION_PATHS = values


def _animation_enum_items(_operator, _context):
    if _ANIMATION_ITEMS:
        return _ANIMATION_ITEMS
    return [("NONE", "Index a NUP first", "Select and index the game package", "INFO", 0)]


def _set_clip_items(animations: tuple[SkeletalAnimation, ...]) -> None:
    global _CLIP_ITEMS, _CLIP_NAMES

    items: list[tuple[str, str, str, str, int]] = []
    values: dict[str, str] = {}
    for index, animation in enumerate(animations):
        identifier = f"CLIP_{index:05d}"
        description = (
            f"{animation.frame_count} frames, {len(animation.bones)} animated bones"
            + (", looping" if animation.loop else "")
        )
        items.append((identifier, animation.name, description, "ACTION", index))
        values[identifier] = animation.name
    _CLIP_ITEMS = items
    _CLIP_NAMES = values


def _clip_enum_items(_operator, _context):
    if _CLIP_ITEMS:
        return _CLIP_ITEMS
    return [("NONE", "Choose an animation resource first", "", "INFO", 0)]


def _load_animation_set(
    indexed: IndexedArchive,
    resource_path: str,
) -> tuple[SkeletalAnimation, ...]:
    if indexed.animation_sets is None:
        indexed.animation_sets = {}
    cached = indexed.animation_sets.get(resource_path)
    if cached is not None:
        return cached
    data = indexed.decoder.decode(resource_path, expected_magic=FRES_MAGIC)
    animations = read_skeletal_animations(data)
    if not animations:
        others = unsupported_animation_counts(data)
        detail = (
            "; it holds only "
            + ", ".join(f"{count} {kind}" for kind, count in others.items())
            + " animation(s), which this importer does not convert"
            if others
            else ""
        )
        raise AnimationError(f"{resource_path} contains no skeletal animations{detail}")
    indexed.animation_sets[resource_path] = animations
    return animations


def _selected_animation(
    indexed: IndexedArchive,
    settings: "OOTR_Settings",
) -> SkeletalAnimation | None:
    if not settings.animation_path:
        return None
    if settings.animation_path not in indexed.animations:
        raise ResourceError("select an indexed animation resource before importing")
    if not settings.animation_clip:
        raise AnimationError("choose a skeletal animation clip before importing")
    animations = _load_animation_set(indexed, settings.animation_path)
    for animation in animations:
        if animation.name == settings.animation_clip:
            return animation
    raise AnimationError(
        f"animation clip {settings.animation_clip!r} is not in {settings.animation_path}"
    )


def _preferences(context) -> "OOTR_AddonPreferences | None":
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon is not None else None


def _converter_path(context) -> Path:
    environment_path = os.environ.get("OOTR_BFRASS", "").strip()
    if environment_path:
        return Path(environment_path).expanduser().resolve()
    preferences = _preferences(context)
    if preferences is not None and preferences.converter_path.strip():
        return _resolved_path(preferences.converter_path)
    return Path(__file__).resolve().parent / "bin" / "bfrass.exe"


def _native_library_path() -> Path:
    environment_path = os.environ.get("OOTR_NATIVE", "").strip()
    if environment_path:
        return Path(environment_path).expanduser().resolve()
    suffix = "dll" if os.name == "nt" else "dylib" if os.sys.platform == "darwin" else "so"
    return Path(__file__).resolve().parent / "bin" / f"ootr_native.{suffix}"


def _find_layer_collection(layer_collection, collection):
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _replace_missing_images(existing_images: set) -> tuple[str, ...]:
    missing: list[str] = []
    for image in set(bpy.data.images) - existing_images:
        if image.packed_file is not None or not image.filepath:
            continue
        source = Path(bpy.path.abspath(image.filepath))
        if source.is_file():
            continue

        original_name = image.name
        is_normal = "_nrm" in original_name.lower() or "normal" in original_name.lower()
        color = (0.5, 0.5, 1.0, 1.0) if is_normal else (1.0, 0.0, 1.0, 1.0)
        placeholder = bpy.data.images.new(f"{original_name}.missing", width=1, height=1, alpha=True)
        placeholder.generated_color = color
        placeholder["oot_remake_missing_source"] = image.filepath
        if is_normal:
            placeholder.colorspace_settings.name = "Non-Color"
        for material in bpy.data.materials:
            if not material.use_nodes or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                if getattr(node, "image", None) == image:
                    node.image = placeholder
        bpy.data.images.remove(image)
        placeholder.name = original_name
        missing.append(original_name)
    return tuple(sorted(missing))


def _import_glb(
    context,
    path: Path,
    collection_name: str,
    scale: float,
    material_metadata: dict[str, MaterialMetadata],
    *,
    remove_helpers: bool = False,
) -> tuple[bpy.types.Collection, set, tuple[str, ...], MaterialMappingStats]:
    existing = set(bpy.data.objects)
    existing_images = set(bpy.data.images)
    existing_materials = set(bpy.data.materials)
    collection = bpy.data.collections.new(collection_name)
    context.scene.collection.children.link(collection)
    target_layer = _find_layer_collection(context.view_layer.layer_collection, collection)
    previous_layer = context.view_layer.active_layer_collection
    if target_layer is not None:
        context.view_layer.active_layer_collection = target_layer
    try:
        result = bpy.ops.import_scene.gltf(
            filepath=str(path),
            bone_heuristic="TEMPERANCE",
            disable_bone_shape=True,
            guess_original_bind_pose=False,
        )
    finally:
        context.view_layer.active_layer_collection = previous_layer
    if "FINISHED" not in result:
        bpy.data.collections.remove(collection)
        raise ConversionError(f"Blender glTF importer returned {result}")

    imported = set(bpy.data.objects) - existing
    if not imported:
        bpy.data.collections.remove(collection)
        raise ConversionError("Blender glTF importer created no objects")

    custom_shapes = set()
    for obj in imported:
        if obj.type != "ARMATURE" or obj.pose is None:
            continue
        obj.show_in_front = False
        for bone in obj.pose.bones:
            if bone.custom_shape is not None:
                custom_shapes.add(bone.custom_shape)
                bone.custom_shape = None
    for custom_shape in custom_shapes:
        imported.discard(custom_shape)
        bpy.data.objects.remove(custom_shape, do_unlink=True)

    if remove_helpers:
        helpers = [obj for obj in imported if obj.type == "EMPTY"]
        for helper in helpers:
            parent = helper.parent
            for child in tuple(helper.children):
                world = child.matrix_world.copy()
                child.parent = parent
                child.matrix_world = world
            imported.remove(helper)
            bpy.data.objects.remove(helper, do_unlink=True)

    for obj in imported:
        for owner in tuple(obj.users_collection):
            owner.objects.unlink(obj)
        collection.objects.link(obj)
    if scale != 1.0:
        for obj in imported:
            if obj.parent not in imported:
                obj.scale = tuple(component * scale for component in obj.scale)
    missing_images = _replace_missing_images(existing_images)
    material_stats = map_imported_materials(
        imported,
        set(bpy.data.materials) - existing_materials,
        material_metadata,
    )
    return collection, imported, missing_images, material_stats


def _write_resource(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _read_model_materials(
    indexed: IndexedArchive,
    model_data: bytes,
) -> dict[str, MaterialMetadata]:
    if indexed.external_strings is None:
        external_data = indexed.decoder.decode(
            EXTERNAL_STRINGS_PATH,
            expected_magic=b"FRES    ",
        )
        indexed.external_strings = read_external_strings(external_data)
    return read_materials(model_data, indexed.external_strings)


def _model_texture_paths(
    indexed: IndexedArchive,
    model_data: bytes,
    *,
    enabled: bool,
    limit: int,
) -> tuple[str, ...]:
    if not enabled:
        return ()
    paths = referenced_texture_paths(model_data, indexed.textures)
    if len(paths) > limit:
        raise ResourceError(
            f"model references {len(paths)} textures, above the configured limit of {limit}"
        )
    return paths


def _actionable_warnings(report: ConversionReport) -> tuple[str, ...]:
    return tuple(warning for warning in report.warnings if EXPECTED_PLATFORM_WARNING not in warning)


def _matching_skeleton(
    skeletons: tuple[ModelSkeleton, ...],
    animation: SkeletalAnimation,
) -> ModelSkeleton:
    animated_names = {bone.name for bone in animation.bones}
    candidates = tuple(
        skeleton
        for skeleton in skeletons
        if skeleton.scale_mode == animation.scale_mode
        and rotation_modes_compatible(animation, skeleton)
        and animated_names.issubset(bone.name for bone in skeleton.bones)
    )
    if not candidates:
        raise AnimationError(
            f"animation {animation.name!r} does not match a skeleton in the selected model"
        )
    if len(candidates) > 1:
        names = ", ".join(skeleton.name for skeleton in candidates)
        raise AnimationError(
            f"animation {animation.name!r} matches multiple model skeletons: {names}"
        )
    return candidates[0]


def _import_indexed_model(context, settings: "OOTR_Settings") -> ImportResult:
    indexed = _index_archive(settings.nup_path)
    if settings.asset_path not in indexed.models:
        raise ResourceError("select an indexed model resource before importing")
    animation = _selected_animation(indexed, settings)

    window_manager = context.window_manager
    previous_fps = context.scene.render.fps
    previous_fps_base = context.scene.render.fps_base
    previous_frame_start = context.scene.frame_start
    previous_frame_end = context.scene.frame_end
    fps_changed = False
    succeeded = False
    window_manager.progress_begin(0, 5)
    try:
        model_data = indexed.decoder.decode(settings.asset_path, expected_magic=b"FRES    ")
        skeleton = (
            _matching_skeleton(read_model_skeletons(model_data), animation)
            if animation is not None
            else None
        )
        material_metadata = _read_model_materials(indexed, model_data)
        texture_paths = _model_texture_paths(
            indexed,
            model_data,
            enabled=settings.import_textures,
            limit=settings.max_textures,
        )
        window_manager.progress_update(1)

        with tempfile.TemporaryDirectory(prefix="oot_remake_import_") as temporary:
            work = Path(temporary)
            source_name = PurePosixPath(settings.asset_path).name[:-3]
            collection_name = PurePosixPath(settings.asset_path).name[
                :-len(".bfres.zs")
            ]
            fallback_warnings: list[str] = []
            if animation is None:
                native_library = _native_library_path()
                if native_library.is_file():
                    existing_images = set(bpy.data.images)
                    direct_texture_cache: dict[
                        tuple[str, str], bpy.types.Image
                    ] = {}
                    direct_texture_failures: dict[tuple[str, str], str] = {}
                    texture_images, texture_warnings = _load_direct_textures(
                        indexed,
                        material_metadata,
                        native_library,
                        work,
                        direct_texture_cache,
                        direct_texture_failures,
                        enabled=settings.import_textures,
                        max_model_textures=settings.max_textures,
                        max_total_textures=settings.max_textures,
                    )
                    fallback_warnings.extend(texture_warnings)
                    try:
                        collection, imported, material_stats = import_bfres_direct(
                            model_data,
                            None,
                            collection_name,
                            material_metadata,
                            texture_images,
                            import_lods=settings.import_lods,
                            library_path=native_library,
                        )
                    except NativeBridgeError as error:
                        fallback_warnings.append(
                            "direct static import failed; using glTF fallback: "
                            f"{error}"
                        )
                        for image in tuple(set(bpy.data.images) - existing_images):
                            if image.users == 0:
                                bpy.data.images.remove(image)
                    else:
                        context.scene.collection.children.link(collection)
                        if settings.scale != 1.0:
                            for obj in imported:
                                obj.scale = tuple(
                                    component * settings.scale
                                    for component in obj.scale
                                )
                        collection["oot_remake_nup"] = str(indexed.archive.path)
                        collection["oot_remake_asset"] = settings.asset_path
                        collection["oot_remake_version"] = indexed.package.version
                        collection["oot_remake_texture_count"] = len(texture_images)
                        collection["oot_remake_missing_textures"] = "\n".join(
                            texture_warnings
                        )
                        collection["oot_remake_material_semantics"] = (
                            material_stats.semantics_version
                        )
                        collection["oot_remake_masks_bypassed"] = (
                            material_stats.masks_bypassed
                        )
                        collection["oot_remake_tints_reconstructed"] = (
                            material_stats.tints_reconstructed
                        )
                        collection["oot_remake_vertex_colors_preserved"] = (
                            material_stats.vertex_colors_preserved
                        )
                        collection["oot_remake_layers_reconstructed"] = (
                            material_stats.layers_reconstructed
                        )
                        collection["oot_remake_import_path"] = "direct"
                        window_manager.progress_update(5)
                        succeeded = True
                        return ImportResult(
                            object_count=len(imported),
                            texture_count=len(texture_images),
                            warnings=tuple(fallback_warnings),
                        )
                else:
                    fallback_warnings.append(
                        f"native bridge is missing at {native_library}; "
                        "using glTF fallback"
                    )

            converter = _converter_path(context)
            if not converter.is_file():
                detail = (
                    "\n".join(fallback_warnings) + "\n"
                    if fallback_warnings
                    else ""
                )
                raise ConversionError(
                    f"{detail}bundled converter is missing: {converter}. "
                    "Build and install the extension with tools/build_addon.py"
                )

            model_path = work / SAFE_NAME.sub("_", source_name)
            _write_resource(model_path, model_data)

            decoded_textures: list[Path] = []
            for index, resource_path in enumerate(texture_paths):
                texture_data = indexed.decoder.decode(resource_path, expected_magic=b"BNTX")
                texture_name = PurePosixPath(resource_path).name[:-3]
                destination = work / "textures" / SAFE_NAME.sub("_", texture_name)
                if destination.exists():
                    destination = destination.with_name(f"{destination.stem}_{index:04d}{destination.suffix}")
                _write_resource(destination, texture_data)
                decoded_textures.append(destination)
                if texture_paths:
                    window_manager.progress_update(1 + index / len(texture_paths))

            output = work / "model.glb"
            report = convert_bfres(
                converter,
                model_path,
                output,
                decoded_textures,
                import_lods=settings.import_lods,
            )
            window_manager.progress_update(3)

            import_path = output
            rig_report = None
            build_rig = animation is not None
            if skeleton is None and not glb_has_meshes(output):
                # Locators, probes and animation reference rigs have no
                # meshes, so glTF has no skin to turn into an armature.
                skeleton = read_model_skeletons(model_data)[0]
                build_rig = True
            if build_rig and skeleton is not None:
                import_path = work / "model_rig.glb"
                rig_report = prepare_skeleton_glb(
                    output,
                    import_path,
                    skeleton,
                )
            window_manager.progress_update(4)

            collection, imported, missing_images, material_stats = _import_glb(
                context,
                import_path,
                collection_name,
                settings.scale,
                material_metadata,
                remove_helpers=build_rig,
            )
            animation_report: BlenderAnimationReport | None = None
            if animation is not None and skeleton is not None:
                try:
                    skeleton_names = {bone.name for bone in skeleton.bones}
                    armatures = [
                        obj
                        for obj in imported
                        if obj.type == "ARMATURE"
                        and skeleton_names.issubset(obj.pose.bones.keys())
                    ]
                    if len(armatures) != 1:
                        raise AnimationError(
                            f"expected one complete Blender armature for "
                            f"{skeleton.name!r}, found {len(armatures)}"
                        )
                    animation_report = create_skeletal_action(
                        context,
                        armatures[0],
                        skeleton,
                        animation,
                        fps=settings.animation_fps,
                    )
                    fps_changed = True
                except Exception:
                    for obj in tuple(imported):
                        bpy.data.objects.remove(obj, do_unlink=True)
                    bpy.data.collections.remove(collection)
                    raise
            collection["oot_remake_nup"] = str(indexed.archive.path)
            collection["oot_remake_asset"] = settings.asset_path
            collection["oot_remake_version"] = indexed.package.version
            collection["oot_remake_texture_count"] = len(decoded_textures)
            collection["oot_remake_missing_textures"] = "\n".join(missing_images)
            collection["oot_remake_material_semantics"] = (
                material_stats.semantics_version
            )
            collection["oot_remake_masks_bypassed"] = material_stats.masks_bypassed
            collection["oot_remake_tints_reconstructed"] = material_stats.tints_reconstructed
            collection["oot_remake_vertex_colors_preserved"] = (
                material_stats.vertex_colors_preserved
            )
            collection["oot_remake_layers_reconstructed"] = (
                material_stats.layers_reconstructed
            )
            collection["oot_remake_import_path"] = "gltf"
            if animation_report is not None:
                collection["oot_remake_animation_asset"] = settings.animation_path
                collection["oot_remake_animation"] = animation_report.animation
                collection["oot_remake_animation_frames"] = animation_report.frame_count
                collection["oot_remake_animation_fps"] = settings.animation_fps
                collection["oot_remake_animation_loop"] = animation.loop
                collection["oot_remake_animation_bones"] = animation_report.matched_bones
                collection["oot_remake_animation_keyed_bones"] = animation_report.keyed_bones
                collection["oot_remake_animation_compensating_bones"] = (
                    animation_report.compensating_bones
                )
                collection["oot_remake_animation_realigned_bones"] = (
                    animation_report.realigned_bones
                )
                collection["oot_remake_animation_transfer_error"] = (
                    animation_report.maximum_transfer_error
                )
                collection["oot_remake_promoted_joints"] = (
                    rig_report.promoted_joints if rig_report is not None else 0
                )
                collection["oot_remake_animation_source"] = "BFRES FSKA direct Action"
            window_manager.progress_update(5)
        result = ImportResult(
            object_count=len(imported),
            texture_count=len(texture_paths),
            warnings=tuple(fallback_warnings) + _actionable_warnings(report),
            animation=animation_report.animation if animation_report is not None else None,
            animation_frames=animation_report.frame_count if animation_report is not None else 0,
        )
        succeeded = True
        return result
    finally:
        if fps_changed and not succeeded:
            context.scene.render.fps = previous_fps
            context.scene.render.fps_base = previous_fps_base
            context.scene.frame_start = previous_frame_start
            context.scene.frame_end = previous_frame_end
        window_manager.progress_end()


def _selected_level(indexed: IndexedArchive, identifier: str) -> LevelDefinition:
    for level in indexed.levels:
        if level.identifier == identifier:
            return level
    raise LevelError("select an indexed level placement before importing")


def _resolve_indexed_actor(
    indexed: IndexedArchive,
    gyaml: str,
) -> ActorModelResolution:
    if indexed.actor_models is None:
        indexed.actor_models = {}
    cached = indexed.actor_models.get(gyaml)
    if cached is not None:
        return cached
    pack_path = actor_pack_path(gyaml)
    if pack_path not in indexed.entries:
        raise LevelError(f"actor pack does not exist: {pack_path}")
    pack_data = indexed.decoder.decode(pack_path, expected_magic=b"SARC")
    resolution = resolve_actor_models(
        gyaml,
        read_sarc(pack_data),
        indexed.entries,
    )
    indexed.actor_models[gyaml] = resolution
    return resolution


_GAME_TO_BLENDER = Matrix(
    (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, -1.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
)


def _placement_matrix(actor: PlacementActor, global_scale: float) -> Matrix:
    game_matrix = (
        Matrix.Translation(Vector(actor.translate))
        @ Euler(actor.rotate, "XYZ").to_matrix().to_4x4()
        @ Matrix.Diagonal(Vector((*actor.scale, 1.0)))
    )
    return (
        Matrix.Scale(global_scale, 4)
        @ _GAME_TO_BLENDER
        @ game_matrix
        @ _GAME_TO_BLENDER.inverted()
    )


def _actor_object_name(actor: PlacementActor) -> str:
    label = SAFE_NAME.sub("_", actor.name or actor.gyaml).strip("_") or "Actor"
    return f"{label}_{actor.actor_hash & 0xFFFFFFFFFFFFFFFF:016X}"


def _set_actor_properties(
    obj,
    actor: PlacementActor,
    resolution: ActorModelResolution,
) -> None:
    obj["oot_remake_gyaml"] = actor.gyaml
    obj["oot_remake_actor_hash"] = f"{actor.actor_hash & 0xFFFFFFFFFFFFFFFF:016X}"
    obj["oot_remake_srt_hash"] = (
        f"{actor.srt_hash & 0xFFFFFFFF:08X}" if actor.srt_hash is not None else ""
    )
    obj["oot_remake_placement_asset"] = actor.source_path
    obj["oot_remake_dynamic"] = actor.dynamic
    obj["oot_remake_model_resources"] = "\n".join(
        model.resource_path for model in resolution.models
    )
    if resolution.reason is not None:
        obj["oot_remake_resolution"] = resolution.reason


def _create_visual_instance(
    target: bpy.types.Collection,
    actor: PlacementActor,
    resolution: ActorModelResolution,
    sources: dict[ActorModel, bpy.types.Collection],
    global_scale: float,
) -> None:
    name = _actor_object_name(actor)
    collections = tuple(sources[model] for model in resolution.models)
    if len(collections) == 1:
        instance = bpy.data.objects.new(name, None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collections[0]
        instance.matrix_world = _placement_matrix(actor, global_scale)
        target.objects.link(instance)
        _set_actor_properties(instance, actor, resolution)
        return

    parent = bpy.data.objects.new(name, None)
    parent.empty_display_type = "PLAIN_AXES"
    parent.matrix_world = _placement_matrix(actor, global_scale)
    target.objects.link(parent)
    _set_actor_properties(parent, actor, resolution)
    for index, collection in enumerate(collections):
        instance = bpy.data.objects.new(f"{name}_model_{index:02d}", None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = collection
        instance.parent = parent
        instance.matrix_parent_inverse = Matrix.Identity(4)
        instance.matrix_basis = Matrix.Identity(4)
        target.objects.link(instance)


def _create_nonvisual_marker(
    target: bpy.types.Collection,
    actor: PlacementActor,
    resolution: ActorModelResolution,
    global_scale: float,
) -> None:
    marker = bpy.data.objects.new(_actor_object_name(actor), None)
    marker.empty_display_type = "CUBE"
    marker.empty_display_size = 0.5
    marker.matrix_world = _placement_matrix(actor, global_scale)
    target.objects.link(marker)
    _set_actor_properties(marker, actor, resolution)


def _available_actor_resolution(
    resolution: ActorModelResolution,
    failures: dict[ActorModel, str],
) -> ActorModelResolution:
    failed = tuple(model for model in resolution.models if model in failures)
    if not failed:
        return resolution
    available = tuple(model for model in resolution.models if model not in failures)
    failure_reason = "model conversion failed: " + ", ".join(
        f"{model.resource_path} ({failures[model]})" for model in failed
    )
    reason = (
        f"{resolution.reason}; {failure_reason}"
        if resolution.reason is not None
        else failure_reason
    )
    return ActorModelResolution(resolution.gyaml, available, reason)


def _delete_collection(collection: bpy.types.Collection) -> None:
    if bpy.data.collections.get(collection.name) is None:
        return
    for child in tuple(collection.children):
        _delete_collection(child)
    for obj in tuple(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)


def _cleanup_orphans(existing: dict[str, set]) -> None:
    for name, previous in existing.items():
        datablocks = getattr(bpy.data, name)
        for datablock in tuple(set(datablocks) - previous):
            if datablock.users == 0:
                datablocks.remove(datablock)


def _load_direct_textures(
    indexed: IndexedArchive,
    material_metadata: dict[str, MaterialMetadata],
    native_library: Path,
    work: Path,
    texture_cache: dict[tuple[str, str], bpy.types.Image],
    texture_failures: dict[tuple[str, str], str],
    *,
    enabled: bool,
    max_model_textures: int,
    max_total_textures: int,
) -> tuple[dict[str, bpy.types.Image], tuple[str, ...]]:
    if not enabled:
        return {}, ()

    requests: dict[str, str] = {}
    missing: set[str] = set()
    for metadata in material_metadata.values():
        for texture_name in metadata.samplers.values():
            path = texture_resource_path(texture_name, indexed.textures)
            if path is None:
                missing.add(texture_name)
            else:
                requests[texture_name] = path
    if len(requests) > max_model_textures:
        raise ResourceError(
            f"model references {len(requests)} textures, above the configured "
            f"limit of {max_model_textures}"
        )

    requested_keys = {(path, name) for name, path in requests.items()}
    known_keys = set(texture_cache).union(texture_failures)
    if len(known_keys.union(requested_keys)) > max_total_textures:
        raise ResourceError(
            f"import references more than the configured "
            f"{max_total_textures} unique textures"
        )

    warnings: list[str] = []
    for texture_name in sorted(missing):
        key = ("", texture_name)
        if key not in texture_failures:
            reason = "referenced texture resource does not exist"
            texture_failures[key] = reason
            warnings.append(f"{texture_name}: {reason}")

    for texture_name, resource_path in sorted(requests.items()):
        key = (resource_path, texture_name)
        if key in texture_cache or key in texture_failures:
            continue
        try:
            texture_data = indexed.decoder.decode(
                resource_path,
                expected_magic=b"BNTX",
            )
            decoded = decode_bntx_png(
                texture_data,
                texture_name,
                library_path=native_library,
            )
            texture_cache[key] = load_texture_image(
                decoded,
                work / "native_textures",
                source_path=resource_path,
            )
        except (NativeBridgeError, ResourceError, OSError) as error:
            reason = str(error)
            texture_failures[key] = reason
            warnings.append(f"{resource_path}: {reason}")

    return (
        {
            texture_name: texture_cache[(resource_path, texture_name)]
            for texture_name, resource_path in requests.items()
            if (resource_path, texture_name) in texture_cache
        },
        tuple(warnings),
    )


def _import_level_source(
    context,
    indexed: IndexedArchive,
    settings: "OOTR_Settings",
    converter: Path,
    model: ActorModel,
    work: Path,
    index: int,
    texture_cache: dict[str, Path],
    direct_texture_cache: dict[tuple[str, str], bpy.types.Image],
    direct_texture_failures: dict[tuple[str, str], str],
) -> tuple[bpy.types.Collection, tuple[str, ...]]:
    model_data = indexed.decoder.decode(model.resource_path, expected_magic=b"FRES    ")
    material_metadata = _read_model_materials(indexed, model_data)
    warnings: list[str] = []
    native_library = _native_library_path()
    if settings.level_direct_import and native_library.is_file():
        texture_images, texture_warnings = _load_direct_textures(
            indexed,
            material_metadata,
            native_library,
            work,
            direct_texture_cache,
            direct_texture_failures,
            enabled=settings.level_import_textures,
            max_model_textures=settings.max_textures,
            max_total_textures=settings.max_level_textures,
        )
        warnings.extend(texture_warnings)
        try:
            collection, _imported, material_stats = import_bfres_direct(
                model_data,
                model.fmdb_name,
                f"OOTR_Source_{model.fmdb_name}",
                material_metadata,
                texture_images,
                import_lods=settings.import_lods,
                library_path=native_library,
            )
        except NativeBridgeError as error:
            warnings.append(
                f"direct import failed for {model.resource_path}; using glTF fallback: {error}"
            )
        else:
            collection["oot_remake_nup"] = str(indexed.archive.path)
            collection["oot_remake_asset"] = model.resource_path
            collection["oot_remake_fmdb"] = model.fmdb_name
            collection["oot_remake_texture_count"] = len(texture_images)
            collection["oot_remake_missing_textures"] = "\n".join(texture_warnings)
            collection["oot_remake_material_semantics"] = material_stats.semantics_version
            collection["oot_remake_layers_reconstructed"] = (
                material_stats.layers_reconstructed
            )
            collection["oot_remake_import_path"] = "direct"
            return collection, tuple(warnings)
    elif settings.level_direct_import:
        warnings.append(
            f"native bridge is missing at {native_library}; using glTF fallback"
        )

    texture_paths = _model_texture_paths(
        indexed,
        model_data,
        enabled=settings.level_import_textures,
        limit=settings.max_textures,
    )
    uncached_textures = [path for path in texture_paths if path not in texture_cache]
    if len(texture_cache) + len(uncached_textures) > settings.max_level_textures:
        raise ResourceError(
            f"level references more than the configured {settings.max_level_textures} "
            "unique textures"
        )

    model_directory = work / f"model_{index:04d}"
    source_name = PurePosixPath(model.resource_path).name[:-3]
    model_path = model_directory / SAFE_NAME.sub("_", source_name)
    _write_resource(model_path, model_data)

    for resource_path in uncached_textures:
        texture_data = indexed.decoder.decode(resource_path, expected_magic=b"BNTX")
        texture_name = PurePosixPath(resource_path).name[:-3]
        destination = work / "textures" / (
            f"{len(texture_cache):05d}_{SAFE_NAME.sub('_', texture_name)}"
        )
        _write_resource(destination, texture_data)
        texture_cache[resource_path] = destination
    decoded_textures = tuple(texture_cache[path] for path in texture_paths)

    output = model_directory / "model.glb"
    try:
        report = convert_bfres(
            converter,
            model_path,
            output,
            decoded_textures,
            import_lods=settings.import_lods,
            model_name=model.fmdb_name,
        )
    except ConversionError as error:
        if warnings:
            raise ConversionError(f"{warnings[-1]}\nglTF fallback failed: {error}") from error
        raise
    collection, _imported, missing_images, material_stats = _import_glb(
        context,
        output,
        f"OOTR_Source_{model.fmdb_name}",
        1.0,
        material_metadata,
    )
    collection["oot_remake_nup"] = str(indexed.archive.path)
    collection["oot_remake_asset"] = model.resource_path
    collection["oot_remake_fmdb"] = model.fmdb_name
    collection["oot_remake_texture_count"] = len(texture_paths)
    collection["oot_remake_missing_textures"] = "\n".join(missing_images)
    collection["oot_remake_material_semantics"] = material_stats.semantics_version
    collection["oot_remake_layers_reconstructed"] = (
        material_stats.layers_reconstructed
    )
    collection["oot_remake_import_path"] = "gltf"
    if context.scene.collection.children.get(collection.name) is not None:
        context.scene.collection.children.unlink(collection)
    warnings.extend(_actionable_warnings(report))
    if not settings.level_import_textures:
        warnings = [
            warning
            for warning in warnings
            if not (
                warning.startswith("warning: Texture ")
                and "or the supplied texture archives" in warning
            )
        ]
    return collection, tuple(warnings)


def _read_level_terrain(
    indexed: IndexedArchive,
    level: LevelDefinition,
    settings: "OOTR_Settings",
) -> tuple[TerrainScene, ...]:
    if not settings.level_import_terrain:
        return ()
    paths = terrain_scene_paths(indexed.entries, level.world, level.name)
    scenes = tuple(
        read_tscb(
            indexed.decoder.decode(path, expected_magic=b"TSCB"),
            path,
        )
        for path in paths
    )
    tile_count = sum(len(scene.height_areas) for scene in scenes)
    if tile_count > settings.max_terrain_tiles:
        raise TerrainError(
            f"level needs {tile_count:,} terrain height tiles, above the configured "
            f"limit of {settings.max_terrain_tiles:,}"
        )
    return scenes


def _import_indexed_level(
    context,
    settings: "OOTR_Settings",
) -> LevelImportResult:
    indexed = _index_archive(settings.nup_path)
    level = _selected_level(indexed, settings.level_id)
    terrain_scenes = _read_level_terrain(indexed, level, settings)
    converter = _converter_path(context)
    if not converter.is_file():
        raise ConversionError(
            f"bundled converter is missing: {converter}. Build and install the extension with "
            "tools/build_addon.py"
        )

    window_manager = context.window_manager
    window_manager.progress_begin(0, 100)
    created_collections: list[bpy.types.Collection] = []
    existing_orphans = {
        name: set(getattr(bpy.data, name))
        for name in (
            "armatures",
            "cameras",
            "curves",
            "images",
            "lights",
            "materials",
            "meshes",
        )
    }
    try:
        placement_sets = []
        for path in level.paths:
            placement_sets.append(
                read_placement(
                    indexed.decoder.decode(path, expected_magic=b"YB"),
                    path,
                    dynamic="_Dynamic." in path,
                )
            )
        actors = tuple(actor for placement in placement_sets for actor in placement.actors)
        skipped_count = sum(placement.skipped_without_transform for placement in placement_sets)
        if len(actors) > settings.max_level_actors:
            raise LevelError(
                f"level has {len(actors):,} placements, above the configured limit "
                f"of {settings.max_level_actors:,}"
            )
        window_manager.progress_update(5)

        actor_types = tuple(sorted({actor.gyaml for actor in actors}))
        resolutions: dict[str, ActorModelResolution] = {}
        for index, gyaml in enumerate(actor_types):
            resolutions[gyaml] = _resolve_indexed_actor(indexed, gyaml)
            window_manager.progress_update(5 + 15 * (index + 1) / max(1, len(actor_types)))

        models = tuple(
            sorted(
                {
                    model
                    for resolution in resolutions.values()
                    for model in resolution.models
                },
                key=lambda model: (model.resource_path, model.fmdb_name),
            )
        )
        if len(models) > settings.max_level_models:
            raise LevelError(
                f"level needs {len(models):,} unique models, above the configured limit "
                f"of {settings.max_level_models:,}"
            )

        sources: dict[ActorModel, bpy.types.Collection] = {}
        failed_models: dict[ActorModel, str] = {}
        warnings: list[str] = []
        texture_cache: dict[str, Path] = {}
        direct_texture_cache: dict[tuple[str, str], bpy.types.Image] = {}
        direct_texture_failures: dict[tuple[str, str], str] = {}
        with tempfile.TemporaryDirectory(prefix="oot_remake_level_") as temporary:
            work = Path(temporary)
            for index, model in enumerate(models):
                try:
                    source, model_warnings = _import_level_source(
                        context,
                        indexed,
                        settings,
                        converter,
                        model,
                        work,
                        index,
                        texture_cache,
                        direct_texture_cache,
                        direct_texture_failures,
                    )
                except ConversionError as error:
                    lines = str(error).strip().splitlines()
                    detail = " | ".join(lines[-3:])
                    failed_models[model] = detail
                    warnings.append(
                        f"could not convert {model.resource_path} model "
                        f"{model.fmdb_name!r}: {detail}"
                    )
                    window_manager.progress_update(
                        20 + 55 * (index + 1) / max(1, len(models))
                    )
                    continue
                created_collections.append(source)
                sources[model] = source
                warnings.extend(model_warnings)
                window_manager.progress_update(
                    20 + 55 * (index + 1) / max(1, len(models))
                )

        level_collection = bpy.data.collections.new(f"Level_{SAFE_NAME.sub('_', level.name)}")
        visual_collection = bpy.data.collections.new("Visual Placements")
        nonvisual_collection = bpy.data.collections.new("Nonvisual Placements")
        created_collections.append(level_collection)
        context.scene.collection.children.link(level_collection)
        level_collection.children.link(visual_collection)
        level_collection.children.link(nonvisual_collection)

        terrain_results: list[TerrainImportStats] = []
        if terrain_scenes:
            terrain_collection = bpy.data.collections.new("Terrain")
            level_collection.children.link(terrain_collection)
            for index, scene in enumerate(terrain_scenes):
                _collection, stats = import_terrain_scene(
                    scene,
                    terrain_collection,
                    indexed.decoder.decode,
                    sample_step=int(settings.level_terrain_sample_step),
                    scale=settings.scale,
                )
                terrain_results.append(stats)
                window_manager.progress_update(
                    75 + 10 * (index + 1) / len(terrain_scenes)
                )

        terrain_tile_count = sum(result.tile_count for result in terrain_results)
        terrain_vertex_count = sum(result.vertex_count for result in terrain_results)
        terrain_face_count = sum(result.face_count for result in terrain_results)
        terrain_material_map_count = sum(
            result.material_map_count for result in terrain_results
        )
        visual_count = 0
        nonvisual_count = 0
        failed_placement_count = 0
        for index, actor in enumerate(actors):
            original_resolution = resolutions[actor.gyaml]
            resolution = _available_actor_resolution(original_resolution, failed_models)
            if len(resolution.models) != len(original_resolution.models):
                failed_placement_count += 1
            if resolution.models:
                _create_visual_instance(
                    visual_collection,
                    actor,
                    resolution,
                    sources,
                    settings.scale,
                )
                visual_count += 1
            elif settings.include_nonvisual_actors:
                _create_nonvisual_marker(
                    nonvisual_collection,
                    actor,
                    resolution,
                    settings.scale,
                )
                nonvisual_count += 1
            window_manager.progress_update(
                85 + 15 * (index + 1) / max(1, len(actors))
            )

        level_collection["oot_remake_nup"] = str(indexed.archive.path)
        level_collection["oot_remake_level"] = level.identifier
        level_collection["oot_remake_level_assets"] = "\n".join(level.paths)
        level_collection["oot_remake_version"] = indexed.package.version
        level_collection["oot_remake_placement_count"] = len(actors)
        level_collection["oot_remake_visual_count"] = visual_count
        level_collection["oot_remake_nonvisual_count"] = nonvisual_count
        level_collection["oot_remake_skipped_without_transform"] = skipped_count
        level_collection["oot_remake_source_model_count"] = len(models)
        level_collection["oot_remake_imported_source_model_count"] = len(sources)
        level_collection["oot_remake_failed_source_model_count"] = len(failed_models)
        level_collection["oot_remake_failed_source_models"] = "\n".join(
            f"{model.resource_path}: {reason}"
            for model, reason in sorted(
                failed_models.items(),
                key=lambda item: (item[0].resource_path, item[0].fmdb_name),
            )
        )
        level_collection["oot_remake_failed_placement_count"] = failed_placement_count
        texture_count = len(direct_texture_cache) + len(texture_cache)
        level_collection["oot_remake_texture_count"] = texture_count
        level_collection["oot_remake_direct_source_model_count"] = sum(
            source.get("oot_remake_import_path") == "direct"
            for source in sources.values()
        )
        level_collection["oot_remake_gltf_source_model_count"] = sum(
            source.get("oot_remake_import_path") == "gltf"
            for source in sources.values()
        )
        level_collection["oot_remake_includes_terrain"] = terrain_tile_count > 0
        level_collection["oot_remake_terrain_assets"] = "\n".join(
            scene.source_path for scene in terrain_scenes
        )
        level_collection["oot_remake_terrain_scene_count"] = len(terrain_scenes)
        level_collection["oot_remake_terrain_tile_count"] = terrain_tile_count
        level_collection["oot_remake_terrain_vertex_count"] = terrain_vertex_count
        level_collection["oot_remake_terrain_face_count"] = terrain_face_count
        level_collection["oot_remake_terrain_material_map_count"] = (
            terrain_material_map_count
        )
        level_collection["oot_remake_terrain_sample_step"] = int(
            settings.level_terrain_sample_step
        )
        level_collection["oot_remake_coordinate_conversion"] = "game Y-up to Blender Z-up"

        if skipped_count:
            warnings.append(f"{skipped_count} placement(s) without transforms were skipped")
        return LevelImportResult(
            placement_count=len(actors),
            visual_count=visual_count,
            nonvisual_count=nonvisual_count,
            source_model_count=len(models),
            texture_count=texture_count,
            skipped_count=skipped_count,
            failed_source_model_count=len(failed_models),
            failed_placement_count=failed_placement_count,
            terrain_scene_count=len(terrain_scenes),
            terrain_tile_count=terrain_tile_count,
            terrain_vertex_count=terrain_vertex_count,
            terrain_face_count=terrain_face_count,
            warnings=tuple(dict.fromkeys(warnings)),
        )
    except Exception:
        for collection in reversed(created_collections):
            _delete_collection(collection)
        _cleanup_orphans(existing_orphans)
        raise
    finally:
        window_manager.progress_end()


class OOTR_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    converter_path: StringProperty(
        name="BfrAss executable",
        description="Optional override for the bundled BFRES-to-glTF converter",
        subtype="FILE_PATH",
        default="",
    )

    def draw(self, _context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "converter_path")
        layout.label(text="Leave empty to use bin/bfrass.exe from the installed extension.")


class OOTR_Settings(bpy.types.PropertyGroup):
    nup_path: StringProperty(
        name="Game NUP",
        description="Your own Ocarina of Time Remake NUP dump",
        subtype="FILE_PATH",
        default="",
    )
    asset_path: StringProperty(name="RomFS model", default="")
    level_id: StringProperty(name="Level placement", default="")
    animation_path: StringProperty(name="RomFS animation", default="")
    animation_clip: StringProperty(name="Animation clip", default="")
    import_textures: BoolProperty(
        name="Materials and textures",
        description="Resolve referenced BNTX resources and embed them in the imported model",
        default=True,
    )
    import_lods: BoolProperty(
        name="All LODs",
        description="Import every level of detail instead of only the highest-detail mesh",
        default=False,
    )
    level_import_textures: BoolProperty(
        name="Level textures",
        description=(
            "Resolve and embed textures for every unique level model; disabled by default "
            "because large levels can require thousands of textures"
        ),
        default=False,
    )
    level_direct_import: BoolProperty(
        name="Direct native import",
        description=(
            "Import level meshes and textures through the bundled in-process bridge; "
            "unsupported resources fall back to glTF"
        ),
        default=True,
    )
    level_import_terrain: BoolProperty(
        name="TerrainArc geometry",
        description="Import native adaptive TerrainArc height geometry and material colors",
        default=True,
    )
    level_terrain_sample_step: EnumProperty(
        name="Terrain sampling",
        description=(
            "Height-map sampling step; lower values preserve more terrain detail "
            "but create much larger meshes"
        ),
        items=(
            ("1", "Full (1x)", "Use every TerrainArc height sample"),
            ("2", "High (2x)", "Use every second TerrainArc height sample"),
            ("4", "Balanced (4x)", "Use every fourth TerrainArc height sample"),
            ("8", "Preview (8x)", "Use every eighth TerrainArc height sample"),
        ),
        default="4",
    )
    include_nonvisual_actors: BoolProperty(
        name="Nonvisual markers",
        description="Create labeled cube empties for logic, area, anchor, and effect placements",
        default=True,
    )
    scale: FloatProperty(
        name="Scale",
        description="Scale applied to imported root objects",
        default=1.0,
        min=0.0001,
        soft_max=100.0,
    )
    max_textures: IntProperty(
        name="Texture safety limit",
        description="Refuse unexpectedly large texture sets before extracting them",
        default=512,
        min=1,
        max=4096,
    )
    max_level_actors: IntProperty(
        name="Level actor limit",
        description="Refuse unexpectedly large placement sets before importing model resources",
        default=20_000,
        min=1,
        max=1_000_000,
    )
    max_level_models: IntProperty(
        name="Level model limit",
        description="Refuse levels needing more than this many unique BFRES model conversions",
        default=512,
        min=1,
        max=8192,
    )
    max_level_textures: IntProperty(
        name="Level texture limit",
        description="Refuse levels exceeding this many unique BNTX texture resources",
        default=4096,
        min=1,
        max=100_000,
    )
    max_terrain_tiles: IntProperty(
        name="Terrain tile limit",
        description="Refuse levels exceeding this many TerrainArc height resources",
        default=512,
        min=1,
        max=100_000,
    )
    animation_fps: IntProperty(
        name="Animation FPS",
        description="Source frame rate; importing an animation sets the scene to this rate",
        default=60,
        min=1,
        max=240,
    )
    status: StringProperty(name="Status", default="", options={"SKIP_SAVE"})


class OOTR_OT_index_nup(bpy.types.Operator):
    bl_idname = "oot_remake.index_nup"
    bl_label = "Index NUP"
    bl_description = "Validate the NUP table of contents and index models, animations, and levels"

    def execute(self, context):
        settings = context.scene.oot_remake_importer
        try:
            indexed = _index_archive(settings.nup_path)
        except (NupError, LevelError, OSError) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        title = indexed.package.metadata.get("title_name", "Ocarina of Time Remake")
        settings.status = (
            f"{title}: {len(indexed.models):,} models and "
            f"{len(indexed.animations):,} animations across "
            f"{len(indexed.levels):,} level variants indexed"
        )
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class OOTR_OT_choose_asset(bpy.types.Operator):
    bl_idname = "oot_remake.choose_asset"
    bl_label = "Choose Model"
    bl_description = "Search models in the indexed RomFS"
    bl_property = "asset"

    asset: EnumProperty(name="Model", items=_asset_enum_items)

    def invoke(self, context, _event):
        settings = context.scene.oot_remake_importer
        try:
            indexed = _index_archive(settings.nup_path)
        except (NupError, LevelError, OSError) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _set_asset_items(indexed.models)
        return context.window_manager.invoke_search_popup(self)

    def execute(self, context):
        path = _ASSET_PATHS.get(self.asset)
        if path is None:
            self.report({"ERROR"}, "the selected model is no longer in the index")
            return {"CANCELLED"}
        settings = context.scene.oot_remake_importer
        settings.asset_path = path
        settings.status = f"Selected {path}"
        return {"FINISHED"}


class OOTR_OT_choose_level(bpy.types.Operator):
    bl_idname = "oot_remake.choose_level"
    bl_label = "Choose Level"
    bl_description = "Search static and dynamic BCETT level placement pairs"
    bl_property = "level"

    level: EnumProperty(name="Level placement", items=_level_enum_items)

    def invoke(self, context, _event):
        settings = context.scene.oot_remake_importer
        try:
            indexed = _index_archive(settings.nup_path)
        except (NupError, LevelError, OSError) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _set_level_items(indexed.levels)
        return context.window_manager.invoke_search_popup(self)

    def execute(self, context):
        identifier = _LEVEL_IDS.get(self.level)
        if identifier is None:
            self.report({"ERROR"}, "the selected level is no longer in the index")
            return {"CANCELLED"}
        settings = context.scene.oot_remake_importer
        settings.level_id = identifier
        settings.status = f"Selected level placement {identifier}"
        return {"FINISHED"}


class OOTR_OT_choose_animation(bpy.types.Operator):
    bl_idname = "oot_remake.choose_animation"
    bl_label = "Choose Animation Resource"
    bl_description = "Search standalone skeletal-animation resources in the indexed RomFS"
    bl_property = "asset"

    asset: EnumProperty(name="Animation resource", items=_animation_enum_items)

    def invoke(self, context, _event):
        settings = context.scene.oot_remake_importer
        try:
            indexed = _index_archive(settings.nup_path)
        except (NupError, LevelError, OSError) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _set_animation_items(indexed.animations)
        return context.window_manager.invoke_search_popup(self)

    def execute(self, context):
        path = _ANIMATION_PATHS.get(self.asset)
        if path is None:
            self.report({"ERROR"}, "the selected animation is no longer in the index")
            return {"CANCELLED"}
        settings = context.scene.oot_remake_importer
        settings.animation_path = path
        settings.animation_clip = ""
        _set_clip_items(())
        settings.status = f"Selected animation resource {path}"
        return {"FINISHED"}


class OOTR_OT_choose_animation_clip(bpy.types.Operator):
    bl_idname = "oot_remake.choose_animation_clip"
    bl_label = "Choose Animation Clip"
    bl_description = "Search skeletal animation clips in the selected resource"
    bl_property = "clip"

    clip: EnumProperty(name="Animation clip", items=_clip_enum_items)

    def invoke(self, context, _event):
        settings = context.scene.oot_remake_importer
        try:
            indexed = _index_archive(settings.nup_path)
            if settings.animation_path not in indexed.animations:
                raise ResourceError("choose an indexed animation resource first")
            animations = _load_animation_set(indexed, settings.animation_path)
        except (NupError, ResourceError, AnimationError, LevelError, OSError) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        _set_clip_items(animations)
        return context.window_manager.invoke_search_popup(self)

    def execute(self, context):
        name = _CLIP_NAMES.get(self.clip)
        if name is None:
            self.report({"ERROR"}, "the selected clip is no longer available")
            return {"CANCELLED"}
        settings = context.scene.oot_remake_importer
        settings.animation_clip = name
        settings.status = f"Selected animation clip {name}"
        return {"FINISHED"}


class OOTR_OT_clear_animation(bpy.types.Operator):
    bl_idname = "oot_remake.clear_animation"
    bl_label = "Clear Animation"
    bl_description = "Import the model and rig without a skeletal animation"

    def execute(self, context):
        settings = context.scene.oot_remake_importer
        settings.animation_path = ""
        settings.animation_clip = ""
        _set_clip_items(())
        settings.status = "Animation selection cleared"
        return {"FINISHED"}


class OOTR_OT_import_model(bpy.types.Operator):
    bl_idname = "oot_remake.import_model"
    bl_label = "Import Model"
    bl_description = "Import the selected model, rig, and optional skeletal animation"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.oot_remake_importer
        try:
            result = _import_indexed_model(context, settings)
        except (
            NupError,
            ResourceError,
            MaterialError,
            ConversionError,
            AnimationError,
            LevelError,
            OSError,
        ) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        summary = f"Imported {result.object_count} objects and {result.texture_count} textures"
        if result.animation is not None:
            summary += f" with {result.animation!r} ({result.animation_frames} frames)"
        if result.warnings:
            LOGGER.warning(
                "Import warnings for %s:\n%s",
                settings.asset_path,
                "\n".join(result.warnings),
            )
            settings.status = f"{summary} with {len(result.warnings)} warning(s)"
            self.report({"WARNING"}, settings.status)
        else:
            settings.status = summary
            self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class OOTR_OT_import_level(bpy.types.Operator):
    bl_idname = "oot_remake.import_level"
    bl_label = "Import Level Placements"
    bl_description = (
        "Import placed BFRES models, nonvisual markers, and native TerrainArc geometry"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.oot_remake_importer
        try:
            result = _import_indexed_level(context, settings)
        except (
            NupError,
            ResourceError,
            MaterialError,
            ConversionError,
            LevelError,
            TerrainError,
            OSError,
        ) as error:
            settings.status = str(error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        summary = (
            f"Imported {result.visual_count:,} visual and "
            f"{result.nonvisual_count:,} nonvisual placements from "
            f"{result.source_model_count - result.failed_source_model_count:,} "
            f"of {result.source_model_count:,} source models"
        )
        if result.terrain_tile_count:
            summary += (
                f", plus {result.terrain_tile_count:,} terrain tiles "
                f"({result.terrain_face_count:,} faces)"
            )
        if result.warnings:
            LOGGER.warning(
                "Level import warnings for %s:\n%s",
                settings.level_id,
                "\n".join(result.warnings),
            )
            settings.status = f"{summary} with {len(result.warnings)} warning(s)"
            self.report({"WARNING"}, settings.status)
        else:
            settings.status = summary
            self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class OOTR_OT_select_nup(bpy.types.Operator, ImportHelper):
    bl_idname = "oot_remake.select_nup"
    bl_label = "Select Ocarina of Time Remake NUP"
    bl_description = "Select and index a user-owned Ocarina of Time Remake NUP"

    filename_ext = ".nup"
    filter_glob: StringProperty(default="*.nup", options={"HIDDEN"})

    def execute(self, context):
        settings = context.scene.oot_remake_importer
        settings.nup_path = self.filepath
        result = bpy.ops.oot_remake.index_nup()
        if "FINISHED" not in result:
            return {"CANCELLED"}
        return {"FINISHED"}


class OOTR_PT_importer(bpy.types.Panel):
    bl_idname = "OOTR_PT_importer"
    bl_label = "Ocarina of Time Remake"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OoT Remake"

    def draw(self, context):
        settings = context.scene.oot_remake_importer
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        source = layout.box()
        source.label(text="User-owned game source", icon="FILE_FOLDER")
        source.prop(settings, "nup_path")
        row = source.row(align=True)
        row.operator(OOTR_OT_select_nup.bl_idname, text="Browse", icon="FILEBROWSER")
        row.operator(OOTR_OT_index_nup.bl_idname, text="Index", icon="FILE_REFRESH")

        level = layout.box()
        level.label(text="Level and terrain preview", icon="OUTLINER_COLLECTION")
        level.label(text="Placed models with optional native TerrainArc geometry", icon="INFO")
        if settings.level_id:
            level.label(text=settings.level_id.rsplit("/", 1)[-1].replace("_", " "))
            level.label(text=settings.level_id)
        else:
            level.label(text="No level selected", icon="INFO")
        level.operator(OOTR_OT_choose_level.bl_idname, icon="VIEWZOOM")
        level.prop(settings, "level_direct_import")
        level.prop(settings, "level_import_textures")
        level.prop(settings, "level_import_terrain")
        if settings.level_import_terrain:
            level.prop(settings, "level_terrain_sample_step")
        level.prop(settings, "include_nonvisual_actors")
        level.prop(settings, "import_lods")
        level.prop(settings, "scale")
        limits = level.column(align=True)
        limits.prop(settings, "max_level_actors")
        limits.prop(settings, "max_level_models")
        if settings.level_import_terrain:
            limits.prop(settings, "max_terrain_tiles")
        if settings.level_import_textures:
            limits.prop(settings, "max_level_textures")
            limits.prop(settings, "max_textures")
        level_import = level.row()
        level_import.scale_y = 1.4
        level_import.enabled = bool(settings.nup_path and settings.level_id)
        level_import.operator(OOTR_OT_import_level.bl_idname, icon="IMPORT")

        asset = layout.box()
        asset.label(text="Model resource", icon="MESH_DATA")
        if settings.asset_path:
            asset.label(text=PurePosixPath(settings.asset_path).name)
            asset.label(text=settings.asset_path)
        else:
            asset.label(text="No model selected", icon="INFO")
        asset.operator(OOTR_OT_choose_asset.bl_idname, icon="VIEWZOOM")

        animation = layout.box()
        animation.label(text="Skeletal animation", icon="ACTION")
        if settings.animation_path:
            animation.label(text=PurePosixPath(settings.animation_path).name)
            if settings.animation_clip:
                animation.label(text=settings.animation_clip, icon="ACTION")
            else:
                animation.label(text="No clip selected", icon="INFO")
        else:
            animation.label(text="Optional", icon="INFO")
        row = animation.row(align=True)
        row.operator(OOTR_OT_choose_animation.bl_idname, icon="VIEWZOOM")
        clear = row.row(align=True)
        clear.enabled = bool(settings.animation_path)
        clear.operator(OOTR_OT_clear_animation.bl_idname, text="", icon="X")
        if settings.animation_path:
            animation.operator(OOTR_OT_choose_animation_clip.bl_idname, icon="ACTION")
            animation.prop(settings, "animation_fps")

        options = layout.box()
        options.label(text="Import options", icon="PREFERENCES")
        options.prop(settings, "import_textures")
        options.prop(settings, "import_lods")
        options.prop(settings, "scale")
        if settings.import_textures:
            options.prop(settings, "max_textures")

        import_row = layout.row()
        import_row.scale_y = 1.4
        animation_ready = not settings.animation_path or bool(settings.animation_clip)
        import_row.enabled = bool(settings.nup_path and settings.asset_path and animation_ready)
        import_row.operator(OOTR_OT_import_model.bl_idname, icon="IMPORT")

        if settings.status:
            status = layout.box()
            status.label(text=settings.status, icon="INFO")


def _menu_import(self, _context):
    self.layout.operator(
        OOTR_OT_select_nup.bl_idname,
        text="Ocarina of Time Remake NUP (.nup)",
    )


CLASSES = (
    OOTR_AddonPreferences,
    OOTR_Settings,
    OOTR_OT_index_nup,
    OOTR_OT_choose_asset,
    OOTR_OT_choose_level,
    OOTR_OT_choose_animation,
    OOTR_OT_choose_animation_clip,
    OOTR_OT_clear_animation,
    OOTR_OT_import_model,
    OOTR_OT_import_level,
    OOTR_OT_select_nup,
    OOTR_PT_importer,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.oot_remake_importer = PointerProperty(type=OOTR_Settings)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    global _INDEXED_ARCHIVE, _ASSET_ITEMS, _ASSET_PATHS
    global _LEVEL_ITEMS, _LEVEL_IDS
    global _ANIMATION_ITEMS, _ANIMATION_PATHS, _CLIP_ITEMS, _CLIP_NAMES

    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    del bpy.types.Scene.oot_remake_importer
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _INDEXED_ARCHIVE = None
    _ASSET_ITEMS = []
    _ASSET_PATHS = {}
    _LEVEL_ITEMS = []
    _LEVEL_IDS = {}
    _ANIMATION_ITEMS = []
    _ANIMATION_PATHS = {}
    _CLIP_ITEMS = []
    _CLIP_NAMES = {}
