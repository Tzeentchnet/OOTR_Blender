# Blender addon citations and research sources

This document records the external sources used to design and verify the
Blender addon. Dependency licenses and redistributed binaries remain covered
by [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md); this page also lists
behavioral references from which no code was copied.

## Shipped conversion dependencies

### BfrAss

- Repository: [RAMDRAGONS/BfrAss](https://github.com/RAMDRAGONS/BfrAss)
- Pinned commit:
  [`c1ca30879029750a230ef7d3d5abcba7ec5c8afb`](https://github.com/RAMDRAGONS/BfrAss/commit/c1ca30879029750a230ef7d3d5abcba7ec5c8afb)
- License: [MIT](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/LICENSE)
- Relevant sources:
  - [BFRES Switch loader](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/src/bfres/SwitchLoader.cpp)
  - [Skeleton hierarchy evaluation](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/src/bfres/Skeleton.cpp)
  - [Scene and vertex-attribute builder](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/src/importer/SceneBuilder.cpp)
  - [Material builder](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/src/importer/MaterialBuilder.cpp)
  - [BNTX reader](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/src/texture/Bntx.cpp)
  - [Tegra block-linear swizzle](https://github.com/RAMDRAGONS/BfrAss/blob/c1ca30879029750a230ef7d3d5abcba7ec5c8afb/src/texture/TegraSwizzle.cpp)

BfrAss supplies BFRES/BNTX geometry and texture decoding, Nintendo g3d
skeleton scale-mode behavior, Assimp scene construction, generic sampler
mappings and the observation that BFRES `_cN` attributes become Assimp color
sets. The addon does not modify its ignored checkout at runtime.

### Assimp

- Repository: [assimp/assimp](https://github.com/assimp/assimp)
- Pinned commit:
  [`78ba52270265bcf3b6534fcfd0f52bcfe6838640`](https://github.com/assimp/assimp/commit/78ba52270265bcf3b6534fcfd0f52bcfe6838640)
- License: [BSD-3-Clause and bundled notices](https://github.com/assimp/assimp/blob/78ba52270265bcf3b6534fcfd0f52bcfe6838640/LICENSE)
- Relevant sources:
  - [glTF 2 exporter](https://github.com/assimp/assimp/blob/78ba52270265bcf3b6534fcfd0f52bcfe6838640/code/AssetLib/glTF2/glTF2Exporter.cpp)
  - [Material texture types](https://github.com/assimp/assimp/blob/78ba52270265bcf3b6534fcfd0f52bcfe6838640/include/assimp/material.h)

Assimp is statically linked into the generated BfrAss executable. Its glTF
exporter defines which generic material channels survive the conversion and
why arbitrary BFRES properties require a separate metadata path.

## Behavioral format references

### BfresLibrary

- Repository:
  [KillzXGaming/BfresLibrary](https://github.com/KillzXGaming/BfresLibrary)
- Revision used for cross-checking:
  [`e2c5debca60e9b95e0fd220d32886102d65166bf`](https://github.com/KillzXGaming/BfresLibrary/commit/e2c5debca60e9b95e0fd220d32886102d65166bf)
- License:
  [MIT](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/LICENSE.txt)
- Relevant sources:
  - [BFRES 10 material parser](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Switch/Model/MaterialParserV10.cs)
  - [General Switch material parser](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Switch/Model/MaterialParser.cs)
  - [Material model](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Shared/Models/Material/Material.cs)
  - [Shader-parameter model](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Shared/Models/Material/ShaderParam.cs)
  - [Switch texture model](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Switch/Texture/Texture.cs)
  - [Skeletal-animation reader](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Shared/SkeletalAnim/SkeletalAnim.cs)
  - [Bone-animation reader](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Shared/SkeletalAnim/BoneAnim.cs)
  - [Animation-curve reader](https://github.com/KillzXGaming/BfresLibrary/blob/e2c5debca60e9b95e0fd220d32886102d65166bf/BfresLibrary/Shared/Common/AnimCurve.cs)

BfresLibrary was read to cross-check BFRES 10 record boundaries, parameter
types, animation structures and pointer relationships. The addon parsers
were written independently and are limited to the fields they validate. The
observed BFRES 10 animation curve moves its value-transform fields four bytes
later than the older layout in the reference reader. No BfresLibrary source
is shipped.

GPL BFRES tools were used only as behavioral comparison during research. No
GPL source or translation of GPL implementation code is included.

### Awn

- Repository: [Watertoon/Awn](https://github.com/Watertoon/Awn)
- Revision used for behavioral cross-checking:
  [`c3c090bb24bc786e6ff94fe3ae68778054f130a9`](https://github.com/Watertoon/Awn/commit/c3c090bb24bc786e6ff94fe3ae68778054f130a9)
- License:
  [GPL-2.0](https://github.com/Watertoon/Awn/blob/c3c090bb24bc786e6ff94fe3ae68778054f130a9/LICENSE)
- Relevant source:
  [BFRES animation curves](https://github.com/Watertoon/Awn/blob/c3c090bb24bc786e6ff94fe3ae68778054f130a9/libraries/lib_vp/include/vp/res/res_bfresanimcurve.hpp)

Awn was used only to cross-check the observed BFRES 10 extended-curve flag
meanings, separate point/tangent quantization and Hermite behavior. The
addon's implementation was written independently from the title's binary
layout and mathematical invariants; no Awn source is included or translated.

## Standards and host behavior

### glTF 2.0

- Specification:
  [Khronos glTF 2.0](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html)
- Relevant sections:
  [mesh attributes](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#meshes-overview)
  and
  [materials](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#materials),
  plus
  [skins](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#skins)

The specification defines `COLOR_n` as an RGB or RGBA linear multiplier. That
is correct for glTF and is the reason a packed BFRES control attribute cannot
be exported as `COLOR_0` without additional semantics. Its skin and node
rules define the static geometry-and-rig handoff. FSKA animation is instead
evaluated from BFRES and keyed directly into a Blender Action.

### Blender glTF importer

- [Blender 5.0 glTF 2.0 manual](https://docs.blender.org/manual/en/5.0/addons/import_export/scene_gltf2.html)

The manual documents that imported glTF materials are reconstructed as
Blender nodes and that vertex color is a base-color multiplier. The adapter
uses Blender's supported node and custom-property APIs; it does not replace
or monkeypatch the importer.

### Zstandard

- Format specification:
  [Meta Zstandard compression format](https://github.com/facebook/zstd/blob/f8745da6ff1ad1e7bab384bd1f9d742439278e99/doc/zstd_compression_format.md)
- Reference revision:
  [`f8745da6ff1ad1e7bab384bd1f9d742439278e99`](https://github.com/facebook/zstd/commit/f8745da6ff1ad1e7bab384bd1f9d742439278e99)

The addon uses Python's `zstandard` package to inspect frame parameters,
enforce declared content-size limits and select dictionaries by frame
dictionary ID.

## Primary observations

The NUP layout, BFRES 10.4 external hashed-string table, title-specific
sampler use and material-color behavior were determined from the user's
legally obtained dump. Those observations are summarized without embedding
game bytes in:

- [NUP container reference](asset-formats/nup.md)
- [BFRES 10 model reference](asset-formats/bfres.md)
- [BNTX 4 texture reference](asset-formats/bntx.md)
- [BYML level placement reference](asset-formats/byml.md)
- [TerrainArc scene and map reference](asset-formats/terrainarc.md)
- [BFRES material semantics](asset-formats/materials.md)

Real extracted assets and validation renders remain outside the repository.
Synthetic fixtures in the test suite exercise the parser layout without
reproducing copyrighted game content.
