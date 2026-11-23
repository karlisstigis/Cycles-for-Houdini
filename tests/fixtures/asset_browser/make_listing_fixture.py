# SPDX-License-Identifier: Apache-2.0
"""Regenerate `listing_v1/` with Blender's own listing generator.

Run with a Python that has Blender as a module (`import bpy`):
    python make_listing_fixture.py <empty folder>
then copy the JSON files, the catalog file and the thumbnail (not the .blend)
into listing_v1/. The committed fixture came from Blender 5.3.0 (bpy module,
generator schema 1.0.0) on 2026-11-12.
"""

import os
import sys


def main(library):
    import bpy
    from _bpy_internal.assets.remote_library import cli

    os.makedirs(os.path.join(library, "materials"), exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    metal, props = "d7a2f3c1-0d5e-4c4b-9a8e-1f2e3d4c5b6a", "a1b2c3d4-e5f6-4a5b-8c7d-9e0f1a2b3c4d"
    with open(os.path.join(library, "blender_assets.cats.txt"), "w", encoding="utf-8") as stream:
        stream.write(f"# test catalogs\nVERSION 1\n\n{metal}:Materials/Metal:Materials-Metal\n{props}:Props:Props\n")
    material = bpy.data.materials.new("Metal - Galvanized")
    material.asset_mark()
    data = material.asset_data
    data.catalog_id = metal
    data.tags.new("metal")
    data.tags.new("shiny")
    data.author, data.description, data.license, data.copyright = "Test Author", "A galvanized metal", "CC0", "nobody"
    material.preview_ensure()
    material.preview.image_size = (4, 4)
    material.preview.image_pixels_float = [0.5, 0.5, 0.6, 1.0] * 16
    material.use_fake_user = True
    mesh = bpy.data.meshes.new("CubeMesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    prop = bpy.data.objects.new("Tri Prop", mesh)
    bpy.context.scene.collection.objects.link(prop)
    prop.asset_mark()
    prop.asset_data.catalog_id = props
    prop.asset_data.tags.new("prop")
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(library, "materials", "metal.blend"))
    print("Blender", bpy.app.version_string)
    cli.main(["generate", library])


if __name__ == "__main__":
    main(os.path.abspath(sys.argv[1]))
