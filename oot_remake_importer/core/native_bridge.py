"""Typed ctypes access to the bundled direct BFRES/BNTX bridge."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


BRIDGE_VERSION = 1
MAX_LAYERS = 8


class NativeBridgeError(RuntimeError):
    """The native direct-import bridge rejected a resource."""


FloatPointer = ctypes.POINTER(ctypes.c_float)
UInt32Pointer = ctypes.POINTER(ctypes.c_uint32)


class _MeshView(ctypes.Structure):
    _fields_ = (
        ("name", ctypes.c_char_p),
        ("material_index", ctypes.c_uint32),
        ("vertex_count", ctypes.c_uint32),
        ("index_count", ctypes.c_uint32),
        ("face_count", ctypes.c_uint32),
        ("uv_layer_count", ctypes.c_uint32),
        ("color_layer_count", ctypes.c_uint32),
        ("positions", FloatPointer),
        ("normals", FloatPointer),
        ("tangents", FloatPointer),
        ("indices", UInt32Pointer),
        ("face_sizes", UInt32Pointer),
        ("uvs", FloatPointer * MAX_LAYERS),
        ("colors", FloatPointer * MAX_LAYERS),
    )


@dataclass(frozen=True, slots=True)
class NativeMeshView:
    name: str
    material_index: int
    vertex_count: int
    index_count: int
    face_count: int
    uv_layer_count: int
    color_layer_count: int
    positions: FloatPointer
    normals: FloatPointer
    tangents: FloatPointer
    indices: UInt32Pointer
    face_sizes: UInt32Pointer
    uvs: tuple[FloatPointer, ...]
    colors: tuple[FloatPointer, ...]

    def floats(self, pointer: FloatPointer, count: int):
        if not pointer:
            return None
        return (ctypes.c_float * count).from_address(ctypes.addressof(pointer.contents))

    def uints(self, pointer: UInt32Pointer, count: int):
        if not pointer:
            return None
        return (ctypes.c_uint32 * count).from_address(ctypes.addressof(pointer.contents))


@dataclass(frozen=True, slots=True)
class NativeMaterial:
    name: str
    two_sided: bool


@dataclass(frozen=True, slots=True)
class DecodedTexture:
    name: str
    width: int
    height: int
    png: bytes


def _library_filename() -> str:
    if os.name == "nt":
        return "ootr_native.dll"
    if os.sys.platform == "darwin":
        return "ootr_native.dylib"
    return "ootr_native.so"


def default_library_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / _library_filename()


def _text(value: bytes | None, label: str) -> str:
    if value is None:
        raise NativeBridgeError(f"native bridge returned no {label}")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NativeBridgeError(f"native bridge returned invalid UTF-8 for {label}") from error


@lru_cache(maxsize=4)
def _load_library(path: str):
    library_path = Path(path)
    if not library_path.is_file():
        raise NativeBridgeError(f"native bridge is missing: {library_path}")
    try:
        library = ctypes.CDLL(str(library_path))
    except OSError as error:
        raise NativeBridgeError(f"cannot load native bridge {library_path}: {error}") from error

    library.ootr_bridge_version.argtypes = ()
    library.ootr_bridge_version.restype = ctypes.c_uint32
    if library.ootr_bridge_version() != BRIDGE_VERSION:
        raise NativeBridgeError(
            f"native bridge ABI is {library.ootr_bridge_version()}, "
            f"expected {BRIDGE_VERSION}"
        )

    library.ootr_bfres_load.argtypes = (
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_char_p),
    )
    library.ootr_bfres_load.restype = ctypes.c_void_p
    library.ootr_scene_free.argtypes = (ctypes.c_void_p,)
    library.ootr_scene_free.restype = None
    library.ootr_scene_mesh_count.argtypes = (ctypes.c_void_p,)
    library.ootr_scene_mesh_count.restype = ctypes.c_uint32
    library.ootr_scene_material_count.argtypes = (ctypes.c_void_p,)
    library.ootr_scene_material_count.restype = ctypes.c_uint32
    library.ootr_scene_material_name.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    library.ootr_scene_material_name.restype = ctypes.c_char_p
    library.ootr_scene_material_two_sided.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    library.ootr_scene_material_two_sided.restype = ctypes.c_int
    library.ootr_scene_mesh.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_MeshView),
    )
    library.ootr_scene_mesh.restype = ctypes.c_int

    library.ootr_bntx_decode_png.argtypes = (
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_char_p),
    )
    library.ootr_bntx_decode_png.restype = ctypes.c_void_p
    library.ootr_image_free.argtypes = (ctypes.c_void_p,)
    library.ootr_image_free.restype = None
    library.ootr_image_name.argtypes = (ctypes.c_void_p,)
    library.ootr_image_name.restype = ctypes.c_char_p
    library.ootr_image_width.argtypes = (ctypes.c_void_p,)
    library.ootr_image_width.restype = ctypes.c_uint32
    library.ootr_image_height.argtypes = (ctypes.c_void_p,)
    library.ootr_image_height.restype = ctypes.c_uint32
    library.ootr_image_png_data.argtypes = (ctypes.c_void_p,)
    library.ootr_image_png_data.restype = ctypes.POINTER(ctypes.c_uint8)
    library.ootr_image_png_size.argtypes = (ctypes.c_void_p,)
    library.ootr_image_png_size.restype = ctypes.c_size_t
    return library


class NativeScene:
    def __init__(
        self,
        data: bytes,
        model_name: str | None = None,
        *,
        import_lods: bool = False,
        library_path: str | os.PathLike[str] | None = None,
    ):
        if not data:
            raise NativeBridgeError("BFRES input is empty")
        self._library = _load_library(
            str(Path(library_path or default_library_path()).resolve())
        )
        source = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        error = ctypes.c_char_p()
        self._handle = self._library.ootr_bfres_load(
            source,
            len(data),
            model_name.encode("utf-8") if model_name else None,
            int(import_lods),
            ctypes.byref(error),
        )
        if not self._handle:
            detail = _text(error.value, "BFRES error") if error.value else "unknown error"
            raise NativeBridgeError(f"direct BFRES import failed: {detail}")

    def close(self) -> None:
        if self._handle:
            self._library.ootr_scene_free(self._handle)
            self._handle = None

    def __enter__(self) -> "NativeScene":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def __del__(self):
        self.close()

    @property
    def mesh_count(self) -> int:
        return int(self._library.ootr_scene_mesh_count(self._handle))

    @property
    def materials(self) -> tuple[NativeMaterial, ...]:
        count = int(self._library.ootr_scene_material_count(self._handle))
        return tuple(
            NativeMaterial(
                name=_text(
                    self._library.ootr_scene_material_name(self._handle, index),
                    "material name",
                ),
                two_sided=bool(
                    self._library.ootr_scene_material_two_sided(
                        self._handle,
                        index,
                    )
                ),
            )
            for index in range(count)
        )

    def mesh(self, index: int) -> NativeMeshView:
        if not 0 <= index < self.mesh_count:
            raise IndexError(index)
        raw = _MeshView()
        if not self._library.ootr_scene_mesh(self._handle, index, ctypes.byref(raw)):
            raise NativeBridgeError(f"native bridge could not read mesh {index}")
        return NativeMeshView(
            name=_text(raw.name, "mesh name"),
            material_index=int(raw.material_index),
            vertex_count=int(raw.vertex_count),
            index_count=int(raw.index_count),
            face_count=int(raw.face_count),
            uv_layer_count=int(raw.uv_layer_count),
            color_layer_count=int(raw.color_layer_count),
            positions=raw.positions,
            normals=raw.normals,
            tangents=raw.tangents,
            indices=raw.indices,
            face_sizes=raw.face_sizes,
            uvs=tuple(raw.uvs[index] for index in range(raw.uv_layer_count)),
            colors=tuple(raw.colors[index] for index in range(raw.color_layer_count)),
        )


def decode_bntx_png(
    data: bytes,
    texture_name: str,
    *,
    library_path: str | os.PathLike[str] | None = None,
) -> DecodedTexture:
    if not data:
        raise NativeBridgeError("BNTX input is empty")
    library = _load_library(str(Path(library_path or default_library_path()).resolve()))
    source = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    error = ctypes.c_char_p()
    handle = library.ootr_bntx_decode_png(
        source,
        len(data),
        texture_name.encode("utf-8"),
        ctypes.byref(error),
    )
    if not handle:
        detail = _text(error.value, "BNTX error") if error.value else "unknown error"
        raise NativeBridgeError(f"direct BNTX decode failed: {detail}")
    try:
        size = int(library.ootr_image_png_size(handle))
        pointer = library.ootr_image_png_data(handle)
        if size == 0 or not pointer:
            raise NativeBridgeError("native bridge returned an empty PNG")
        return DecodedTexture(
            name=_text(library.ootr_image_name(handle), "texture name"),
            width=int(library.ootr_image_width(handle)),
            height=int(library.ootr_image_height(handle)),
            png=ctypes.string_at(pointer, size),
        )
    finally:
        library.ootr_image_free(handle)
