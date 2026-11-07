# BFRES 10 model reference

The Blender addon imports the title's Switch BFRES `10.4.0` model resources.
Geometry and textures are converted by pinned BfrAss and Assimp builds.
Small bounds-checked Python readers extract the BFRES 10 material metadata
that glTF cannot represent faithfully and the skeletal animation data that
the pinned converter currently lists but does not decode.

## Identification and top-level model table

| Offset | Meaning |
|---:|---|
| `0x00` | Eight-byte Switch magic `FRES    ` |
| `0x08` | NX version bytes; byte `0x0A` is major version in the observed files |
| `0x28` | Pointer to the FMDL model array |
| `0x30` | Pointer to the model dictionary |

The addon accepts BFRES major version 10 or newer for this material path. The
observed BFRES 10 FMDL record is `0x78` bytes:

| FMDL offset | Meaning |
|---:|---|
| `0x00` | `FMDL` signature |
| `0x08` | Model-name string reference |
| `0x18` | Pointer to the FSKL skeleton |
| `0x38` | Pointer to the FMAT array |
| `0x6C` | `u16` material count |

The observed FSKL record stores its transform-mode flags at `0x04`, bone-array
pointer at `0x10`, and bone count at `0x38`. BFRES 10 bone records are `0x58`
bytes. Their indexed fields start at `0x20`: parent index at `0x22`, transform
flags at `0x2C`, scale at `0x30`, rotation at `0x3C`, and translation at
`0x4C`. Transform flag bit 23 enables Maya segment-scale compensation.

## BFRES 10 FMAT record

The observed record size is `0xB0`.

| FMAT offset | Meaning |
|---:|---|
| `0x00` | `FMAT` signature |
| `0x08` | Material-name string reference |
| `0x10` | Shader-info pointer |
| `0x20` | Texture-name pointer array |
| `0x30` | Sampler-info array |
| `0x38` | Sampler dictionary |
| `0x40` | Per-material render-info data |
| `0x48` | Render-info counts |
| `0x50` | Render-info offsets |
| `0x58` | Per-material shader-parameter source data |
| `0x70` | User-data array |
| `0xA0` | Material index |
| `0xA2` | Sampler count |
| `0xA3` | Texture count |
| `0xA6` | User-data count |

The shader-info record points to a shared shader assignment. The assignment
contains shader archive and model names plus dictionaries for render infos,
shader parameters, attribute assignments, sampler assignments and options.
Shader-parameter descriptors are 24 bytes; the fields used by the addon are:

| Descriptor offset | Meaning |
|---:|---|
| `0x08` | Parameter-name string reference |
| `0x10` | `u16` offset into the FMAT source-parameter data |
| `0x12` | `u16` parameter type |

The reader supports scalar and vector bool, signed integer, unsigned integer
and float types, float matrices, 2D and 3D SRT values, and texture SRT values.
Unknown types and out-of-bounds source ranges are errors.

## External hashed strings

BFRES 10.4 shader metadata in this title often stores a 64-bit hash where
older readers expect an in-file string pointer. The companion resource
`Shader/ExternalBinaryString.bfres.zs` resolves those hashes.

In the observed external-string BFRES:

| Offset | Meaning |
|---:|---|
| `0xB8` | Pointer to a sorted array of 64-bit string hashes |
| `0xC0` | Pointer to an `nn::util::ResDic` |

For dictionary entry `i`, hash `i` maps to dictionary key node `i + 1`.
The addon validates both arrays, rejects duplicate conflicting hashes and
raises an error for an unresolved external reference. It does not silently
substitute blank shader names.

## BFRES 10 skeletal animations

Standalone `Model/*.anim.bfres.zs` resources can contain one or many FSKA
clips. BFRES 10.4 keeps the 32 reserved bytes after the model table that
version 9 introduced, so each animation kind's array and dictionary sit 0x20
later than in older Switch BFRES (BfresLibrary,
`Switch/ResFileParser.cs::ResFileParser.Load`):

| Offset | Meaning |
|---:|---|
| `0x58` / `0x60` | FSKA skeletal-animation array / dictionary |
| `0x68` / `0x70` | FMAA material-animation array / dictionary |
| `0x78` / `0x80` | FBVS bone-visibility-animation array / dictionary |
| `0x88` / `0x90` | FSHA shape-animation array / dictionary |
| `0x98` / `0xA0` | FSCN scene-animation array / dictionary |
| `0xB8` / `0xC0` | Embedded external-file array / dictionary |

Of the title's 1,427 animation resources, 1,284 contain skeletal clips. The
other 143 hold only material animations (132), only bone-visibility
animations (5) or both (6); the importer names their contents and rejects
them. Embedded files in animation resources are empty `LoadAnimEventSet.flag`
markers.

Three related resource kinds sit beside them:

- `Model/<Name>_Animation.Skeleton.bfres.zs` (55 files) contains only the
  FSKL that `<Name>_Animation.anim.bfres.zs` was authored against, with no
  shapes or materials. It imports as a bare armature.
- `CameraAnimation/*.camera.bfres.zs` (236 files) holds no FSCN data: only
  embedded 116-byte `.recb` records named `Default`, `View01`, and so on.
  They are not animation curves and are not imported.
- `Model/*.bfres.mc` (5 files) are `MCPK` mesh-codec packages; each has a
  `.bfres.zs` twin, which is the one imported.

Each observed FSKA record is `0x50` bytes:

| FSKA offset | Meaning |
|---:|---|
| `0x00` | `FSKA` signature |
| `0x04` | Loop, baked, scale-mode and rotation-mode flags (rotation `0x0000` quaternion, `0x1000` Euler XYZ, `0x2000` mixed) |
| `0x08` | Animation-name string reference |
| `0x28` | Pointer to the bone-animation array |
| `0x40` | Signed frame count |
| `0x44` | Total curve count |
| `0x4C` | Bone-animation count |

The observed bone-animation record is `0x38` bytes. It names the target bone,
points to a `0x30`-byte curve array and optional base scale, rotation and
translation values, and records the curve count at `0x2E`. Curve targets use
the NintendoWare transform offsets `0x04`-`0x0C` for scale, `0x10`-`0x18`
for translation and `0x20`-`0x2C` for rotation.

Rotation mode `0x2000` (6,216 of the title's 22,875 clips, most of Link's
and many cutscene clips) mixes Euler XYZ bones with quaternion bones.
Bone-animation flag bit 20 (`0x100000`) marks a quaternion bone: its base
rotation is a quaternion `(x, y, z, w)` and its only rotation curve is a
whole-quaternion curve aimed at `0x20`. Every other bone stores Euler XYZ
radians in the skeleton's order. Bit 20 is per bone: all 12,661 such bones
in the title follow this rule, one of them inside an ordinary `0x1000` clip.
Verified because the quaternion base value equals the curve's first key and
the Euler bases equal the FSKL rest rotations.

BFRES 10 curve records store frame and key-array pointers at `0x00` and
`0x08`, encoding flags and key count at `0x10`, the transform target at
`0x14`, and the frame range at `0x18`. Bit 14 marks the BFRES 10 extended
layout. Its cubic keys are Hermite point/tangent pairs rather than the four
polynomial coefficients used by the legacy layout. Quantized points use the
value scale and addend at `0x24` and `0x28`. Bit 11 selects a separate
tangent scale and addend stored as two floats at the start of the key array,
followed by 8- or 16-bit point/tangent pairs. Both lanes are unsigned: each
addend is the minimum of its range, so a quantizer covers the complete
minimum-to-maximum range. Reading the point lane as signed wraps every point
in the upper half of the range by 65,536 (or 256) times the scale; in the
title's clips this turned rest rotations of 0 into -3π and flipped bones by
180 degrees. Across the Link, Epona, Stalfos and Zelda clips, unsigned points
are the only reading whose tangents agree with the points' finite
differences.

Frame arrays use floats, Q10.5 signed integers or bytes. When flag bit 10 is
set, `0x20` holds the interval scale and integer frame values are unsigned
offsets from the curve's start frame. Otherwise `0x20` is zero. This differs
from the legacy layout commonly documented with a value scale at `0x20`.
The unsigned reading matters for 16-bit frames: `Animal_Cow_Animation`
stores the last key of a 1,100-frame curve with a 1/32 interval as 35,200,
which a signed read wraps to -30,336, so 103 resources failed with
out-of-order frames. Unsigned, the last key lands on the curve's end frame
in every interval-scaled curve.

Curve type is the high nibble of the low flag byte. Besides the scalar
types, `0x80`, `0x90` and `0xA0` are whole-quaternion curves whose keys are
four floats each (spherical, normalized-linear and a second spherical
variant, per Watertoon/Awn's `BfresAnimCurveCurveType`, read for behaviour
only). The title uses only `0x80`, always with float keys and 16-bit or
byte frames; the importer interpolates along the shorter arc.
Hermite interpolation scales each endpoint tangent by the interval length,
so every decoded key value is reproduced exactly and adjacent segments are
continuous.

The importer combines each FSKA sample with the matching model's complete
FSKL hierarchy. It evaluates scale-less, standard, Maya and Softimage
hierarchy rules, including Maya segment-scale compensation on descendants
that have no FSKA track of their own. Before Blender imports the static model,
the addon appends every missing FSKL node to the glTF skin without changing
the existing weighted-joint order. This produces the complete source
hierarchy as one Blender armature instead of attachment and control empties.

The evaluated matrices do not pass through glTF animation channels. The
addon keys location, quaternion rotation and scale directly on a Blender
Action, computing each pose bone's basis from the bone's authored local
transform rather than by inverting its parent's evaluated matrix. Two cases
make the distinction necessary:

- In Maya mode, a segment-scale-compensating bone under a non-uniformly
  scaled parent is not sheared in model space, but its matrix relative to
  that parent is; a pose basis cannot hold shear. The addon gives such bones
  Blender's `inherit_scale = 'NONE'`, which keeps the parent's rotation and
  scaled location but drops its scale, the same rule. 2,071 of the title's
  clips (9%) need this, typically breathing and squash-and-stretch on skirts,
  tails and faces.
- The title hides parts by scaling a bone to zero. Nothing is inverted, so
  zero-scale parents key like any other.

Both reconstructions, and any non-uniform scale, need each Blender bone's
rest orientation to be an axis permutation of its source bone's, because a
pose basis scales only along the bone's own axes. The glTF importer produces
that for nearly every bone; the others are turned in place to the nearest
permutation before keying.

The addon then evaluates the Action at every frame and rejects it if any
bone's Blender pose differs from the evaluated BFRES pose by more than
2e-4 Blender units. This is a Blender-transfer consistency check, not an
independent oracle for curve decoding. Every FSKA bone name must exist in
the model.

## Vertex attributes and the glTF mismatch

BfrAss recognizes conventional BFRES names such as:

| Attribute | Meaning in the generic converter |
|---|---|
| `_p0` | Position |
| `_n0` | Normal |
| `_t0` / `_b0` | Tangent / bitangent |
| `_u0`...`_u4` | Texture coordinates |
| `_i0`, `_i1` | Bone indices |
| `_w0`, `_w1` | Bone weights |
| `_c0`, `_c1`, ... | Assimp color sets, exported as glTF `COLOR_n` |

glTF defines `COLOR_n` as a linear color multiplier. Nintendo's Model shader
also uses BFRES color attributes as packed controls. The material adapter
therefore classifies `_c0` using source material metadata before accepting
the standard glTF interpretation. See [Material semantics](materials.md).

## Scope and uncertainty

The offsets above are verified for the title's BFRES 10.4 model and skeletal
animation resources and cross-checked against the pinned sources in
[Citations](../citations.md).
They are not promises about other BFRES major versions. Secondary albedo is
reconstructed on the direct static path from observed title semantics;
generated and secondary normals, packed maps, skin and fur lookup tables, and
other shader behavior remain title-specific research areas.
