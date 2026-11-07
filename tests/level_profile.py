"""Profile the expensive stages of a real level placement import.

Run with Blender after building the native converter:

  blender --background --factory-startup --python tests/level_profile.py -- \
    --nup <game.nup> --level KokiriForest/KokiriForest_Child \
    --converter <bfrass.exe>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path

import bpy


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nup", type=Path, required=True)
    parser.add_argument("--level", required=True)
    parser.add_argument("--converter", type=Path, required=True)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--textures", action="store_true")
    parser.add_argument("--no-direct", action="store_true")
    parser.add_argument("--no-terrain", action="store_true")
    parser.add_argument("--terrain-step", choices=("1", "2", "4", "8"), default="4")
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1:])


def main() -> None:
    args = arguments()
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))
    os.environ["OOTR_BFRASS"] = str(args.converter.resolve())
    if args.native is not None:
        os.environ["OOTR_NATIVE"] = str(args.native.resolve())

    import oot_remake_importer
    from oot_remake_importer import addon

    durations: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    decoded_bytes: dict[str, int] = defaultdict(int)

    original_convert = addon.convert_bfres
    original_import_glb = addon._import_glb
    original_decode = addon.ResourceDecoder.decode

    def timed_convert(*call_args, **call_kwargs):
        start = time.perf_counter()
        try:
            return original_convert(*call_args, **call_kwargs)
        finally:
            durations["bfrass_seconds"] += time.perf_counter() - start
            counts["bfrass_calls"] += 1

    def timed_import_glb(*call_args, **call_kwargs):
        start = time.perf_counter()
        try:
            return original_import_glb(*call_args, **call_kwargs)
        finally:
            durations["gltf_seconds"] += time.perf_counter() - start
            counts["gltf_calls"] += 1

    def timed_decode(decoder, path, *call_args, **call_kwargs):
        category = Path(path).suffixes[-2:] if "." in Path(path).name else ()
        label = "decode_" + "_".join(suffix.lstrip(".") for suffix in category)
        start = time.perf_counter()
        try:
            result = original_decode(decoder, path, *call_args, **call_kwargs)
        finally:
            durations[f"{label}_seconds"] += time.perf_counter() - start
            counts[f"{label}_calls"] += 1
        decoded_bytes[label] += len(result)
        return result

    addon.convert_bfres = timed_convert
    addon._import_glb = timed_import_glb
    addon.ResourceDecoder.decode = timed_decode

    oot_remake_importer.register()
    settings = bpy.context.scene.oot_remake_importer
    settings.nup_path = str(args.nup.resolve())
    settings.level_id = args.level
    settings.level_import_textures = args.textures
    settings.level_direct_import = not args.no_direct
    settings.level_import_terrain = not args.no_terrain
    settings.level_terrain_sample_step = args.terrain_step

    start = time.perf_counter()
    index_result = bpy.ops.oot_remake.index_nup()
    index_seconds = time.perf_counter() - start
    if "FINISHED" not in index_result:
        raise RuntimeError(f"NUP indexing failed: {index_result}")

    start = time.perf_counter()
    import_result = bpy.ops.oot_remake.import_level()
    import_seconds = time.perf_counter() - start
    if "FINISHED" not in import_result:
        raise RuntimeError(f"level import failed: {import_result}")

    level = next(
        collection
        for collection in bpy.data.collections
        if collection.get("oot_remake_level") == args.level
    )
    report = {
        "level": args.level,
        "textures": args.textures,
        "index_seconds": index_seconds,
        "import_seconds": import_seconds,
        "durations": dict(sorted(durations.items())),
        "counts": dict(sorted(counts.items())),
        "decoded_bytes": dict(sorted(decoded_bytes.items())),
        "placements": int(level["oot_remake_placement_count"]),
        "source_models": int(level["oot_remake_source_model_count"]),
        "imported_source_models": int(level["oot_remake_imported_source_model_count"]),
        "failed_source_models": int(level["oot_remake_failed_source_model_count"]),
        "textures_loaded": int(level["oot_remake_texture_count"]),
        "direct_source_models": int(level["oot_remake_direct_source_model_count"]),
        "gltf_source_models": int(level["oot_remake_gltf_source_model_count"]),
        "terrain_scenes": int(level["oot_remake_terrain_scene_count"]),
        "terrain_tiles": int(level["oot_remake_terrain_tile_count"]),
        "terrain_vertices": int(level["oot_remake_terrain_vertex_count"]),
        "terrain_faces": int(level["oot_remake_terrain_face_count"]),
        "terrain_material_maps": int(
            level["oot_remake_terrain_material_map_count"]
        ),
        "terrain_sample_step": int(level["oot_remake_terrain_sample_step"]),
    }
    print("OOTR_LEVEL_PROFILE " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
