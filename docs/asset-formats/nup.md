# NUP container reference

The title is delivered in a `.nup` container. This reference documents the
safe subset implemented by `oot_remake_importer/core/nup.py`.
It is based on direct observation of the user's dump and should not be treated
as a vendor specification.

## Superblock

All integers are little-endian. The importer requires a file of at least
`0x80` bytes.

| Offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 4 | Magic `NUP\x01` |
| `0x04` | 2 | Header size, at least `0x80` |
| `0x06` | 2 | Header version |
| `0x08` | 8 | Table-of-contents offset |
| `0x10` | 8 | Table-of-contents size |
| `0x18` | 32 | SHA-256 of the complete table of contents |
| `0x38` | 8 | Start of member extents |
| `0x40` | 8 | Recorded container size |
| `0x48` | 4 | Package count |
| `0x4C` | 4 | Flags; the observed and supported value is zero |

The reader verifies the recorded file size, bounds the table to 512 MiB and
checks its SHA-256 before parsing any record.

## Table of contents

The table begins with:

| Offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 4 | Magic `0x01434F54` (`TOC\x01` in little-endian bytes) |
| `0x04` | 4 | Package count |
| `0x08` | 8 | Directory count |
| `0x10` | 8 | File count |
| `0x18` | 8 | UTF-8 string-pool offset |
| `0x20` | 8 | String-pool size |
| `0x30` | - | First package record |

Records are contiguous in this order:

1. `package_count` package records of `0x40` bytes;
2. `directory_count` directory records of `0x18` bytes;
3. `file_count` file records of `0x50` bytes;
4. a 32-bit child-index table; and
5. the UTF-8 string pool.

### Package record

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | `u64` | Title ID |
| `0x08` | `u32` | Root directory index |
| `0x0C` | `u32` | ExeFS directory index or `0xFFFFFFFF` |
| `0x10` | `u32` | RomFS directory index or `0xFFFFFFFF` |
| `0x14` | `u32` | Version |
| `0x18` | `u8` | Kind: 0 base, 1 update, 2 add-on |
| `0x19` | `u8` | Package flags; bit 0 marks a standalone update |
| `0x20` | `u64` | Optional metadata-block file offset |
| `0x28` | `u32` | Metadata-block size |

Metadata is an external TLV stream with an eight-byte
`u16 kind, u16 reserved, u32 length` header per entry.

### Directory and child records

A directory record stores a name offset and length followed by the first
child index and child count. Child indices below `directory_count` refer to
directories; higher values refer to file index
`child - directory_count`.

The reader rejects absolute, empty, dot, parent and separator-containing
names, limits recursion to 127 levels and rejects duplicate resolved paths.

### File record

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | `u32` | Name offset |
| `0x04` | `u32` | Name length |
| `0x08` | `u64` | Decompressed size |
| `0x10` | `u64` | Stored extent offset |
| `0x18` | `u64` | Stored extent size |
| `0x20` | 32 bytes | SHA-256 of decompressed bytes |
| `0x40` | `u8` | Codec: 0 raw, 1 Zstandard |

Every selected member is size-bounded, decompressed if necessary, checked
against its declared size and verified with SHA-256 before use.

## Package selection

The Blender importer selects packages that expose a RomFS. It prefers the
highest-version update package and otherwise selects the highest-version
candidate. The expected title ID is `0400486024DEC000`.

## Non-goals

The reader does not modify NUP files, merge packages, expose ExeFS content to
the addon or attempt recovery from failed integrity checks. Unknown flags,
kinds and codecs are explicit errors.
