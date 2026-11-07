"""Create static Blender assets directly from the bundled native bridge."""

from __future__ import annotations

from array import array
from pathlib import Path

import bpy

from .core.materials import (
    MaterialMetadata,
    MaterialSemantics,
    load_material_semantics,
    texture_coordinate_transform,
)
from .core.native_bridge import DecodedTexture, NativeScene
from .material_mapping import MaterialMappingStats, map_imported_materials


def _socket(sockets, identifier: str):
    return next(
        (socket for socket in sockets if socket.identifier == identifier),
        None,
    )


def _image_node(
    node_tree,
    metadata: MaterialMetadata | None,
    sampler: str,
    image: bpy.types.Image,
    name: str,
    location: tuple[float, float],
    mappings: dict[tuple, object],
    *,
    non_color: bool = False,
):
    if non_color:
        image.colorspace_settings.name = "Non-Color"
    node = node_tree.nodes.new("ShaderNodeTexImage")
    node.name = name
    node.label = name
    node.image = image
    node.location = location
    if metadata is not None:
        transform = texture_coordinate_transform(metadata, sampler)
        if transform is not None:
            mapping_key = (
                transform.coordinate_index,
                transform.srt_index,
                transform.mode,
                transform.scale,
                transform.rotation,
                transform.translation,
            )
            mapping = mappings.get(mapping_key)
            if mapping is None:
                uv = node_tree.nodes.new("ShaderNodeUVMap")
                uv.name = (
                    f"BFRES UV {transform.coordinate_index} "
                    f"SRT {transform.srt_index}"
                )
                uv.label = uv.name
                uv.uv_map = "UVMap"
                uv.location = (location[0] - 440.0, location[1] - 40.0)

                mapping = node_tree.nodes.new("ShaderNodeMapping")
                mapping.name = (
                    f"BFRES Texcoord {transform.coordinate_index} "
                    f"SRT {transform.srt_index}"
                )
                mapping.label = mapping.name
                mapping.vector_type = "POINT"
                mapping.location = (location[0] - 220.0, location[1] - 40.0)
                mapping.inputs["Scale"].default_value = (
                    transform.scale[0],
                    transform.scale[1],
                    1.0,
                )
                mapping.inputs["Rotation"].default_value = (
                    0.0,
                    0.0,
                    transform.rotation,
                )
                mapping.inputs["Location"].default_value = (
                    transform.translation[0],
                    transform.translation[1],
                    0.0,
                )
                node_tree.links.new(uv.outputs["UV"], mapping.inputs["Vector"])
                mappings[mapping_key] = mapping
            node_tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    return node


def _first_texture(
    metadata: MaterialMetadata | None,
    images: dict[str, bpy.types.Image],
    samplers: tuple[str, ...],
) -> tuple[str, bpy.types.Image] | None:
    if metadata is None:
        return None
    for sampler in samplers:
        texture_name = metadata.samplers.get(sampler)
        if texture_name in images:
            return sampler, images[texture_name]
    return None


def _blender_color_attribute(source_name: str) -> str:
    index = int(source_name[2:])
    return "Color" if index == 0 else f"Color.{index:03d}"


def _native_material(
    name: str,
    two_sided: bool,
    metadata: MaterialMetadata | None,
    images: dict[str, bpy.types.Image],
    semantics: MaterialSemantics,
) -> bpy.types.Material:
    material = bpy.data.materials.new(name=name or "Material")
    material.use_nodes = True
    material.use_backface_culling = not two_sided
    material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
    alpha_enabled = (
        metadata is not None
        and metadata.options.get("ns_enable_alpha") == "True"
    )
    if alpha_enabled:
        material.surface_render_method = "DITHERED"
        material["oot_remake_alpha_mode"] = "DITHERED"
    node_tree = material.node_tree
    node_tree.nodes.clear()
    mappings: dict[tuple, object] = {}

    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (520.0, 0.0)
    principled = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (240.0, 0.0)
    node_tree.links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    base_texture = _first_texture(metadata, images, ("_a0", "_albedo0"))
    if base_texture is None:
        base = node_tree.nodes.new("ShaderNodeRGB")
        base.name = "BFRES Base Color"
        base.label = "BFRES base color"
        base.location = (-580.0, 120.0)
        base.outputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        base_output = base.outputs["Color"]
    else:
        base_sampler, base_image = base_texture
        base = _image_node(
            node_tree,
            metadata,
            base_sampler,
            base_image,
            "BFRES Base Color",
            (-800.0, 180.0),
            mappings,
        )
        base_output = base.outputs["Color"]
        if alpha_enabled:
            node_tree.links.new(base.outputs["Alpha"], principled.inputs["Alpha"])

    vertex = node_tree.nodes.new("ShaderNodeVertexColor")
    vertex.name = "BFRES Vertex Color"
    vertex.label = "BFRES vertex colors"
    vertex.layer_name = "Color"
    vertex.location = (-800.0, -80.0)

    secondary_texture = _first_texture(metadata, images, ("_a1",))
    secondary_semantic = semantics.samplers.get("_a1")
    if (
        base_texture is not None
        and secondary_texture is not None
        and secondary_semantic is not None
        and secondary_semantic.layer_blend is not None
    ):
        secondary_sampler, secondary_image = secondary_texture
        secondary = _image_node(
            node_tree,
            metadata,
            secondary_sampler,
            secondary_image,
            "BFRES Secondary Base Color",
            (-800.0, 420.0),
            mappings,
        )
        blend = secondary_semantic.layer_blend
        layer_vertex = vertex
        layer_attribute = _blender_color_attribute(blend.attribute)
        if layer_attribute != vertex.layer_name:
            layer_vertex = node_tree.nodes.new("ShaderNodeVertexColor")
            layer_vertex.name = "BFRES Layer Vertex Color"
            layer_vertex.label = "BFRES layer controls"
            layer_vertex.layer_name = layer_attribute
            layer_vertex.location = (-800.0, -180.0)

        separate = node_tree.nodes.new("ShaderNodeSeparateColor")
        separate.name = "BFRES Layer Mask"
        separate.label = f"BFRES {blend.attribute}.{blend.channel} layer mask"
        separate.mode = "RGB"
        separate.location = (-540.0, -80.0)
        node_tree.links.new(layer_vertex.outputs["Color"], separate.inputs["Color"])

        layer_mix = node_tree.nodes.new("ShaderNodeMix")
        layer_mix.name = "BFRES Layered Albedo"
        layer_mix.label = "Blend BFRES base and secondary albedo"
        layer_mix.data_type = "RGBA"
        layer_mix.blend_type = "MIX"
        layer_mix.location = (-320.0, 260.0)
        layer_factor = _socket(layer_mix.inputs, "Factor_Float")
        layer_a = _socket(layer_mix.inputs, "A_Color")
        layer_b = _socket(layer_mix.inputs, "B_Color")
        layer_result = _socket(layer_mix.outputs, "Result_Color")
        if any(
            value is None
            for value in (layer_factor, layer_a, layer_b, layer_result)
        ):
            raise RuntimeError(
                "Blender 5.2 Mix node sockets do not match the expected layout"
            )
        channel_output = {
            "red": "Red",
            "green": "Green",
            "blue": "Blue",
            "alpha": "Alpha",
        }[blend.channel]
        node_tree.links.new(separate.outputs[channel_output], layer_factor)
        node_tree.links.new(base_output, layer_a)
        node_tree.links.new(secondary.outputs["Color"], layer_b)
        base_output = layer_result
        material["oot_remake_layered_albedo"] = True
        material["oot_remake_layer_mask"] = (
            f"{blend.attribute}.{blend.channel}"
        )
        material["oot_remake_layer_mask_confidence"] = blend.confidence
        material["oot_remake_layer_base_texture"] = base_image.name
        material["oot_remake_layer_secondary_texture"] = secondary_image.name

    mix = node_tree.nodes.new("ShaderNodeMix")
    mix.name = "BFRES Base and Vertex Color"
    mix.label = "Multiply BFRES vertex color"
    mix.data_type = "RGBA"
    mix.blend_type = "MULTIPLY"
    mix.location = (-220.0, 100.0)
    factor = _socket(mix.inputs, "Factor_Float")
    input_a = _socket(mix.inputs, "A_Color")
    input_b = _socket(mix.inputs, "B_Color")
    result = _socket(mix.outputs, "Result_Color")
    if any(value is None for value in (factor, input_a, input_b, result)):
        raise RuntimeError("Blender 5.2 Mix node sockets do not match the expected layout")
    factor.default_value = 1.0
    node_tree.links.new(base_output, input_a)
    node_tree.links.new(vertex.outputs["Color"], input_b)
    node_tree.links.new(result, principled.inputs["Base Color"])

    normal_texture_info = _first_texture(metadata, images, ("_n0", "_normal0"))
    if normal_texture_info is not None:
        normal_sampler, normal_image = normal_texture_info
        normal_texture = _image_node(
            node_tree,
            metadata,
            normal_sampler,
            normal_image,
            "BFRES Normal",
            (-560.0, -360.0),
            mappings,
            non_color=True,
        )
        normal = node_tree.nodes.new("ShaderNodeNormalMap")
        normal.location = (-180.0, -280.0)
        node_tree.links.new(normal_texture.outputs["Color"], normal.inputs["Color"])
        node_tree.links.new(normal.outputs["Normal"], principled.inputs["Normal"])

    roughness_info = _first_texture(metadata, images, ("_rgh0", "_rn0", "_r0"))
    if roughness_info is not None:
        roughness_sampler, roughness_image = roughness_info
        roughness = _image_node(
            node_tree,
            metadata,
            roughness_sampler,
            roughness_image,
            "BFRES Roughness",
            (-180.0, -500.0),
            mappings,
            non_color=True,
        )
        node_tree.links.new(roughness.outputs["Color"], principled.inputs["Roughness"])

    metallic_info = _first_texture(metadata, images, ("_mt0",))
    if metallic_info is not None:
        metallic_sampler, metallic_image = metallic_info
        metallic = _image_node(
            node_tree,
            metadata,
            metallic_sampler,
            metallic_image,
            "BFRES Metallic",
            (-180.0, -640.0),
            mappings,
            non_color=True,
        )
        node_tree.links.new(metallic.outputs["Color"], principled.inputs["Metallic"])

    emission_info = _first_texture(metadata, images, ("_e0", "_em0"))
    emission_input = principled.inputs.get("Emission Color") or principled.inputs.get(
        "Emission"
    )
    if emission_info is not None and emission_input is not None:
        emission_sampler, emission_image = emission_info
        emission = _image_node(
            node_tree,
            metadata,
            emission_sampler,
            emission_image,
            "BFRES Emission",
            (-180.0, -780.0),
            mappings,
        )
        node_tree.links.new(emission.outputs["Color"], emission_input)
    return material


def _set_custom_normals(mesh: bpy.types.Mesh, values, vertex_count: int) -> None:
    if values is None:
        return
    try:
        import numpy

        normals = numpy.ctypeslib.as_array(values).reshape((vertex_count, 3))
        mesh.normals_split_custom_set_from_vertices(normals)
    except ImportError:
        mesh.normals_split_custom_set_from_vertices(
            tuple(
                (values[index], values[index + 1], values[index + 2])
                for index in range(0, vertex_count * 3, 3)
            )
        )


def _mesh_object(
    native_mesh,
    materials: tuple[bpy.types.Material, ...],
) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(native_mesh.name)
    positions = native_mesh.floats(
        native_mesh.positions,
        native_mesh.vertex_count * 3,
    )
    indices = native_mesh.uints(native_mesh.indices, native_mesh.index_count)
    face_sizes = native_mesh.uints(
        native_mesh.face_sizes,
        native_mesh.face_count,
    )
    if positions is None or indices is None or face_sizes is None:
        raise RuntimeError(f"native mesh {native_mesh.name!r} has incomplete arrays")

    mesh.vertices.add(native_mesh.vertex_count)
    mesh.vertices.foreach_set("co", positions)
    mesh.loops.add(native_mesh.index_count)
    mesh.loops.foreach_set("vertex_index", indices)
    mesh.polygons.add(native_mesh.face_count)
    mesh.polygons.foreach_set("loop_total", face_sizes)

    loop_starts = array("i")
    offset = 0
    for size in face_sizes:
        loop_starts.append(offset)
        offset += size
    if offset != native_mesh.index_count:
        raise RuntimeError(
            f"native mesh {native_mesh.name!r} face indices are inconsistent"
        )
    mesh.polygons.foreach_set("loop_start", loop_starts)
    mesh.polygons.foreach_set("use_smooth", array("b", [1]) * native_mesh.face_count)

    for index, pointer in enumerate(native_mesh.uvs):
        values = native_mesh.floats(pointer, native_mesh.index_count * 2)
        layer = mesh.uv_layers.new(name="UVMap" if index == 0 else f"UVMap.{index:03d}")
        layer.data.foreach_set("uv", values)

    for index, pointer in enumerate(native_mesh.colors):
        values = native_mesh.floats(pointer, native_mesh.vertex_count * 4)
        attribute = mesh.color_attributes.new(
            name="Color" if index == 0 else f"Color.{index:03d}",
            type="FLOAT_COLOR",
            domain="POINT",
        )
        attribute.data.foreach_set("color", values)

    mesh.update(calc_edges=True)
    normals = native_mesh.floats(
        native_mesh.normals,
        native_mesh.vertex_count * 3,
    )
    _set_custom_normals(mesh, normals, native_mesh.vertex_count)
    if 0 <= native_mesh.material_index < len(materials):
        mesh.materials.append(materials[native_mesh.material_index])
    return bpy.data.objects.new(native_mesh.name, mesh)


def import_bfres_direct(
    model_data: bytes,
    model_name: str | None,
    collection_name: str,
    material_metadata: dict[str, MaterialMetadata],
    texture_images: dict[str, bpy.types.Image],
    *,
    import_lods: bool,
    library_path: Path,
) -> tuple[bpy.types.Collection, set[bpy.types.Object], MaterialMappingStats]:
    collection = bpy.data.collections.new(collection_name)
    imported: set[bpy.types.Object] = set()
    materials: list[bpy.types.Material] = []
    semantics = load_material_semantics()
    try:
        with NativeScene(
            model_data,
            model_name,
            import_lods=import_lods,
            library_path=library_path,
        ) as scene:
            for native in scene.materials:
                materials.append(
                    _native_material(
                        native.name,
                        native.two_sided,
                        material_metadata.get(native.name),
                        texture_images,
                        semantics,
                    )
                )
            material_tuple = tuple(materials)
            for index in range(scene.mesh_count):
                obj = _mesh_object(scene.mesh(index), material_tuple)
                collection.objects.link(obj)
                imported.add(obj)

        stats = map_imported_materials(
            imported,
            set(materials),
            material_metadata,
        )
        return collection, imported, stats
    except Exception:
        for obj in tuple(collection.objects):
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if isinstance(data, bpy.types.Mesh) and data.users == 0:
                bpy.data.meshes.remove(data)
        bpy.data.collections.remove(collection)
        for material in materials:
            if material.users == 0:
                bpy.data.materials.remove(material)
        raise


def load_texture_image(
    texture: DecodedTexture,
    directory: Path,
    *,
    source_path: str,
) -> bpy.types.Image:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{texture.name}.png"
    destination.write_bytes(texture.png)
    image = bpy.data.images.load(str(destination), check_existing=False)
    image.name = texture.name
    image.pack()
    image.filepath_raw = f"//{texture.name}.png"
    image["oot_remake_source_texture"] = source_path
    image["oot_remake_width"] = texture.width
    image["oot_remake_height"] = texture.height
    return image
