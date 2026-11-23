# SPDX-License-Identifier: Apache-2.0
"""Owned native/portable/UDIM generation, auto provenance and safe clearing."""

from pathlib import Path
from array import array
import argparse
import json
import os
import subprocess
import time

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import texture_cache as cache
from support.paths import artifacts_root


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", type=Path)
parser.add_argument("--blender-rop", action="store_true", help="Also exercise a real linked Blender image material")
args = parser.parse_args()
folder = artifacts_root() / "texture-cache-actions" / str(time.time_ns())
folder.mkdir(parents=True)
native = ch.load()
original = OCIO.GetCurrentConfig()
original_path = hou.getenv("OCIO")
original_mode = hou.getenv("HOUDINI_OCIO_FILENAME_COLORSPACE")


def write(path, portable=False):
    spec = OIIO.ImageSpec(64, 64, 4, OIIO.FLOAT)
    if portable:
        spec.attribute("colorInteropID", "lin_ap0_scene")
    pixels = np.ones((64, 64, 4), dtype=np.float32) * (0.25, 0.5, 0.75, 1)
    output = OIIO.ImageOutput.create(str(path))
    assert output.open(str(path), spec) and output.write_image(pixels) and output.close()


def material(path, colorspace="data"):
    return dict(
        key="surface",
        kind="graph",
        graph=dict(
            schema_version=1,
            nodes=[
                dict(
                    id="file",
                    definition="cycles/image_texture/1",
                    parameters=dict(filename=str(path), colorspace=colorspace, alpha_type="channel_packed"),
                ),
                dict(id="light", definition="cycles/emission/1", parameters=dict(strength=1.0)),
            ],
            links=[dict(**{"from": dict(node="file", socket="color"), "to": dict(node="light", socket="color")})],
            surface=dict(node="light", socket="emission"),
        ),
    )


def plan(value):
    handle = native.create("CPU", 8, 8, 1, False, False, False, False, None, True, 1)
    try:
        owned = native.apply(handle, dict(materials=[value]), hou.time(), False)
        return native.texture_cache_plan(owned)
    finally:
        native.close(handle)


def run_manual(path, *, portable=False, tiles=False):
    if tiles:
        write(Path(str(path).replace("<UDIM>", "1001")), portable)
        write(Path(str(path).replace("<UDIM>", "1002")), portable)
    else:
        write(path, portable)
    items = plan(material(path, "auto" if portable else "data"))
    assert len(items) == (2 if tiles else 1), items
    assert all((".portable-" in item["destination"]) == portable for item in items), items
    before = {item["source"]: Path(item["source"]).read_bytes() for item in items}
    generated = cache.generate(items, native.texture_cache_generate)
    assert len(generated["generated"]) == len(items) and not generated["errors"], generated
    assert all(cache._owned(item["source"], item["destination"]) for item in items)
    reused = cache.generate(items, native.texture_cache_generate)
    assert len(reused["reused"]) == len(items) and not reused["errors"], reused
    legacy = Path(items[0]["destination"]).with_name(Path(items[0]["source"]).name + ".7-" + "f" * 32 + ".tx")
    legacy.write_bytes(b"unowned user image")
    cleared = cache.clear(items)
    assert len(cleared["cleared"]) == len(items) and not cleared["errors"], cleared
    assert legacy.read_bytes() == b"unowned user image"
    assert all(Path(path).read_bytes() == data for path, data in before.items())
    assert all(not Path(item["destination"]).exists() for item in items)
    return dict(generated=generated, reused=reused, cleared=cleared)


reports = []
reports.append(run_manual(folder / "stock.exr"))
# A fresh source directory exposes discovery-directory timestamp changes caused
# by the first generated UDIM's cache namespace.
udim = folder / "udim"
udim.mkdir()
reports.append(run_manual(udim / "stock.<UDIM>.exr", tiles=True))

# Automatic rendering must produce the same positive provenance as the button.
auto = folder / "automatic.exr"
write(auto)
geo = hou.node("/obj").createNode("geo", "cache_action_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parmTuple("size").set((8, 8))
uv = geo.createNode("attribwrangle")
uv.setInput(0, grid)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "cache_action_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
with ch.Session(
    device="CPU",
    width=8,
    height=8,
    samples=1,
    execution="sync",
    interactive=False,
    use_texture_cache=True,
    auto_texture_cache=True,
    threads=1,
) as session:
    frame = session.wait(
        session.apply(
            dict(
                geometry=[ch.geometry_request(geo, uv, material="surface")],
                materials=[material(auto)],
                camera_path=camera.path(),
            )
        )
    )
    pixels = array("f")
    pixels.frombytes(frame["planes"]["combined"]["pixels"])
    center = np.asarray(pixels).reshape((8, 8, 4))[4, 4]
    assert np.allclose(center, (0.25, 0.5, 0.75, 1), rtol=0, atol=1e-5), center
auto_items = plan(material(auto))
assert auto_items and all(cache._owned(item["source"], item["destination"]) for item in auto_items), auto_items
automatic = cache.clear(auto_items)
assert len(automatic["cleared"]) == len(auto_items) and not automatic["errors"], automatic
reports.append(dict(automatic=automatic))


def rop_actions(rop, original_sources, label):
    """Actual HDA buttons must acquire a scene using capture-only native handles."""
    captures = []
    create, session_type = native.create, ch.Session

    def capture_only_create(*values, **options):
        assert len(values) > 9 and values[9] is True, (values, options)
        assert values[1:4] == (1, 1, 1), values
        captures.append(dict(device=values[0], capture_only=values[9], dimensions=list(values[1:3])))
        return create(*values, **options)

    def no_render_session(*_, **__):
        raise AssertionError("Texture cache buttons must not construct a rendering session")

    native.create, ch.Session = capture_only_create, no_render_session
    before = {str(path): Path(path).read_bytes() for path in original_sources}
    try:
        items = cache.plan_from_rop(rop)
        assert items and any(item["destination"] for item in items), items
        rop.parm("texture_cache_generate").pressButton()
        generated = rop.cachedUserData("cycles_houdini.texture_cache")
        assert generated["action"] == "generate" and generated["generated"] and not generated["errors"], generated
        assert all(Path(path).is_file() for path in generated["generated"]), generated
        rop.parm("texture_cache_clear").pressButton()
        cleared = rop.cachedUserData("cycles_houdini.texture_cache")
        assert cleared["action"] == "clear" and not cleared["errors"], cleared
        assert set(cleared["cleared"]) == set(generated["generated"]), (generated, cleared)
        assert all(not Path(path).exists() for path in cleared["cleared"]), cleared
        assert len(captures) == 3, captures
        assert all(Path(path).read_bytes() == data for path, data in before.items())
    finally:
        native.create, ch.Session = create, session_type
    return dict(rop=label, generated=generated, cleared=cleared, capture_only_sessions=captures)


# This follows the actual public VOP -> object material assignment -> ROP path,
# including prototype-lease ownership in plan_from_rop, rather than synthetic
# snapshot plans or direct calls to the action helpers.
button_source = folder / "rop-image.exr"
write(button_source)
builder = ch.create_material(hou.node("/mat"), "cache_button_surface", shader="emission")
texture = builder.createNode("cycles::image_texture::1", "file_image")
texture.parm("filename").set(str(button_source))
texture.parm("colorspace").set("data")
emission = next(node for node in builder.children() if node.type().name() == "cycles::emission::1")
emission.setNamedInput("color", texture, "color")
uv.setDisplayFlag(True)
uv.setRenderFlag(True)
geo.parm("shop_materialpath").set(builder.path())
rop = hou.node("/out").createNode("cycles_ipr", "cache_action_buttons")
from cycles_houdini.device_selection import set_devices

set_devices(rop, "CPU")
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject=geo.name(),
        lights="",
        use_material_assignments=1,
        soppath=uv.path(),
        camera=camera.path(),
        materialpath="",
        resx=8,
        resy=8,
        use_denoise=0,
        world_source="color",
    )
)
reports.append(rop_actions(rop, [button_source], "native Houdini material"))

if args.blender_rop:
    from cycles_houdini.core.blender_bridge import require_executable

    blend_source = folder / "blend-image.exr"
    write(blend_source)
    blend_file = folder / "image-material.blend"
    generator = folder / "make_blend_image.py"
    generator.write_text(
        "import bpy\n"
        "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
        "bpy.ops.mesh.primitive_plane_add()\n"
        "obj = bpy.context.object\n"
        "obj.name = 'CacheAsset'\n"
        "material = bpy.data.materials.new('Linked Image Material')\n"
        "material.use_nodes = True\n"
        "material.node_tree.nodes.clear()\n"
        "image = material.node_tree.nodes.new('ShaderNodeTexImage')\n"
        f"image.image = bpy.data.images.load({str(blend_source)!r})\n"
        "image.image.colorspace_settings.name = 'Non-Color'\n"
        "emission = material.node_tree.nodes.new('ShaderNodeEmission')\n"
        "output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')\n"
        "material.node_tree.links.new(image.outputs['Color'], emission.inputs['Color'])\n"
        "material.node_tree.links.new(emission.outputs[0], output.inputs['Surface'])\n"
        "obj.data.materials.append(material)\n"
        f"bpy.ops.wm.save_as_mainfile(filepath={str(blend_file)!r}, relative_remap=False)\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "OCIO")}
    with (folder / "blender-fixture.log").open("w", encoding="utf-8") as log:
        subprocess.run(
            [
                require_executable(),
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--python-exit-code",
                "1",
                "--python",
                str(generator),
            ],
            env=environment,
            check=True,
            timeout=120,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    blender_geo = hou.node("/obj").createNode("geo", "cache_button_blender", run_init_scripts=False)
    reference = blender_geo.createNode("cycles::blender_reference", "linked_image_asset")
    reference.setParms(dict(file=str(blend_file), ref_target_kind1="object", ref_target1="CacheAsset"))
    reference.setDisplayFlag(True)
    reference.setRenderFlag(True)
    rop.setParms(dict(vobject=blender_geo.name(), soppath=reference.path(), materialpath=""))
    reports.append(rop_actions(rop, [blend_source, blend_file], "linked Blender image material"))

try:
    # Deliberately omit named ACES/Rec.709 source spaces, exercising the owned
    # PortableImageLoader conversion rather than stock OCIO cache generation.
    builtin = OCIO.Config.CreateFromBuiltinConfig("studio-config-latest")
    config = OCIO.Config()
    config.setVersion(2, 5)
    reference = OCIO.ColorSpace(name="Cache Reference")
    working = OCIO.ColorSpace(name="Cache Working")
    working.setTransform(
        builtin.getProcessor("ACEScg", "ACES2065-1").createGroupTransform(), OCIO.COLORSPACE_DIR_TO_REFERENCE
    )
    raw = OCIO.ColorSpace(name="Cache Raw", isData=True)
    for space in (reference, working, raw):
        config.addColorSpace(space)
    for role in (OCIO.ROLE_SCENE_LINEAR, OCIO.ROLE_DEFAULT):
        config.setRole(role, working.getName())
    config.setRole(OCIO.ROLE_DATA, raw.getName())
    for role in (OCIO.ROLE_COMPOSITING_LOG, OCIO.ROLE_COLOR_TIMING, "aces_interchange"):
        config.setRole(role, reference.getName())
    config.addDisplayView("Cache Display", "Raw", working.getName(), "")
    config.validate()
    config_path = folder / "portable.ocio"
    config_path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(config_path))
    hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "0")
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
    reports.append(run_manual(folder / "portable.exr", portable=True))
    portable_udim = folder / "portable-udim"
    portable_udim.mkdir()
    reports.append(run_manual(portable_udim / "image.<UDIM>.exr", portable=True, tiles=True))
finally:
    if original_path is not None:
        hou.putenv("OCIO", original_path)
    else:
        hou.unsetenv("OCIO")
    if original_mode is not None:
        hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", original_mode)
    else:
        hou.unsetenv("HOUDINI_OCIO_FILENAME_COLORSPACE")
    OCIO.SetCurrentConfig(original)
    hou.Color.reloadOCIO()

report = dict(result="passed", cases=reports)
report_path = args.report or folder.parent / "report.json"
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
