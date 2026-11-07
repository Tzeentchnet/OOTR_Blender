# User guide

## Install the extension

The packaged extension supports Windows x64 and Blender 5.2 or newer.

1. Open **Edit > Preferences > Extensions**.
2. Open the Extensions menu and choose **Install from Disk**.
3. Select `oot_remake_importer-<version>.zip`.
4. Open **3D View > Sidebar > OoT Remake**.

If you are building from source, follow the
[development guide](development.md).

## Index a NUP

Choose the supported, already-decrypted `.nup`, then click **Index**. The
extension validates the table of contents and member hashes before presenting
the available models, animations, and level variants.

Indexing reads the RomFS directory tree without extracting the full image.
Only resources selected for an import are decompressed, and temporary files
are removed afterward.

## Import a static model

1. Click **Choose Model** and select a BFRES resource.
2. Leave the animation selection empty.
3. Choose whether to import referenced textures and additional LODs.
4. Click **Import Model**.

Static imports use the native bridge by default. Every FMDL in the selected
resource is imported unless a level actor explicitly selects one model.
Node transforms are baked into the static geometry and the rest pose is
flattened. If native parsing fails, the extension reports the reason and tries
the glTF fallback.

## Import skeletal animation

1. Choose the model that owns the matching skeleton.
2. Click **Choose Animation Resource** and select an
   `.anim.bfres.zs` resource.
3. Click **Choose Animation Clip** and select one FSKA clip.
4. Confirm the source frame rate, then click **Import Model**.

The animated workflow has two distinct parts:

| Data | Route |
|---|---|
| Mesh, armature, skin weights, and rest pose | BfrAss/Assimp glTF interchange |
| FSKA curves and evaluated poses | Direct BFRES decoding into a Blender Action |

No animation channel is imported from glTF. The extension evaluates the
complete FSKL hierarchy, including standard, scale-less, Maya, and Softimage
scale modes, then keys location, quaternion rotation, and scale directly.
Maya segment-scale compensation is mapped to Blender's matching inherit-scale
behavior.

After creating the Action, every frame is evaluated and compared with the
decoded BFRES pose. An import is rejected if the transfer error exceeds
`2e-4` Blender units. A mismatched clip is also rejected when an animated bone
does not exist in the selected model.

Models without meshes, including locator and animation-reference rigs, import
as bare armatures. One clip is imported at a time so resources containing many
actions do not unnecessarily enlarge the `.blend`.

More detail is available in the
[BFRES animation reference](asset-formats/bfres.md).

## Import a level

1. Click **Choose Level** and select a level variant.
2. Review the level options.
3. Click **Import Level Placements**.

The extension combines static and dynamic BCETT resources, resolves each
actor's `ModelInfo`, imports each unique visual source once, and creates
collection instances at the recorded transforms. Areas, anchors, effects, and
other nonvisual records can be preserved as labeled empties.

### Important level options

| Option | Default | Notes |
|---|---|---|
| Direct native import | On | Uses the fast in-process static path; unsupported resources fall back to glTF |
| Level textures | Off | Large levels can reference thousands of packed images |
| TerrainArc geometry | On | Adds adaptive terrain height meshes |
| Terrain sampling | Balanced (4x) | Samples every fourth height value |
| Nonvisual actors | On | Keeps useful placement markers |

Use **Full (1x)** terrain only when the extra density is needed. It can produce
many millions of quads on a large level.

Terrain material IDs and weights are decoded into representative vertex
colors. They do not yet reproduce the complete terrain shader.

## Imported metadata

Imported collections store useful custom properties, including:

- source NUP and RomFS path;
- direct or glTF import route;
- texture count and missing-texture warnings;
- material-semantics catalog version;
- animation resource, clip, frame rate, loop flag, bone counts, and transfer
  error;
- level placement and source-model counts;
- TerrainArc scene, tile, vertex, face, material-map, and sampling counts.

Materials retain their source sampler bindings, shader parameters, selected
vertex-color interpretation, and reconstructed-layer information.

## Troubleshooting

### A texture is reported missing

Some materials reference textures that are not present in the title. The
extension records these names instead of silently substituting unrelated
content. See [known limitations](limitations.md#missing-source-textures).

### A direct model falls back to glTF

The native and glTF paths share the pinned BFRES parser, so a malformed source
may fail both routes. A level keeps that placement as a labeled marker and
records the parser error.

### A level import uses too much memory

Disable level textures, keep terrain at **Balanced (4x)** or **Coarse (8x)**,
and avoid importing every LOD. Configured actor, model, texture, and terrain
limits are enforced before Blender creates an unbounded scene.

### An animation does not match the model

Select the model or animation-reference skeleton the clip was authored for.
The importer requires every animated bone to exist and intentionally rejects
partial matches.
