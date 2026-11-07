"""Classify BFRES controls and adapt Blender materials using packaged semantics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import bpy

from .core.materials import (
    MaterialDecision,
    MaterialError,
    MaterialMetadata,
    MaterialSemantics,
    classify_vertex_colors,
    describe_sampler_bindings,
    load_material_semantics,
    parameter_color,
)


@dataclass(frozen=True, slots=True)
class VertexColorProfile:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]
    grayscale_error: float
    sample_count: int

    def is_grayscale(self, semantics: MaterialSemantics) -> bool:
        return self.grayscale_error <= semantics.vertex_colors.grayscale_tolerance

    def is_control_mask(self, semantics: MaterialSemantics) -> bool:
        rules = semantics.vertex_colors
        ranges = tuple(high - low for low, high in zip(self.minimum, self.maximum))
        has_boundary_channel = any(
            span <= rules.boundary_span_max
            and (high <= rules.boundary_low_max or low >= rules.boundary_high_min)
            for low, high, span in zip(self.minimum, self.maximum, ranges)
        )
        return has_boundary_channel and any(
            span >= rules.varying_span_min for span in ranges
        )


@dataclass(slots=True)
class MaterialMappingStats:
    semantics_version: str
    masks_bypassed: int = 0
    tints_reconstructed: int = 0
    vertex_colors_preserved: int = 0
    layers_reconstructed: int = 0


def _source_material_name(name: str, metadata: dict[str, MaterialMetadata]) -> str | None:
    if name in metadata:
        return name
    match = re.fullmatch(r"(.+)\.\d{3}", name)
    if match is not None and match.group(1) in metadata:
        return match.group(1)
    return None


def _vertex_color_profile(
    imported: set[bpy.types.Object],
    material: bpy.types.Material,
    layer_name: str,
) -> VertexColorProfile | None:
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    grayscale_error = 0.0
    sample_count = 0
    for obj in imported:
        if obj.type != "MESH" or material.name not in obj.data.materials:
            continue
        attributes = obj.data.color_attributes
        attribute = attributes.get(layer_name) if layer_name else attributes.active_color
        if attribute is None:
            continue
        step = max(1, len(attribute.data) // 8192)
        for index in range(0, len(attribute.data), step):
            color = attribute.data[index].color
            for channel in range(3):
                minimum[channel] = min(minimum[channel], color[channel])
                maximum[channel] = max(maximum[channel], color[channel])
            grayscale_error = max(
                grayscale_error,
                abs(color[0] - color[1]),
                abs(color[0] - color[2]),
                abs(color[1] - color[2]),
            )
            sample_count += 1
    if sample_count == 0:
        return None
    return VertexColorProfile(
        minimum=tuple(minimum),
        maximum=tuple(maximum),
        grayscale_error=grayscale_error,
        sample_count=sample_count,
    )


def _socket_by_identifier(sockets, identifier: str):
    return next((socket for socket in sockets if socket.identifier == identifier), None)


def _vertex_mix_links(node_tree):
    for principled in node_tree.nodes:
        if principled.bl_idname != "ShaderNodeBsdfPrincipled":
            continue
        base_color = principled.inputs.get("Base Color")
        output_link = next(
            (link for link in node_tree.links if link.to_socket == base_color),
            None,
        )
        if output_link is None:
            continue
        mix = output_link.from_node
        if mix.bl_idname != "ShaderNodeMix" or mix.blend_type != "MULTIPLY":
            continue
        incoming = tuple(link for link in node_tree.links if link.to_node == mix)
        vertex_link = next(
            (link for link in incoming if link.from_node.bl_idname == "ShaderNodeVertexColor"),
            None,
        )
        base_link = next(
            (
                link
                for link in incoming
                if link.to_socket.identifier == "A_Color" and link is not vertex_link
            ),
            None,
        )
        output = _socket_by_identifier(mix.outputs, "Result_Color")
        outgoing = tuple(
            link for link in node_tree.links
            if output is not None and link.from_socket == output
        )
        if vertex_link is not None and base_link is not None and outgoing:
            return mix, base_link, vertex_link, outgoing
    return None


def _record_material_metadata(
    material: bpy.types.Material,
    metadata: MaterialMetadata,
    semantics: MaterialSemantics,
) -> None:
    material["oot_remake_source_material"] = metadata.name
    material["oot_remake_shader_archive"] = metadata.shader_archive
    material["oot_remake_shader_model"] = metadata.shader_model
    material["oot_remake_material_semantics"] = semantics.catalog_version
    material["oot_remake_sampler_bindings"] = json.dumps(metadata.samplers, sort_keys=True)
    material["oot_remake_sampler_semantics"] = json.dumps(
        describe_sampler_bindings(metadata, semantics),
        sort_keys=True,
    )
    material["oot_remake_shader_options"] = json.dumps(metadata.options, sort_keys=True)
    for index in range(6):
        name = f"ns_param_color{index}"
        color = parameter_color(metadata, name)
        if color is not None:
            material[f"oot_remake_{name}"] = color


def _bypass_vertex_mask(node_tree, mix, base_link, outgoing, vertex_node) -> None:
    for link in outgoing:
        destination = link.to_socket
        node_tree.links.remove(link)
        node_tree.links.new(base_link.from_socket, destination)
    mix.label = "BFRES vertex control mask (bypassed)"
    vertex_node.label = "BFRES packed material mask"


def _reconstruct_vertex_tint(
    node_tree,
    mix,
    vertex_link,
    decision: MaterialDecision,
) -> None:
    if decision.colors is None:
        raise MaterialError("material tint decision does not contain colors")
    color0, color1 = decision.colors
    vertex_node = vertex_link.from_node
    separate = node_tree.nodes.new("ShaderNodeSeparateColor")
    separate.name = "BFRES Tint Mask"
    separate.label = "BFRES tint mask"
    separate.mode = "RGB"
    separate.location = (mix.location.x - 420, mix.location.y - 180)

    tint = node_tree.nodes.new("ShaderNodeMix")
    tint.name = "BFRES Material Colors"
    tint.label = "BFRES material colors 0 / 1"
    tint.data_type = "RGBA"
    tint.blend_type = "MIX"
    tint.location = (mix.location.x - 180, mix.location.y - 180)
    factor = _socket_by_identifier(tint.inputs, "Factor_Float")
    input_a = _socket_by_identifier(tint.inputs, "A_Color")
    input_b = _socket_by_identifier(tint.inputs, "B_Color")
    output = _socket_by_identifier(tint.outputs, "Result_Color")
    mix_color = _socket_by_identifier(mix.inputs, "B_Color")
    if any(value is None for value in (factor, input_a, input_b, output, mix_color)):
        node_tree.nodes.remove(tint)
        node_tree.nodes.remove(separate)
        raise MaterialError("Blender 5.2 Mix node sockets do not match the expected layout")

    input_a.default_value = color0
    input_b.default_value = color1
    node_tree.links.new(vertex_link.from_socket, separate.inputs["Color"])
    node_tree.links.new(separate.outputs["Red"], factor)
    node_tree.links.remove(vertex_link)
    node_tree.links.new(output, mix_color)
    mix.label = "Multiply BFRES material tint"
    vertex_node.label = "BFRES tint weights"


def map_imported_materials(
    imported: set[bpy.types.Object],
    imported_materials: set[bpy.types.Material],
    metadata: dict[str, MaterialMetadata],
) -> MaterialMappingStats:
    semantics = load_material_semantics()
    stats = MaterialMappingStats(semantics_version=semantics.catalog_version)
    for material in imported_materials:
        source_name = _source_material_name(material.name, metadata)
        if source_name is None:
            continue
        source = metadata[source_name]
        _record_material_metadata(material, source, semantics)
        if material.get("oot_remake_layered_albedo"):
            stats.layers_reconstructed += 1
        if not material.use_nodes or material.node_tree is None:
            continue
        links = _vertex_mix_links(material.node_tree)
        if links is None:
            continue
        mix, base_link, vertex_link, outgoing = links
        vertex_node = vertex_link.from_node
        profile = _vertex_color_profile(imported, material, vertex_node.layer_name)
        if profile is None:
            continue

        decision = classify_vertex_colors(
            source,
            grayscale=profile.is_grayscale(semantics),
            control_profile=profile.is_control_mask(semantics),
            semantics=semantics,
        )
        material["oot_remake_vertex_color_mode"] = decision.mode
        material["oot_remake_vertex_color_reason"] = decision.reason
        if decision.mode == "material_tint_mask":
            _reconstruct_vertex_tint(material.node_tree, mix, vertex_link, decision)
            stats.tints_reconstructed += 1
        elif decision.mode == "control_mask_bypassed":
            _bypass_vertex_mask(
                material.node_tree,
                mix,
                base_link,
                outgoing,
                vertex_node,
            )
            stats.masks_bypassed += 1
        else:
            stats.vertex_colors_preserved += 1
    return stats
