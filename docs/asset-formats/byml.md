# BYML level placement reference

The level placement preview reads the title's
`Banc/<world>/<variant>_{Static,Dynamic}.bcett.byml.zs` resources. This page
documents the verified subset used by `oot_remake_importer/core/byml.py` and
`oot_remake_importer/core/levels.py`. It is based on direct observation of the
user's dump, not a general Nintendo format specification.

## BYML container

The observed documents use little-endian BYML version 7:

| Offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 2 | Magic `YB` |
| `0x02` | 2 | Version `7` |
| `0x04` | 4 | Key string-table offset |
| `0x08` | 4 | Value string-table offset, or zero |
| `0x0C` | 4 | Root container offset |

Container counts and dictionary key indices are unsigned 24-bit
little-endian values. The safe reader supports arrays, dictionaries, UTF-8
strings, binary/file payloads, booleans, nulls, signed and unsigned 32-bit and
64-bit integers, binary32 floats and binary64 doubles. It bounds every offset,
limits container and document item counts, rejects duplicate dictionary keys
and detects recursive container cycles.

## Level discovery

A selectable level variant is the pair formed by removing `_Static` or
`_Dynamic` from a BCETT filename. A pair may contain either or both resources.
Static and dynamic actors are retained as separate metadata even though they
are imported into the same Blender level collection.

The BCETT root is a dictionary. `Actors` is an array of actor dictionaries;
other arrays such as rails and AI groups are not imported in this milestone.
The importer reads:

- `Gyaml`: actor type and actor-pack name;
- `Hash` and optional `SRTHash`: stable placement identifiers;
- `Translate`: required three-component game-space position;
- optional `Rotate`: XYZ Euler radians, defaulting to zero;
- optional `Scale`: three-component scale, defaulting to one; and
- optional `Name`: placement label.

Records without `Translate` do not describe an instantiable transform. They
are counted and reported rather than silently placed at the origin. Invalid
present fields are errors.

Game coordinates are Y-up. Blender placement matrices apply the same
right-handed Y-up to Z-up basis change as the glTF import:

```text
(x, y, z) -> (x, -z, y)
```

The complete SRT matrix is basis-converted, not just its translation, so
non-uniform scale and rotation remain aligned with the converted model.

## Actor model resolution

For each unique `Gyaml`, the importer reads
`Pack/Actor/<Gyaml>.pack.zs` as SARC and follows:

1. `Actor/<Gyaml>.engine__actor__ActorParam.bgyml`;
2. `Components.ModelInfoRef`;
3. the referenced `Component/ModelInfo/*.bgyml`; then
4. `ModelProjectName` and `FmdbName`.

The corresponding resource path is:

```text
Model/<ModelProjectName>.<FmdbName>.bfres.zs
```

Declared submodels use the same mapping. Each unique model is converted once;
placements use Blender collection instances. Actors with no model component
or an intentionally empty model reference become labeled cube empties. If
BfrAss rejects one model resource, only placements using that resource fall
back to markers; the level collection records the failed path and converter
error.

## Current non-goals

The preview does not import rails, AI groups, links, physics, effects,
gameplay logic or TerrainArc terrain. In particular, `.tscb` scene indices and
their `.hght`, `.mate` and `.extm` tile payloads require a separate native
height/material implementation. The collection property
`oot_remake_includes_terrain` is therefore explicitly false.
