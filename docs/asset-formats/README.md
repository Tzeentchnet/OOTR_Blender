# Asset format references

These notes describe the subset of the title's asset pipeline that the Blender
importer reads. They are implementation references, not general specifications
for every Nintendo title or every version of the formats.

| Reference | Scope |
|---|---|
| [NUP container](nup.md) | Package table of contents, directory tree, member integrity and compression |
| [BFRES 10](bfres.md) | Model, skeletal-animation, material and external-string records used by the importer |
| [BNTX 4](bntx.md) | Texture archive and BRTI fields consumed through BfrAss |
| [BYML level placements](byml.md) | BCETT scene pairing, actor transforms and actor-pack model resolution |
| [TerrainArc](terrainarc.md) | TSCB areas, TARB resources, block-linear HGHT surfaces and MATE blends |
| [Material semantics](materials.md) | Sampler catalog, vertex-control classification and Blender mapping |

## Evidence labels

The documents use three evidence levels:

- **Verified**: checked against the title's user-owned dump and exercised by
  importer tests or validation imports.
- **Upstream**: behavior supplied by a pinned dependency and linked from
  [the citations document](../citations.md).
- **Observed**: consistent across the studied sample set but not yet proven to
  be a format-wide contract.

No extracted file, texture, model, key or game-derived screenshot belongs in
this directory. Synthetic test fixtures may reproduce record shapes, but not
game bytes. Real validation outputs stay in ignored `build/` directories or
outside the repository.
