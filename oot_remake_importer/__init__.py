"""Blender entry point for the Ocarina of Time Remake importer."""

bl_info = {
    "name": "Ocarina of Time Remake Importer",
    "author": "OOT Remake Importer contributors",
    "version": (0, 6, 0),
    "blender": (5, 2, 0),
    "location": "3D View > Sidebar > OoT Remake",
    "description": "Import models, level placements, and terrain from an Ocarina of Time Remake NUP",
    "category": "Import-Export",
}

try:
    import bpy as _bpy  # noqa: F401
except ModuleNotFoundError:
    _bpy = None

if _bpy is not None:
    from .addon import register, unregister
