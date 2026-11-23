# SPDX-License-Identifier: Apache-2.0
"""Visible area-light texture coordinates, owned COP inputs and native light compatibility."""

import argparse, json, math, traceback
import hou, numpy as np, OpenImageIO as oiio
from cycles_houdini import registry, light_properties, object_scene, _native
from light_shader_fixture import Fixture, write_image

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
f.rop.parm("vobject").set("")
f.light.setParms(dict(cycles_light_type="grid", tz=0, ry=180, normalizearea=0, light_intensity=math.pi))
f.light.setParms(dict(areasizex=2, areasizey=2))
entry = f.session()
report = dict(result="running", device=f.device, checks=[])


def render(label, expected=None, code=None):
    frame = f.update()
    status = entry.session.status()
    pixels = f.pixels(frame)
    assert status["state"] == "complete" and not status["error"] and status["geometry_resources"] == 0, status
    assert status["render_device_ids"] == [f.device] and np.isfinite(pixels).all(), status
    assert {d["code"] for d in status["source_diagnostics"]} == ({code} if code else set()), status[
        "source_diagnostics"
    ]
    if expected is not None:
        np.testing.assert_allclose(pixels[30:34, 30:34, :3].mean((0, 1)), expected, rtol=0, atol=0.0005)
    report["checks"].append(dict(label=label, status=status))
    return pixels, status


try:
    render("Visible area emission matches power/pi when Normalize is off", (1, 1, 1))
    colors = np.array(((0.6, 0.05, 0.05), (0.05, 0.6, 0.05), (0.05, 0.05, 0.6), (0.6, 0.6, 0.05)), np.float32)
    pixels = np.broadcast_to(np.repeat(colors, 4, axis=0)[None, :, :], (8, 16, 3)).copy()
    image = f.dest / "striped-area.exr"
    output = oiio.ImageOutput.create(str(image))
    assert output.open(str(image), oiio.ImageSpec(16, 8, 3, oiio.FLOAT))
    assert output.write_image(pixels) and output.close()
    f.light.setParms(dict(cycles_light_texture=str(image), cycles_light_texture_colorspace="data"))
    pixels, first = render("Area surface uses normalized light-local image coordinates")
    np.testing.assert_allclose(pixels[32, 20, :3], colors[3], atol=0.0005, rtol=0)
    np.testing.assert_allclose(pixels[32, 44, :3], colors[0], atol=0.0005, rtol=0)
    f.light.setParms(dict(areasizex=4, areasizey=2))
    pixels, _ = render("Changing area dimensions updates coordinates without a mesh adapter")
    np.testing.assert_allclose(pixels[32, 20, :3], colors[2], atol=0.0005, rtol=0)
    np.testing.assert_allclose(pixels[32, 44, :3], colors[1], atol=0.0005, rtol=0)
    f.parent.parm("sx").set(2)
    pixels, _ = render("Parent scale leaves the authored area size and texture mapping intact")
    np.testing.assert_allclose(pixels[32, 20, :3], colors[2], atol=0.0005, rtol=0)
    np.testing.assert_allclose(pixels[32, 44, :3], colors[1], atol=0.0005, rtol=0)
    f.parent.parm("sx").set(1)
    f.light.setParms(dict(areasizex=2, areasizey=2))
    write_image(f.texture, (0.25, 0.5, 0.75))
    f.light.parm("cycles_light_texture").set(str(f.texture))
    for shape in ("square", "rectangle", "disk", "ellipse"):
        f.light.parm("cycles_light_area_shape").set(shape)
        render("Constant color texture on " + shape, (0.25, 0.5, 0.75))
    f.light.parm("cycles_light_area_shape").set("rectangle")
    legacy = hou.node("/img").createNode("img", "light_cops")
    source = legacy.createNode("color", "color")
    source.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
    source.parmTuple("color").set((0.2, 0.4, 0.6, 1))
    f.light.parm("cycles_light_texture").set("op:" + source.path())
    render("Legacy COP color is owned across the worker boundary", (0.2, 0.4, 0.6))
    captures = _native.cop_capture_stats(entry.session._handle)["captures"]
    f.camera.parm("tx").set(0.1)
    render("Camera edits retain the owned light raster", (0.2, 0.4, 0.6))
    assert _native.cop_capture_stats(entry.session._handle)["captures"] == captures
    source.parm("colorr").set(0.7)
    render("Upstream COP light color edit replaces only its owned raster", (0.7, 0.4, 0.6))
    modern = hou.node("/img").createNode("copnet", "light_copernicus")
    modern.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
    constant = modern.createNode("constant", "color")
    constant.parm("signature").set("f3")
    constant.parmTuple("f3").set((0.5, 0.25, 0.125))
    f.light.parm("cycles_light_texture").set("op:" + constant.path())
    render("COPernicus light raster uses its actual color channels", (0.5, 0.25, 0.125))
    f.rop.parm("allowmotionblur").set(1)
    render("Static COP light does not invent shutter motion warnings", (0.5, 0.25, 0.125))
    constant.parmTuple("f3")[0].setExpression("$F*.1", hou.exprLanguage.Hscript)
    hou.setFrame(2)
    # Lights do not blur over the shutter (as in Blender): no diagnostic, requested-frame pixels.
    render("Animated COP light under motion blur uses requested-frame pixels without a warning", (0.2, 0.25, 0.125))
    constant.parmTuple("f3")[0].deleteAllKeyframes()
    constant.parmTuple("f3")[0].set(0.5)
    render("Static COP light after animation restores its raster", (0.5, 0.25, 0.125))
    f.light.parm("cycles_light_texture").set("op:/img/missing_light_image")
    render("Missing COP light keeps a visible untextured emitter", (1, 1, 1), "light.texture_unavailable")
    f.light.parm("cycles_light_texture").set("op:" + constant.path())
    render("Repairing COP path restores the light texture", (0.5, 0.25, 0.125))
    # Native hlight area-map compatibility uses the same graph path and optional tab.
    f.light.parm("light_enable").set(0)
    native = f.scene.createNode("hlight::2.0", "native_textured_area")
    light_properties.install(native)
    native.setParms(
        dict(
            light_type="grid",
            light_intensity=math.pi,
            light_exposure=0,
            normalizearea=0,
            singlesided=1,
            ry=180,
            areamap=str(f.texture),
            cycles_light_texture_colorspace="data",
            light_contribprimary=1,
        )
    )
    native.parmTuple("areasize").set((2, 2))
    render("Native Houdini Area Map uses the supported texture adapter", (0.25, 0.5, 0.75))
    report.update(result="passed", cop_stats=_native.cop_capture_stats(entry.session._handle))
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (f.dest / "texture-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
