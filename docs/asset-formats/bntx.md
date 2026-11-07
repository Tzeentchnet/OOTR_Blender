# BNTX 4 texture reference

The imported models reference Switch BNTX `4.1.1` archives. The addon locates
the required archives by exact texture names found in the selected BFRES,
decompresses only those NUP members and supplies them to BfrAss.

## Container

The pinned BfrAss reader requires:

| Offset | Meaning |
|---:|---|
| `0x00` | Eight-byte `BNTX\0\0\0\0` signature |
| `0x08` | Container version |
| `0x0C` | Byte-order marker |
| `0x20` | Four-byte platform marker |
| `0x24` | Texture count |
| `0x28` | Pointer to the BRTI-pointer array |

Standard Switch archives identify the platform as `NX  `. The title's
Switch 2 archives use `Ounc`. BfrAss reports that difference and currently
decodes the resources through its NX path. Successful decoding across the
validation set is **observed compatibility**, not proof that all `Ounc`
texture layouts are identical to the earlier Switch layout.

## BRTI texture record

Each pointer in the container array leads to a `BRTI` record. The fields used
by the converter are:

| BRTI offset | Meaning |
|---:|---|
| `0x11` | Storage dimension |
| `0x12` | Tile mode |
| `0x16` | Mip count |
| `0x1C` | Image format |
| `0x24` | Width |
| `0x28` | Height |
| `0x2C` | Depth |
| `0x30` | Array count |
| `0x34` | Texture layout |
| `0x50` | Image-data size |
| `0x54` | Alignment |
| `0x58`...`0x5B` | Four channel mappings |
| `0x5C` | Image dimension |
| `0x60` | Texture-name string reference |
| `0x70` | Mip-pointer array |
| `0x78` | User-data array |
| `0x98` | User-data dictionary |

The converter derives per-mip offsets from the pointer array, deswizzles the
NX layout, applies the channel map, reconstructs supported normal Z channels
and encodes the result as an embedded PNG for glTF.

## Selection and safety

The addon builds an exact index keyed by BNTX archive stem. Candidate names
must appear as complete ASCII resource names in the BFRES. When duplicate
stems exist, the importer prefers `Tex/`, then the shortest path, then lexical
order. A user-configurable texture-count limit is enforced before decoding.

Zstandard frames must declare their decompressed size, stay below 2 GiB and
reference a dictionary present in `Pack/ZsDic.pack.zs`. The dictionary pack is
a bounds-checked SARC subset; unsafe member names and duplicate dictionary IDs
are errors.

## Known gaps

- Game-specific packed maps such as `_rah0` and `_rah1` are retained as
  bindings but not split into Blender channels.
- `_rgh0` and `_mt0` are not forced into glTF's combined metallic-roughness
  texture until their channel semantics are verified.
- Some normal-map channel combinations need broader corpus validation.
- Missing texture references produce explicit generated placeholders and are
  recorded on the imported collection.
