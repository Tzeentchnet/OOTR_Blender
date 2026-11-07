"""Create Blender terrain meshes from decoded TerrainArc resources."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable

import bpy

from .core.terrain import (
    HEIGHT_MAP_SIZE,
    HEIGHT_RESOURCE_TYPE,
    MATERIAL_MAP_SIZE,
    MATERIAL_RESOURCE_TYPE,
    TerrainArea,
    TerrainMaterialMap,
    TerrainScene,
    height_is_populated,
    read_height_map,
    read_material_map,
    validate_height_range,
)


TERRAIN_COLOR_ATTRIBUTE = "TerrainColor"
DEFAULT_TERRAIN_COLOR = (0.18, 0.18, 0.18)


@dataclass(frozen=True, slots=True)
class TerrainImportStats:
    tile_count: int
    vertex_count: int
    face_count: int
    material_map_count: int


def _terrain_material(scene: TerrainScene) -> bpy.types.Material:
    stem = PurePosixPath(scene.source_path).name[:-len(".tscb")]
    material = bpy.data.materials.new(name=f"Terrain_{stem}")
    material.use_nodes = True
    material.diffuse_color = (*DEFAULT_TERRAIN_COLOR, 1.0)
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (320.0, 0.0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (40.0, 0.0)
    principled.inputs["Roughness"].default_value = 0.9
    colors = nodes.new("ShaderNodeVertexColor")
    colors.layer_name = TERRAIN_COLOR_ATTRIBUTE
    colors.location = (-240.0, 0.0)
    links.new(colors.outputs["Color"], principled.inputs["Base Color"])
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])
    material["oot_remake_terrain_scene"] = scene.source_path
    material["oot_remake_material_source"] = "TSCB colors and MATE blend weights"
    return material


def _contained(child: TerrainArea, parent: TerrainArea) -> bool:
    tolerance = 1e-7
    return (
        child.size < parent.size
        and child.minimum_x >= parent.minimum_x - tolerance
        and child.maximum_x <= parent.maximum_x + tolerance
        and child.minimum_z >= parent.minimum_z - tolerance
        and child.maximum_z <= parent.maximum_z + tolerance
    )


def _coverage_mask(
    area: TerrainArea,
    height_areas: tuple[TerrainArea, ...],
    sample_step: int,
) -> bytearray:
    cells = (HEIGHT_MAP_SIZE - 1) // sample_step
    covered = bytearray(cells * cells)
    for child in height_areas:
        if not _contained(child, area):
            continue
        x_start = round((child.minimum_x - area.minimum_x) / area.size * cells)
        x_end = round((child.maximum_x - area.minimum_x) / area.size * cells)
        z_start = round((child.minimum_z - area.minimum_z) / area.size * cells)
        z_end = round((child.maximum_z - area.minimum_z) / area.size * cells)
        x_start = max(0, min(cells, x_start))
        x_end = max(0, min(cells, x_end))
        z_start = max(0, min(cells, z_start))
        z_end = max(0, min(cells, z_end))
        if x_end <= x_start or z_end <= z_start:
            continue
        fill = bytes((1,)) * (x_end - x_start)
        for z in range(z_start, z_end):
            offset = z * cells
            covered[offset + x_start:offset + x_end] = fill
    return covered


def _blended_color(
    material_map: TerrainMaterialMap | None,
    palette: dict[int, tuple[float, float, float]],
    x: int,
    z: int,
) -> tuple[float, float, float, float]:
    if material_map is None:
        return (*DEFAULT_TERRAIN_COLOR, 1.0)
    x = min(x, MATERIAL_MAP_SIZE - 1)
    z = min(z, MATERIAL_MAP_SIZE - 1)
    offset = (z * MATERIAL_MAP_SIZE + x) * 4
    weighted = [0.0, 0.0, 0.0]
    total = 0
    for layer in range(4):
        material_id = material_map.material_ids[offset + layer]
        weight = material_map.weights[offset + layer]
        color = palette.get(material_id)
        if color is None or weight == 0:
            continue
        total += weight
        for channel in range(3):
            weighted[channel] += color[channel] * weight
    if total == 0:
        return (*DEFAULT_TERRAIN_COLOR, 1.0)
    return (
        weighted[0] / total,
        weighted[1] / total,
        weighted[2] / total,
        1.0,
    )


def _create_tile_object(
    scene: TerrainScene,
    area: TerrainArea,
    height_areas: tuple[TerrainArea, ...],
    height_data: bytes,
    material_data: bytes | None,
    material: bpy.types.Material,
    collection: bpy.types.Collection,
    sample_step: int,
    scale: float,
) -> tuple[bpy.types.Object | None, int, int, bool]:
    height_resource = area.file(HEIGHT_RESOURCE_TYPE)
    if height_resource is None:
        return None, 0, 0, False
    height_map = read_height_map(height_data, height_resource.path)
    validate_height_range(height_map, height_resource)

    material_resource = area.file(MATERIAL_RESOURCE_TYPE)
    material_map = None
    if material_data is not None and material_resource is not None:
        material_map = read_material_map(material_data, material_resource.path)

    covered = _coverage_mask(area, height_areas, sample_step)
    if covered and all(covered):
        return None, 0, 0, material_map is not None

    grid_size = HEIGHT_MAP_SIZE
    cells = (grid_size - 1) // sample_step
    vertex_map = [-1] * (grid_size * grid_size)
    positions = array("f")
    colors = array("f")
    indices = array("i")
    palette = {
        terrain_material.material_id: terrain_material.color
        for terrain_material in scene.materials
    }

    def add_vertex(source_index: int, x: int, z: int) -> int:
        mapped = vertex_map[source_index]
        if mapped >= 0:
            return mapped
        height = height_map.values[source_index]
        game_x = (
            area.minimum_x + x / (grid_size - 1) * area.size
        ) * scene.world_scale
        game_z = (
            area.minimum_z + z / (grid_size - 1) * area.size
        ) * scene.world_scale
        mapped = len(positions) // 3
        vertex_map[source_index] = mapped
        positions.extend((game_x * scale, -game_z * scale, height * scene.height_scale * scale))
        colors.extend(_blended_color(material_map, palette, x, z))
        return mapped

    face_count = 0
    for cell_z in range(cells):
        z = cell_z * sample_step
        for cell_x in range(cells):
            if covered[cell_z * cells + cell_x]:
                continue
            x = cell_x * sample_step
            top_left = z * grid_size + x
            top_right = top_left + sample_step
            bottom_left = (z + sample_step) * grid_size + x
            bottom_right = bottom_left + sample_step
            if any(
                not height_is_populated(height_map.values[index], height_resource)
                for index in (top_left, top_right, bottom_left, bottom_right)
            ):
                continue
            indices.extend(
                (
                    add_vertex(top_left, x, z),
                    add_vertex(bottom_left, x, z + sample_step),
                    add_vertex(bottom_right, x + sample_step, z + sample_step),
                    add_vertex(top_right, x + sample_step, z),
                )
            )
            face_count += 1

    if face_count == 0:
        return None, 0, 0, material_map is not None

    mesh = bpy.data.meshes.new(f"Terrain_{area.name}")
    vertex_count = len(positions) // 3
    mesh.vertices.add(vertex_count)
    mesh.vertices.foreach_set("co", positions)
    mesh.loops.add(len(indices))
    mesh.loops.foreach_set("vertex_index", indices)
    mesh.polygons.add(face_count)
    mesh.polygons.foreach_set("loop_start", array("i", range(0, len(indices), 4)))
    mesh.polygons.foreach_set("loop_total", array("i", (4,)) * face_count)
    mesh.polygons.foreach_set("use_smooth", array("b", (1,)) * face_count)
    color_attribute = mesh.color_attributes.new(
        name=TERRAIN_COLOR_ATTRIBUTE,
        type="FLOAT_COLOR",
        domain="POINT",
    )
    color_attribute.data.foreach_set("color", colors)
    mesh.materials.append(material)
    mesh.update()

    obj = bpy.data.objects.new(f"Terrain_{area.name}", mesh)
    obj["oot_remake_terrain_scene"] = scene.source_path
    obj["oot_remake_height_resource"] = height_resource.path
    obj["oot_remake_material_resource"] = (
        material_resource.path if material_resource is not None else ""
    )
    obj["oot_remake_terrain_center"] = (area.center_x, area.center_z)
    obj["oot_remake_terrain_size"] = area.size
    obj["oot_remake_height_range"] = (
        height_resource.minimum * scene.height_scale,
        height_resource.maximum * scene.height_scale,
    )
    obj["oot_remake_sample_step"] = sample_step
    collection.objects.link(obj)
    return obj, vertex_count, face_count, material_map is not None


def import_terrain_scene(
    scene: TerrainScene,
    parent: bpy.types.Collection,
    load_resource: Callable[[str], bytes],
    *,
    sample_step: int,
    scale: float,
) -> tuple[bpy.types.Collection, TerrainImportStats]:
    """Import one adaptive TSCB scene under a parent level collection."""
    if sample_step not in (1, 2, 4, 8):
        raise ValueError(f"unsupported terrain sample step {sample_step}")
    stem = PurePosixPath(scene.source_path).name[:-len(".tscb")]
    collection = bpy.data.collections.new(f"Terrain_{stem}")
    parent.children.link(collection)
    material = _terrain_material(scene)
    height_areas = scene.height_areas

    tile_count = 0
    vertex_count = 0
    face_count = 0
    material_map_count = 0
    for area in height_areas:
        covered = _coverage_mask(area, height_areas, sample_step)
        if covered and all(covered):
            continue
        height_resource = area.file(HEIGHT_RESOURCE_TYPE)
        material_resource = area.file(MATERIAL_RESOURCE_TYPE)
        height_data = load_resource(height_resource.path)
        material_data = (
            load_resource(material_resource.path)
            if material_resource is not None
            else None
        )
        obj, vertices, faces, has_material_map = _create_tile_object(
            scene,
            area,
            height_areas,
            height_data,
            material_data,
            material,
            collection,
            sample_step,
            scale,
        )
        if obj is None:
            continue
        tile_count += 1
        vertex_count += vertices
        face_count += faces
        material_map_count += has_material_map

    collection["oot_remake_terrain_scene"] = scene.source_path
    collection["oot_remake_terrain_tile_count"] = tile_count
    collection["oot_remake_terrain_vertex_count"] = vertex_count
    collection["oot_remake_terrain_face_count"] = face_count
    collection["oot_remake_terrain_material_map_count"] = material_map_count
    collection["oot_remake_terrain_sample_step"] = sample_step
    return collection, TerrainImportStats(
        tile_count=tile_count,
        vertex_count=vertex_count,
        face_count=face_count,
        material_map_count=material_map_count,
    )
