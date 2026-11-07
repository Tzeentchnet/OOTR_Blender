# Known limitations

The importer aims for useful, inspectable Blender scenes rather than claiming
pixel-perfect reproduction of the complete game renderer.

## Import scope

The current implementation targets this title's observed BFRES 10.4 and
BNTX 4 resources. These observations are not a general contract for every
Nintendo title or format revision.

## Skeletal animation

Skeletal FSKA clips are supported and keyed directly into Blender Actions.
The title contains 1,427 animation resources; 1,284 contain skeletal clips.

The remaining 143 resources contain only unsupported material animations,
bone-visibility animations, or both. Material animation is the most visible
gap because it drives effects such as blinking eyes and texture-pattern
changes.

The title ships no shape or scene animation curves. Its `CameraAnimation`
resources contain static camera records rather than FSCN animation data and
are not imported.

Animated models still use glTF for mesh, armature, skin, and rest-pose
interchange. The animation curves themselves do not pass through glTF.

## Materials

Blender materials approximate Nintendo's Model shader.

The direct static path currently reconstructs:

- base color and common normal, roughness, metallic, and emission samplers;
- secondary `_a1` albedo blended with the observed `_c0.r` layer weight;
- material-color tints where the source control is verified; and
- dithered alpha for materials that declare alpha rendering.

The following remain metadata-only or incomplete:

- packed `_rah0` and `_rah1` roughness/AO/height maps;
- secondary and generated normal layers;
- skin and fur lookup tables;
- eye-specific shading;
- the complete wetness and overwrite-surface graphs; and
- secondary albedo reconstruction on the animated glTF material path.

See [material semantics](asset-formats/materials.md) for sampler-level details.

## TerrainArc

TerrainArc imports adaptive HGHT geometry and blends decoded MATE material IDs
and weights into representative vertex colors.

It does not yet reproduce:

- `.norm` terrain maps;
- `.extm` maps;
- complete terrain texture layering;
- the original terrain normal and roughness shader; or
- seamless game-renderer LOD transitions.

Balanced sampling intentionally trades density for a much smaller Blender
scene. Full sampling can produce millions of quads.

## Missing source textures

Some source materials name textures that the title does not ship. Common
examples include `CmnTex_BakeLightDefault` in bake-light slots and
`DummyBlack_Gn2`.

Missing textures are reported in the imported collection's
`oot_remake_missing_textures` property. When a missing image is part of a
visible material, the importer preserves its name rather than silently using
an unrelated texture.

## Malformed or unsupported BFRES resources

The direct and glTF paths share the pinned BfrAss parser. A malformed BFRES
resource can therefore fail both paths. Level imports preserve the associated
actor as a marker and record the error instead of dropping the placement.

## Platform

The packaged extension currently targets Windows x64 and Blender 5.2 or
newer. The Python code is largely portable, but native binaries for other
platforms are not currently shipped.
