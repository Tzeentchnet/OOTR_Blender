# Development guide

## Requirements

- Windows x64
- Blender 5.2 or newer
- Python 3.11 or newer
- Git
- CMake and Ninja
- A C++20 compiler available to CMake

## Repository layout

| Path | Purpose |
|---|---|
| `oot_remake_importer/` | Blender extension source |
| `native/` | In-process BFRES/BNTX bridge and BfrAss CLI build |
| `tests/` | Parser, packaging, and optional Blender integration tests |
| `tools/build_addon.py` | Reproducible native build and extension packaging |
| `docs/asset-formats/` | Bounds-checked format and material notes |
| `build/blender-addon/` | Ignored third-party checkouts, native builds, and staging |
| `dist/` | Ignored installable extension archives |

## Build and install

From the repository root:

```powershell
python tools\build_addon.py `
  --blender "D:\Blender Foundation\Blender 5.2\blender.exe" `
  --install
```

The build script:

1. checks out the pinned BfrAss and Assimp revisions under
   `build/blender-addon/native/`;
2. builds `bfrass.exe` and `ootr_native.dll`;
3. stages the extension, dependency licenses, and notices;
4. creates `dist/oot_remake_importer-<version>.zip`; and
5. optionally installs and enables it in Blender's `user_default` extension
   repository.

Third-party checkouts and binaries are never committed.

## Validation

Run the unit and packaging tests:

```powershell
python -m unittest discover -s tests -p "*_test.py"
```

Compile the Python sources:

```powershell
python -m compileall -q oot_remake_importer tests tools
```

Validate the extension manifest and source layout:

```powershell
& "D:\Blender Foundation\Blender 5.2\blender.exe" --background `
  --command extension validate .\oot_remake_importer
```

The optional integration scripts use a user-owned NUP and are not part of the
normal unit suite:

- `tests/addon_smoke.py` checks standalone static and animated imports.
- `tests/level_smoke.py` checks placement counts, source collections, terrain,
  textures, and coordinate conversion.
- `tests/level_profile.py` records real-level import phase timings.

Use the smallest relevant test first, then run the complete unit suite before
packaging.

## Native and glTF boundaries

The `ootr_native` bridge exposes static mesh arrays and decoded BNTX images
directly to Blender. It deliberately requests no rigging and bakes node
transforms, which makes it fast and suitable for static assets and instanced
level sources.

Animated imports still use the pinned BfrAss/Assimp glTF route for armature,
skin, and rest-pose interchange. FSKA curves are parsed and applied directly
by the extension; they are not exported as glTF animation channels.

See the [BFRES reference](asset-formats/bfres.md) and
[material semantics](asset-formats/materials.md) for the verified technical
details.

## Dependencies and provenance

The package includes:

- BfrAss at its pinned MIT-licensed revision;
- Assimp at its pinned BSD-licensed revision; and
- the corresponding license and notice files.

Exact revisions and behavioral research sources are listed in
[citations](citations.md) and
[third-party notices](../THIRD_PARTY_NOTICES.md).

External GPL BFRES tools were used only for behavioral comparison; no GPL
source or translated implementation is included.
