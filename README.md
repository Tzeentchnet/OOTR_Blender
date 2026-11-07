# Ocarina of Time Remake Blender Importer

Import models, skeletal animations, level placements, and TerrainArc geometry
from your own *The Legend of Zelda: Ocarina of Time* Switch 2 `.nup` into
Blender 5.2.

> **You provide the game dump.** This project contains no game data, keys,
> extracted assets, or generated models. It expects the already-decrypted NUP
> for title `0400486024DEC000`.

## What it does

- Indexes the NUP without extracting the full game image.
- Imports static BFRES models and BNTX textures directly into Blender.
- Turns BFRES FSKA clips into normal Blender Actions.
- Builds complete level previews from BCETT actor placements and collection
  instances.
- Adds adaptive TerrainArc height meshes and decoded terrain material colors.
- Reconstructs common BFRES materials, including layered bark and moss.
- Keeps nonvisual actors and failed resources as labeled markers instead of
  silently dropping them.

## Import paths at a glance

| What you import | Default path |
|---|---|
| Static model | Direct native BFRES/BNTX import |
| Level model | Direct native import, once per unique source |
| Animated model | glTF for mesh, armature, skin, and rest pose |
| Animation clip | Direct BFRES FSKA evaluation into a Blender Action |

Animation curves do **not** pass through glTF. The animated-model workflow uses
glTF only to establish Blender's skinned mesh and armature before applying the
directly decoded Action. Static resources fall back to glTF only when the
native bridge cannot read them.

See the [user guide](docs/user-guide.md) for the complete workflow and import
options.

## Requirements

### To use the extension

- Windows x64
- Blender 5.2 or newer
- A user-owned, already-decrypted `.nup` for the supported title

### To build it

- Python 3.11 or newer
- Git, CMake, and Ninja
- A C++20 compiler available to CMake

## Install

If you already have a packaged extension ZIP:

1. Open **Edit > Preferences > Extensions** in Blender.
2. Open the Extensions menu and choose **Install from Disk**.
3. Select `oot_remake_importer-<version>.zip`.

To build and install from this repository:

```powershell
python tools\build_addon.py `
  --blender "D:\Blender Foundation\Blender 5.2\blender.exe" `
  --install
```

The build is reproducible and pins its BfrAss and Assimp revisions. Build
requirements, validation commands, package layout, and dependency details are
in the [development guide](docs/development.md).

## Quick start

1. Open **3D View > Sidebar > OoT Remake**.
2. Select your `.nup`, then click **Index**.
3. Choose one workflow:
   - **Model:** choose a BFRES resource and click **Import Model**.
   - **Animation:** choose a model, animation resource, and clip, then import.
   - **Level:** choose a level variant and click **Import Level Placements**.
4. Enable level textures when you need them. They default off because a large
   level can reference thousands of images.
5. Keep TerrainArc at **Balanced (4x)** for normal previews; use **Full (1x)**
   only when you need the much larger full-resolution mesh.

Imported collections record their source paths, import route, texture counts,
warnings, and relevant animation or terrain statistics as custom properties.

## Documentation

- [Documentation index](docs/README.md)
- [User guide](docs/user-guide.md)
- [Known limitations](docs/limitations.md)
- [Development and build guide](docs/development.md)
- [Asset format references](docs/asset-formats/README.md)
- [Research sources and citations](docs/citations.md)

## Project status

The extension currently targets the supported title's BFRES 10.4 and BNTX 4
resources. Static models, skeletal animation, level placement, textures, and
TerrainArc height geometry are working, but Blender materials remain an
approximation of the complete game shader.

See [known limitations](docs/limitations.md) for unsupported animation types,
packed material channels, terrain shading gaps, missing source textures, and
other current boundaries.

## License and dependencies

The extension is MIT licensed. Its native build pins the MIT-licensed BfrAss
converter and BSD-licensed Assimp. License files and notices are included in
the packaged extension. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
