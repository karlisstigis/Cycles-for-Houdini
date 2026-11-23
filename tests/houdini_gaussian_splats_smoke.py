# SPDX-License-Identifier: Apache-2.0
"""Native Gaussian splats: analytic images, cooked Bake GSplats, edits and devices."""

import argparse
import json
import math
import os
from pathlib import Path

import hou
import numpy as np
import PyOpenColorIO as ocio
import cycles_houdini as ch
from native_test_runtime import runtime_context

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--device", default="CPU")
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=True)
context = runtime_context()
caps = ch.capabilities()
assert caps["gaussian_splats"] and caps["image_iris"], caps
device = a.device
if device == "OPTIX":
    device = next(d["id"] for d in ch.devices() if d["backend"] == "OPTIX")
elif device != "CPU":
    assert any(d["id"] == device for d in ch.devices()), ch.devices()

obj = hou.node("/obj").createNode("geo", "gaussian_splat_test", run_init_scripts=False)
source = obj.createNode("python", "raw_3dgs")
camera = hou.node("/obj").createNode("cam", "gaussian_splat_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=2.0))
W = H = 96
C0 = 0.28209479177387814
SRGB = np.array((0.7, 0.5, 0.3))
ocio_config = ocio.GetCurrentConfig()
rec709_space = (
    "lin_rec709_scene"
    if ocio_config.getColorSpace("lin_rec709_scene")
    else next(n for n in ocio_config.getColorSpaceNames() if n.startswith("Linear Rec.709"))
)
reference_processor = ocio_config.getProcessor(rec709_space, ocio.ROLE_SCENE_LINEAR).getDefaultCPUProcessor()


def linear(c):
    c = np.asarray(c)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def working(c):
    return np.asarray(reference_processor.applyRGB([float(v) for v in c]))


def author(*, opacity=0.75, scale=(0.30, 0.10, 0.03), angle=0, count=1, sh=False):
    # Original PLY data, deliberately expanded component attributes.
    config = dict(opacity=opacity, scale=scale, angle=angle, count=count, sh=sh)
    source.parm("python").set(
        """import hou, math
g = hou.pwd().geometry()
c = """
        + repr(config)
        + """
for name, count in [('f_dc',3),('scale',3),('rot',4),('f_rest',45)]:
    for i in range(count): g.addAttrib(hou.attribType.Point, name+'_'+str(i), 0.0)
g.addAttrib(hou.attribType.Point, 'opacity', 0.0)
g.addAttrib(hou.attribType.Point, 'id', 0)
for j in range(c['count']):
    p=g.createPoint();p.setPosition((0,0,-j*.15));p.setAttribValue('id',j+1)
    p.setAttribValue('opacity',math.log(c['opacity']/(1-c['opacity'])))
    for i,v in enumerate((.7,.5,.3)):p.setAttribValue('f_dc_'+str(i),(v-.5)/.28209479177387814)
    for i,v in enumerate(c['scale']):p.setAttribValue('scale_'+str(i),math.log(v))
    q=(math.cos(c['angle']/2),0,0,math.sin(c['angle']/2))
    for i,v in enumerate(q):p.setAttribValue('rot_'+str(i),float(v))
    if c['sh']:p.setAttribValue('f_rest_2',.6)
"""
    )
    source.setDisplayFlag(True)
    source.setRenderFlag(True)
    try:
        source.cook(force=True)
    except hou.OperationFailed as error:
        raise AssertionError(source.errors()) from error
    assert not source.errors(), source.errors()


def rgba(frame):
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(H, W, plane["channels"]).copy()
    assert image.shape[2] == 4 and np.isfinite(image).all()
    return image


def analytic(scale=(0.30, 0.10), opacity=0.75, angle=0, count=1):
    y, x = np.mgrid[:H, :W]
    x = (x + 0.5 - W / 2) * 2 / W
    y = (y + 0.5 - H / 2) * 2 / H
    u = math.cos(angle) * x + math.sin(angle) * y
    v = -math.sin(angle) * x + math.cos(angle) * y
    alpha = opacity * np.exp(-0.5 * ((u / scale[0]) ** 2 + (v / scale[1]) ** 2))
    return 1 - (1 - alpha) ** count


rows = []
with ch.Session(
    device=device, width=W, height=H, samples=1024, threads=4, execution="worker", denoising={"enabled": False}
) as session:

    def render(sop=source, mode="auto", label="frame", **extra):
        try:
            sop.cook()
        except hou.OperationFailed as error:
            raise AssertionError(sop.errors()) from error
        request = ch.geometry_request(obj, sop, point_render_mode=mode)
        update = dict(
            geometry=[request],
            camera_path=camera.path(),
            background=(0, 0, 0),
            transparent_background=True,
            motion={"enabled": False},
            rendering=dict(adaptive_sampling=False, pixel_filter="box", filter_width=1, seed=17, samples=1024),
        )
        update.update(extra)
        frame = session.wait(session.apply(update), timeout=180)
        status = session.status()
        assert not status["error"] and status["render_device_ids"] == [device], status
        assert not [
            d
            for d in status.get("source_diagnostics", [])
            if d.get("severity") == "error" or d.get("code") == "geometry.cook_failed"
        ], status
        image = rgba(frame)
        session.write_exr(a.output / (label + ".exr"))
        return image, status

    def profile(image, expected, label):
        alpha_error = np.abs(image[:, :, 3] - expected)
        rgb_error = np.abs(image[:, :, :3] - expected[:, :, None] * working(linear(SRGB)))
        # This compares sampled pixels to an independently specified Gaussian,
        # rather than comparing the CPU and GPU implementations to each other.
        assert float(np.sqrt(np.mean(alpha_error**2))) < 0.025, (label, float(alpha_error.max()))
        assert float(np.sqrt(np.mean(rgb_error**2))) < 0.018, (label, float(rgb_error.max()))
        rows.append(
            dict(
                label=label,
                alpha_rmse=float(np.sqrt(np.mean(alpha_error**2))),
                rgb_rmse=float(np.sqrt(np.mean(rgb_error**2))),
            )
        )

    author()
    initial, initial_status = render(label="raw-auto")
    profile(initial, analytic(), "raw PLY auto detection")
    assert initial_status["points"] == 1
    grouped = obj.createNode("python", "grouped_3dgs_attributes")
    grouped.setInput(0, source)
    grouped.parm("python").set("""import hou
g=hou.pwd().geometry()
for name,count in [('f_dc',3),('scale',3),('rot',4),('f_rest',45)]:
    g.addAttrib(hou.attribType.Point,name,tuple(0. for i in range(count)))
    for p in g.points():p.setAttribValue(name,tuple(p.attribValue(name+'_'+str(i)) for i in range(count)))
    for i in range(count):g.findPointAttrib(name+'_'+str(i)).destroy()
""")
    grouped_image, _ = render(grouped, label="grouped-raw")
    profile(grouped_image, analytic(), "grouped raw PLY attributes")
    author(angle=math.pi / 2)
    rotated, _ = render(label="rotated")
    profile(rotated, analytic(angle=math.pi / 2), "orientation edit")
    assert float(np.mean(np.abs(rotated - initial))) > 0.015
    author(opacity=0.35, scale=(0.18, 0.12, 0.03))
    edited, _ = render(label="opacity-scale-edit")
    profile(edited, analytic(scale=(0.18, 0.12), opacity=0.35), "opacity and scale edit")
    author(count=2)
    overlap, _ = render(label="overlap")
    profile(overlap, analytic(count=2), "two overlapping splats")
    author()
    bake = obj.createNode("bakegsplat", "houdini_bake")
    bake.setInput(0, source)
    bake.parm("sphcoeff").set(1)
    bake.cook(force=True)
    assert not bake.errors(), bake.errors()
    prepared, _ = render(bake, label="bake-gsplats")
    profile(prepared, analytic(), "Houdini Bake GSplats attributes")
    assert float(np.sqrt(np.mean((prepared - initial) ** 2))) < 0.025
    prepared_edit = obj.createNode("python", "prepared_scale_and_color")
    prepared_edit.setInput(0, bake)
    prepared_edit.parm("python").set(
        "import hou\ng=hou.pwd().geometry()\ng.addAttrib(hou.attribType.Point,'pscale',.6)\n"
    )
    scaled, _ = render(prepared_edit, label="prepared-pscale")
    profile(scaled, analytic(scale=(0.18, 0.06)), "prepared pscale multiplier")
    prepared_edit.parm("python").set(
        "import hou\ng=hou.pwd().geometry()\nfor name in ('GS_SPH_R','GS_SPH_G','GS_SPH_B','restorient'):g.findPointAttrib(name).destroy()\n"
    )
    cd_only, _ = render(prepared_edit, label="prepared-cd-only")
    profile(cd_only, analytic(), "prepared scene-linear Cd without harmonics")

    # Restore source and explicitly toggle representation in the same session.
    splat, _ = render(mode="gaussian_splats", label="explicit-splats")
    profile(splat, analytic(), "explicit mode")
    sphere, _ = render(
        mode="spheres", label="sphere-control", materials=[dict(key="default", kind="emission", color=(1, 1, 1))]
    )
    assert float(np.mean(np.abs(sphere - splat))) > 0.01
    restored, _ = render(label="restored-splats")
    profile(restored, analytic(), "return from spheres")
    for name, corruption in (
        ("NaN opacity", "g.points()[0].setAttribValue('opacity',float('nan'))"),
        ("zero quaternion", "[g.points()[0].setAttribValue('rot_'+str(i),0.) for i in range(4)]"),
        ("incomplete DC fields", "g.findPointAttrib('f_dc_2').destroy()"),
    ):
        author()
        source.parm("python").set(source.parm("python").eval() + "\n" + corruption + "\n")
        try:
            session.wait(session.apply(dict(geometry=[ch.geometry_request(obj, source)])), timeout=60)
        except (ValueError, RuntimeError) as error:
            assert "Gaussian" in str(error) or "splat" in str(error), str(error)
        else:
            status = session.status()
            assert status["points"] == 0 and any(
                "Gaussian" in d["message"] or "splat" in d["message"] for d in status["source_diagnostics"]
            ), (name, status["source_diagnostics"])
    rows.append(dict(label="malformed native splat inputs rejected", cases=3))

    # Compare view-dependent SH color to the independent L1 basis formula.
    author(scale=(0.16, 0.16, 0.02), sh=True)
    camera.parm("projection").set(0)
    view_colors = []
    for x in (-2.0, 2.0):
        camera.setParms(dict(tx=x, ry=math.degrees(math.atan2(x, 5))))
        image, _ = render(label="sh-left" if x < 0 else "sh-right")
        patch = image[H // 2 - 2 : H // 2 + 2, W // 2 - 2 : W // 2 + 2]
        observed = patch[:, :, :3].sum(axis=(0, 1)) / patch[:, :, 3].sum()
        direction_x = -x / math.sqrt(x * x + 25)
        expected_srgb = SRGB.copy()
        expected_srgb[0] += -0.4886025119029199 * direction_x * 0.6
        expected = working(linear(expected_srgb))
        assert float(np.max(np.abs(observed - expected))) < 0.055, (x, observed, expected)
        view_colors.append(observed.tolist())
    assert abs(view_colors[0][0] - view_colors[1][0]) > 0.08
    rows.append(dict(label="view-dependent spherical harmonics", colors=view_colors))

    # Public controls are installed through the actual object settings module.
    from cycles_houdini import object_properties

    object_properties.install(obj)
    assert obj.parm("cycles_point_render_mode") is not None
    obj.parm("cycles_point_render_mode").set("gaussian_splats")
    assert object_properties.component_settings(obj, hou.time())["point_render_mode"] == "gaussian_splats"

# Exercise the ordinary Houdini ROP, its default material fallback and the
# installed object controls, independently of the explicit Session requests.
import OpenImageIO as oiio
from cycles_houdini.device_selection import set_devices

author()
camera.setParms(dict(tx=0, ry=0, projection=1))
rop = hou.node("/out").createNode("cycles_ipr", "gaussian_splat_rop")
set_devices(rop, device)
destination = a.output / "native-rop.exr"
rop.setParms(
    dict(
        source_mode="sop",
        soppath=source.path(),
        camera=camera.path(),
        resx=W,
        resy=H,
        samples=1024,
        adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        transparent_background=1,
        pixel_filter="box",
        filter_width=1,
        picture=str(destination.resolve()),
    )
)
rop.parmTuple("background").set((0, 0, 0))
rop.render(frame_range=(hou.frame(), hou.frame()))
inp = oiio.ImageInput.open(str(destination))
assert inp is not None
names = list(inp.spec().channelnames)
native_image = np.array(inp.read_image(format=oiio.FLOAT))
inp.close()
native_rgba = native_image[:, :, [names.index(c) for c in ("R", "G", "B", "A")]]
profile(native_rgba, analytic(), "native Houdini ROP and default material")

report = dict(
    result="passed",
    device=device,
    capabilities=caps,
    runtime=context,
    working_space=ocio_config.getColorSpace(ocio.ROLE_SCENE_LINEAR).getName(),
    checks=rows,
)
(a.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
