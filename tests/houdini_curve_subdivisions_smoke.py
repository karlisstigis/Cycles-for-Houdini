# SPDX-License-Identifier: Apache-2.0
"""ROP Curve Subdivisions (Cycles SceneParams::hair_subdivisions, Blender's setting).

A coarse polyline (90 degree steps on a circle) renders as thin Rounded Ribbons. Cycles splits
each Catmull-Rom segment into 2^n straight pieces (the ribbon kernel on GPU) or 2^n + 1
(Embree's tessellation rate on the CPU), so a quarter of the way along an interior segment
the ribbon lies on the curve for 4 and visibly inside it for 0.
3D Curves and Linear 3D Curves do not change. A change in a live
interactive session recreates the session and shows the new image. A ROP whose asset
lacks the parameter (an asset older than the setting) renders with 2, interactively and
to disk."""

import argparse
import json
import math
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry, object_properties
from cycles_houdini.device_selection import set_devices as set_rop_devices
from support.paths import artifacts_root

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
cpu = device.upper().startswith("CPU")
dest = artifacts_root() / "stage2/hair/curve-subdivisions"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, renders=[])

RADIUS, WIDTH, ORTHO, RES = 1.0, 0.04, 3.0, 320
ANGLES = [math.radians(-135 + 90 * i) for i in range(5)]  # four coarse segments
POINTS = [(RADIUS * math.cos(a), RADIUS * math.sin(a), 0.0) for a in ANGLES]

obj = hou.node("/obj").createNode("geo", "subdivision_curves", run_init_scripts=False)
sop = obj.createNode("python", "coarse_arc")
sop.parm("python").set(
    "import hou\ng = hou.pwd().geometry()\ng.addAttrib(hou.attribType.Point, 'width', %r)\n"
    "curve = g.createPolygon(is_closed=False)\n"
    "for p in %r:\n    point = g.createPoint(); point.setPosition(p); curve.addVertex(point)\n" % (WIDTH, POINTS)
)
sop.setDisplayFlag(True)
sop.setRenderFlag(True)
object_properties.install(obj)
material = ch.create_material(name="subdivision_emission", shader="emission")
material.node("surface_shader").parmTuple("color").set((1, 1, 1))
material.node("surface_shader").parm("strength").set(1)
obj.parm("shop_materialpath").set(material.path())
camera = hou.node("/obj").createNode("cam", "subdivision_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=ORTHO, resx=RES, resy=RES, aspect=1))
rop = hou.node("/out").createNode("cycles_ipr", "curve_subdivisions")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        vobject=obj.path(),
        camera=camera.path(),
        resx=RES,
        resy=RES,
        samples=16,
        ipr_samples=16,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
assert rop.parm("curve_subdivisions").eval() == 2, "default must be Blender's 2"
template = rop.parm("curve_subdivisions").parmTemplate()
assert (template.label(), template.minValue(), template.maxValue()) == ("Curve Subdivisions", 0, 24)
assert rop.parm("curve_segments").parmTemplate().label() == "Bezier/NURBS Segments"


def catmull_rom(i, t):
    p0, p1, p2, p3 = (np.array(POINTS[i + k][:2]) for k in (-1, 0, 1, 2))
    return 0.5 * (2 * p1 + (p2 - p0) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t + (3 * p1 - p0 - 3 * p2 + p3) * t**3)


def pieces(count):
    """Straight pieces per segment for Curve Subdivisions count on this device."""
    return min(16, 2**count) + (1 if cpu else 0)


def piecewise(i, t, n):
    k = min(n - 1, int(t * n))
    a, b = catmull_rom(i, k / n), catmull_rom(i, (k + 1) / n)
    return a + (t * n - k) * (b - a)


INTERIOR = (1, 2)  # segments with a neighbour key on both sides
T = 0.25


def value(image, position):
    height, width = image.shape[:2]
    x = (position[0] / ORTHO + 0.5) * width - 0.5
    y = (position[1] / ORTHO * width / height + 0.5) * height - 0.5
    return float(image[int(round(y)), int(round(x))].max())


def probe(image, points):
    return float(np.mean([value(image, point) for point in points]))


def classify(image, count=0):
    """Brightness on the true curve and on the expected piecewise ribbon for count."""
    return (
        probe(image, [catmull_rom(i, T) for i in INTERIOR]),
        probe(image, [piecewise(i, T, pieces(count)) for i in INTERIOR]),
    )


def frame_pixels(frame):
    plane = frame["planes"]["combined"]
    data = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])
    return data[:, :, :3].copy()


def render(label, entry=None):
    entry = registry.for_rop(rop, native_scene.configuration(rop)) if entry is None else entry
    start = time.monotonic()
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    status = entry.session.status()
    assert status["state"] != "failed" and status["curves"] == 1, status
    image = frame_pixels(frame)
    count = rop.parm("curve_subdivisions").eval() if rop.parm("curve_subdivisions") else 2
    curve, ribbon = classify(image, count)
    report["renders"].append(
        dict(
            label=label,
            curve=curve,
            expected_ribbon=ribbon,
            seconds=round(time.monotonic() - start, 3),
            pid=status.get("renderer_pid"),
        )
    )
    return image


def shape(name):
    obj.parm("cycles_curve_shape").set(name)


def subdivisions(value):
    rop.parm("curve_subdivisions").set(value)


try:
    # Rounded Ribbons: 0 renders inside the curve (1 or 2 straight pieces), 2 and 4 on it.
    separation = min(np.linalg.norm(catmull_rom(i, T) - piecewise(i, T, pieces(0))) for i in INTERIOR)
    assert separation > 1.5 * WIDTH, separation
    shape("ribbon")
    images = {}
    for count in (2, 0, 4):
        subdivisions(count)
        images[count] = render("ribbon %d" % count)
        curve, ribbon = classify(images[count], count)
        assert ribbon > 0.5, report["renders"]
        if count == 0:
            assert curve < 0.1, report["renders"]
        else:
            assert curve > 0.5 and classify(images[count], 0)[1] < 0.1, report["renders"]
    report["ribbon_mean_difference_0_4"] = float(np.abs(images[0] - images[4]).mean())
    assert report["ribbon_mean_difference_0_4"] > 1e-4, report

    # 3D Curves and Linear 3D Curves do not use it (CPU and OptiX; measured, not assumed).
    for name in ("linear", "thick"):
        shape(name)
        subdivisions(0)
        low = render(name + " 0")
        subdivisions(4)
        high = render(name + " 4")
        difference = float(np.abs(low - high).max())
        report[name + "_max_difference_0_4"] = difference
        assert difference < 1e-5, (name, difference)

    # Live change: the interactive session is recreated and shows the new value at once.
    shape("ribbon")
    subdivisions(2)
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    render("live before", entry)
    before = entry.session
    start = time.monotonic()
    subdivisions(0)
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    image = render("live after", entry)
    report["live_change_seconds"] = time.monotonic() - start
    assert entry.session is not before, "a changed SceneParams value must recreate the session"
    on_curve, ribbon = classify(image, 0)
    assert ribbon > 0.5 and on_curve < 0.1, report["renders"]
    subdivisions(4)
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    on_curve, ribbon = classify(render("live back", entry), 0)
    assert on_curve > 0.5 and ribbon < 0.1, report["renders"]
    registry.close_all()

    # Final render to disk.
    subdivisions(0)
    rop.setParms(dict(picture=str(dest / "final.$F4.exr"), trange=0))
    rop.render()
    final = oiio.ImageBuf(str(dest / ("final.%04d.exr" % int(hou.frame()))))
    assert not final.has_error, final.geterror()
    on_curve, ribbon = classify(np.flipud(final.get_pixels()[:, :, :3]), 0)
    report["final_0"] = dict(curve=on_curve, expected_ribbon=ribbon)
    assert ribbon > 0.5 and on_curve < 0.1, report["final_0"]

    # An asset older than the setting: the ROP lacks the parameter and renders with 2.
    rop_type = rop.type()
    current = rop_type.definition()
    old_path = dest / "cycles_ipr_without_curve_subdivisions.hda"
    if old_path.exists():
        old_path.unlink()
    current.copyToHDAFile(str(old_path))
    hou.hda.installFile(str(old_path), force_use_assets=True)
    try:
        old = next(d for d in hou.hda.definitionsInFile(str(old_path)) if d.nodeTypeName() == rop_type.name())
        group = old.parmTemplateGroup()
        group.remove("curve_subdivisions")
        old.setParmTemplateGroup(group)
        old.setIsPreferred(True)
        assert rop.parm("curve_subdivisions") is None, "the older asset must lack the parameter"
        config = native_scene.configuration(rop)
        # Curve Subdivisions is followed by the mesh refit setting in the configuration.
        assert config[-2] == 2, config
        on_curve, ribbon = classify(render("old asset interactive"), 0)
        assert on_curve > 0.5 and ribbon < 0.1, report["renders"]
        registry.close_all()
        rop.setParms(dict(picture=str(dest / "old-asset.$F4.exr")))
        rop.render()
        final = oiio.ImageBuf(str(dest / ("old-asset.%04d.exr" % int(hou.frame()))))
        assert not final.has_error, final.geterror()
        on_curve, ribbon = classify(np.flipud(final.get_pixels()[:, :, :3]), 0)
        report["old_asset_final"] = dict(curve=on_curve, ribbon_for_0=ribbon)
        assert on_curve > 0.5 and ribbon < 0.1, report["old_asset_final"]
    finally:
        current.setIsPreferred(True)
        hou.hda.uninstallFile(str(old_path))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / ("report-%s.json" % ("cpu" if cpu else "gpu"))).write_text(json.dumps(report, indent=2))
print("CURVE_SUBDIVISIONS_REPORT " + json.dumps(report), flush=True)
