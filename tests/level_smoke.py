"""Optional Blender integration test for a level placement preview.

Run with Blender after building the native converter:

  blender --background --factory-startup --python tests/level_smoke.py -- \
    --nup <game.nup> \
    --level KokiriForest/Demo_ShootingLocation_Tapestry_ED \
    --converter <bfrass.exe> --output <result.blend> \
    --expect-placements 4 --expect-visual 2 --expect-nonvisual 2 \
    --expect-source-models 2
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import traceback
from pathlib import Path

import bpy


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nup", type=Path, required=True)
    parser.add_argument("--level", required=True)
    parser.add_argument("--converter", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--textures", action="store_true")
    parser.add_argument("--no-terrain", action="store_true")
    parser.add_argument("--terrain-step", choices=("1", "2", "4", "8"), default="4")
    parser.add_argument("--expect-placements", type=int)
    parser.add_argument("--expect-visual", type=int)
    parser.add_argument("--expect-nonvisual", type=int)
    parser.add_argument("--expect-source-models", type=int)
    parser.add_argument("--expect-failed-source-models", type=int)
    parser.add_argument("--expect-direct-source-models", type=int)
    parser.add_argument("--expect-gltf-source-models", type=int)
    parser.add_argument("--expect-terrain-scenes", type=int)
    parser.add_argument("--expect-terrain-tiles", type=int)
    parser.add_argument("--expect-terrain-vertices", type=int)
    parser.add_argument("--expect-terrain-faces", type=int)
    parser.add_argument("--expect-terrain-material-maps", type=int)
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1:])


def require_count(actual: int, expected: int | None, label: str) -> None:
    if expected is not None and actual != expected:
        raise RuntimeError(f"{label} is {actual}, expected {expected}")


def main() -> None:
    args = arguments()
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))
    os.environ["OOTR_BFRASS"] = str(args.converter.resolve())

    import oot_remake_importer

    oot_remake_importer.register()
    settings = bpy.context.scene.oot_remake_importer
    settings.nup_path = str(args.nup.resolve())
    settings.level_id = args.level
    settings.level_import_textures = args.textures
    settings.level_import_terrain = not args.no_terrain
    settings.level_terrain_sample_step = args.terrain_step
    index_result = bpy.ops.oot_remake.index_nup()
    if "FINISHED" not in index_result:
        raise RuntimeError(f"NUP indexing failed: {index_result}")
    import_result = bpy.ops.oot_remake.import_level()
    if "FINISHED" not in import_result:
        raise RuntimeError(f"level import failed: {import_result}")

    matches = [
        collection
        for collection in bpy.data.collections
        if collection.get("oot_remake_level") == args.level
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one level collection, found {len(matches)}")
    level = matches[0]
    placements = int(level.get("oot_remake_placement_count", -1))
    visual = int(level.get("oot_remake_visual_count", -1))
    nonvisual = int(level.get("oot_remake_nonvisual_count", -1))
    source_models = int(level.get("oot_remake_source_model_count", -1))
    failed_source_models = int(level.get("oot_remake_failed_source_model_count", -1))
    direct_source_models = int(level.get("oot_remake_direct_source_model_count", -1))
    gltf_source_models = int(level.get("oot_remake_gltf_source_model_count", -1))
    terrain_scenes = int(level.get("oot_remake_terrain_scene_count", -1))
    terrain_tiles = int(level.get("oot_remake_terrain_tile_count", -1))
    terrain_vertices = int(level.get("oot_remake_terrain_vertex_count", -1))
    terrain_faces = int(level.get("oot_remake_terrain_face_count", -1))
    terrain_material_maps = int(
        level.get("oot_remake_terrain_material_map_count", -1)
    )
    require_count(placements, args.expect_placements, "placement count")
    require_count(visual, args.expect_visual, "visual count")
    require_count(nonvisual, args.expect_nonvisual, "nonvisual count")
    require_count(source_models, args.expect_source_models, "source model count")
    require_count(
        failed_source_models,
        args.expect_failed_source_models,
        "failed source model count",
    )
    require_count(
        direct_source_models,
        args.expect_direct_source_models,
        "direct source model count",
    )
    require_count(
        gltf_source_models,
        args.expect_gltf_source_models,
        "glTF source model count",
    )
    require_count(terrain_scenes, args.expect_terrain_scenes, "terrain scene count")
    require_count(terrain_tiles, args.expect_terrain_tiles, "terrain tile count")
    require_count(
        terrain_vertices,
        args.expect_terrain_vertices,
        "terrain vertex count",
    )
    require_count(terrain_faces, args.expect_terrain_faces, "terrain face count")
    require_count(
        terrain_material_maps,
        args.expect_terrain_material_maps,
        "terrain material-map count",
    )
    if bool(level.get("oot_remake_includes_terrain")) != (terrain_tiles > 0):
        raise RuntimeError("terrain inclusion metadata does not match the terrain tile count")

    visual_collection = level.children.get("Visual Placements")
    nonvisual_collection = level.children.get("Nonvisual Placements")
    if visual_collection is None or nonvisual_collection is None:
        raise RuntimeError("level collection is missing placement child collections")
    if len(visual_collection.objects) != visual:
        raise RuntimeError("visual placement object count does not match collection metadata")
    if len(nonvisual_collection.objects) != nonvisual:
        raise RuntimeError("nonvisual placement object count does not match collection metadata")
    if any(obj.instance_collection is None for obj in visual_collection.objects):
        raise RuntimeError("a visual placement is not a collection instance")
    if any(obj.instance_collection is not None for obj in nonvisual_collection.objects):
        raise RuntimeError("a nonvisual marker unexpectedly instances a model")
    sources = {obj.instance_collection for obj in visual_collection.objects}
    imported_source_models = source_models - failed_source_models
    if len(sources) != imported_source_models:
        raise RuntimeError(
            f"visual placements reference {len(sources)} sources, "
            f"expected {imported_source_models}"
        )
    if any(
        bpy.context.scene.collection.children.get(source.name) is not None
        for source in sources
    ):
        raise RuntimeError("a source model collection remains directly linked to the scene")
    terrain_collection = level.children.get("Terrain")
    if (terrain_collection is not None) != (terrain_scenes > 0):
        raise RuntimeError("terrain collection does not match terrain scene metadata")
    if terrain_collection is not None:
        terrain_objects = tuple(
            obj
            for scene_collection in terrain_collection.children
            for obj in scene_collection.objects
        )
        if len(terrain_objects) != terrain_tiles:
            raise RuntimeError("terrain object count does not match terrain metadata")
        if sum(len(obj.data.vertices) for obj in terrain_objects) != terrain_vertices:
            raise RuntimeError("terrain mesh vertices do not match terrain metadata")
        if sum(len(obj.data.polygons) for obj in terrain_objects) != terrain_faces:
            raise RuntimeError("terrain mesh faces do not match terrain metadata")
        if any(obj.data.color_attributes.get("TerrainColor") is None for obj in terrain_objects):
            raise RuntimeError("a terrain mesh has no decoded material colors")
    if args.textures and not any(image.packed_file for image in bpy.data.images):
        raise RuntimeError("textured level import has no packed BNTX images")

    from oot_remake_importer.core.levels import discover_levels, read_placement
    from oot_remake_importer.core.nup import NupArchive
    from oot_remake_importer.core.resources import ResourceDecoder

    archive = NupArchive(args.nup)
    package = archive.default_package()
    entries = archive.package_files(package)
    decoder = ResourceDecoder(archive, package)
    definition = next(
        level_definition
        for level_definition in discover_levels(entries)
        if level_definition.identifier == args.level
    )
    expected_locations = {}
    for path in definition.paths:
        placement = read_placement(
            decoder.decode(path, expected_magic=b"YB"),
            path,
            dynamic="_Dynamic." in path,
        )
        for actor in placement.actors:
            expected_locations[f"{actor.actor_hash & 0xFFFFFFFFFFFFFFFF:016X}"] = (
                actor.translate[0] * settings.scale,
                -actor.translate[2] * settings.scale,
                actor.translate[1] * settings.scale,
            )
    for obj in tuple(visual_collection.objects) + tuple(nonvisual_collection.objects):
        actor_hash = obj.get("oot_remake_actor_hash")
        if actor_hash is None:
            continue
        expected = expected_locations[actor_hash]
        actual = tuple(obj.matrix_world.translation)
        if not all(math.isclose(a, b, rel_tol=0.0, abs_tol=1e-4) for a, b in zip(actual, expected)):
            raise RuntimeError(
                f"placement {actor_hash} is at {actual!r}, expected {expected!r}"
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.output.resolve()))
    print(
        f"OOTR_LEVEL_SMOKE_OK level={args.level!r} placements={placements} "
        f"visual={visual} nonvisual={nonvisual} sources={source_models} "
        f"failed_sources={failed_source_models} "
        f"textures={level.get('oot_remake_texture_count')} "
        f"terrain_scenes={terrain_scenes} terrain_tiles={terrain_tiles} "
        f"terrain_vertices={terrain_vertices} terrain_faces={terrain_faces} "
        f"terrain_material_maps={terrain_material_maps} "
        f"direct_sources={direct_source_models} gltf_sources={gltf_source_models}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
