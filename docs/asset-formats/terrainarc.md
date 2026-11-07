# TerrainArc scenes and maps

This note documents the clean, independently verified subset used by the
level importer. No AGPL terrain implementation or dependency is included.
Record offsets were established from the title's resources and exercised by
synthetic parser fixtures plus real Kokiri Forest imports.

## Scene index (`.tscb`)

Verified TSCB files are little-endian, use magic `TSCB`, and report version
`0x10D`.

| Offset | Type | Meaning |
|---:|---|---|
| `0x10` | `float` | Horizontal world scale |
| `0x14` | `float` | Vertical height scale |
| `0x30` | `u32` | Resource-type count |
| `0x34` | `u32` | Material count |
| `0x38` | `u32` | Area count |
| `0x70` | relative pointer | Resource-type pointer array |
| `0x74` | relative pointer | Material pointer array |
| `0x78` | relative pointer | Area pointer array |

Every relative pointer is a signed 32-bit displacement from the address of
the pointer field itself. The parser bounds-checks the field, target, table,
record count, string terminator, UTF-8 text and duplicate identifiers before
using a record.

A resource-type record stores its type ID at `+0x00` and a relative pointer to
its filename extension at `+0x64`. Observed IDs are:

| ID | Extension | Role |
|---:|---|---|
| `0x0000` | `hght` | Height surface |
| `0x0100` | `norm` | Normal surface |
| `0x2000` | `mate` | Material IDs and blend weights |
| `0x5100` | `bake_ao.extm` | Baked ambient occlusion |
| `0x5200` | `bake_gi.extm` | Baked global illumination |
| `0x5300` | `custom.extm` | Custom terrain data |

An area record contains normalized X/Z center coordinates at `+0x00` and
`+0x04`, normalized square size at `+0x08`, file count at `+0x20`, filename
pointer at `+0x60`, and one relative file-info pointer per resource beginning
at `+0x68`. A file-info record stores its type at `+0x00` and observed
minimum/maximum values at `+0x20` and `+0x24`.

Material records are 64 bytes. They carry the sequential material ID at
`+0x00`, observed U/V scales at `+0x10`/`+0x14`, and linear RGB base color at
`+0x30` through `+0x38`.

## Resource wrapper (`TARB`)

HGHT and MATE files use a 20-byte version-1 TARB wrapper:

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | four bytes | `TARB` |
| `0x04` | `u32` | Version (`1`) |
| `0x08` | `u16` | Observed flags (`1`) |
| `0x0A` | `u16` | Header size (`20`) |
| `0x0C` | `u32` | Decompressed size |
| `0x10` | `u32` | Compressed size |
| `0x14` | bytes | Zstandard frame |

The decoder requires exact payload sizes, a declared output below its safety
limit and a complete Zstandard frame.

## Height surface (`.hght`)

The decoded HGHT payload identifies format `0x01020401`. Its height stream is
selected by the relative pointer at `0x2C` and byte count at `0x30`.

The stream is a 257 by 257 `R32_FLOAT` Tegra block-linear surface with block
height 16. Its padded allocation is 417,792 bytes. The address mapping follows
the Tegra GOB layout already implemented by the pinned MIT-licensed BfrAss
dependency. Values are absolute normalized heights, so Blender Z is
`sample * TSCB vertical scale`; they are not remapped through each tile's
minimum and maximum.

Zero and out-of-range padded samples are treated as unpopulated. The remaining
samples must substantially agree with the TSCB file-info range or import
stops with a format error.

## Material surface (`.mate`)

The decoded MATE payload identifies format `0x02020401`. Its one 835,584-byte
stream is two consecutive block-linear planes using the same padded
257-by-257 allocation:

1. four 8-bit material IDs per logical 256-by-256 cell;
2. four 8-bit blend weights per logical cell.

Observed unused layers use material ID 127 with zero weight. The importer
normalizes the populated weights and blends the corresponding TSCB material
colors into the `TerrainColor` point attribute. This provides stable material
regions without claiming to reproduce the complete terrain texture shader.

## Adaptive Blender mesh

Areas form a dyadic quadtree. Parent cells covered by any finer HGHT area are
excluded, preventing overlapping LOD surfaces while retaining coarse terrain
where no child exists. The default sampling step is four; steps one, two,
four and eight are available in the level UI.

Game coordinates become Blender coordinates as:

```text
(x, height, z) -> (x, -z, height)
```

The level collection records scene, tile, vertex, face and material-map counts
plus the selected sampling step. Normal and EXTM maps remain preserved in the
NUP but are not yet connected.
