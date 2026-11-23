# SPDX-License-Identifier: Apache-2.0
"""File/Bake GSplats, independent SH camera oracle, SOP/OBJ/packed transforms and IPR edits."""

import argparse
import json
import math
import os
from pathlib import Path

import hou
import numpy as np
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from cycles_houdini import native_scene
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--out", default="")
args = parser.parse_args()
dest = Path(args.out or Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]).parent / "qa/native-gsplats")
dest.mkdir(parents=True, exist_ok=True)
# The independent numeric oracle below is in linear Rec.709. Pin the test
# process's working role explicitly rather than assume the artist's profile.
config = OCIO.Config.CreateFromStream(OCIO.GetCurrentConfig().serialize())
config.setWorkingDir(OCIO.GetCurrentConfig().getWorkingDir())
assert config.getColorSpace("Linear Rec.709 (sRGB)") is not None
config.setRole(OCIO.ROLE_SCENE_LINEAR, "Linear Rec.709 (sRGB)")
profile = dest / "rec709.ocio"
profile.write_text(config.serialize())
hou.putenv("OCIO", str(profile))
hou.Color.reloadOCIO()
ch.synchronize_color_configuration()
hou.setFps(24)
hou.setFrame(1)
root = hou.node("/obj").createNode("subnet", "blender53_gsplats")
geo = root.createNode("geo", "splats", run_init_scripts=False)
# Ordinary File SOP receives an actual PLY, rather than a test-only fake
# PointData constructor. Only the red Y(1,0) coefficient is nonzero.
properties = [("x", 0.0), ("y", 0.0), ("z", 0.0)]
properties += [(f"f_dc_{i}", v) for i, v in enumerate((0.3, 0.1, -0.1))]
properties += [(f"rot_{i}", v) for i, v in enumerate((1.0, 0.0, 0.0, 0.0))]
properties += [(f"scale_{i}", math.log(v)) for i, v in enumerate((0.3, 0.2, 0.1))]
properties += [("opacity", math.log(4.0))]
properties += [(f"f_rest_{i}", 0.35 if i == 1 else 0.0) for i in range(45)]
ply = dest / "single-splat.ply"
ply.write_text(
    "ply\nformat ascii 1.0\nelement vertex 1\n"
    + "".join("property float " + n + "\n" for n, _ in properties)
    + "end_header\n"
    + " ".join(str(v) for _, v in properties)
    + "\n"
)
file = geo.createNode("file")
file.parm("file").set(str(ply))
bake = geo.createNode("bakegsplat")
bake.setInput(0, file)
bake.setParms(dict(linearize=1, sphcoeff=1))
animate = geo.createNode("attribwrangle", "animate_orientation")
animate.setInput(0, bake)
animate.addSpareParmTuple(hou.FloatParmTemplate("rate", "Degrees per Second", 1, default_value=(0.0,)))
animate.addSpareParmTuple(hou.IntParmTemplate("srgb", "sRGB Cd", 1, default_value=(0,)))
animate.addSpareParmTuple(hou.FloatParmTemplate("size", "Uniform Scale", 1, default_value=(1.0,)))
animate.parm("snippet").set(
    'p@orient=quaternion(radians(@Time*chf("rate")), {0,1,0}); f@pscale=chf("size"); setdetailattrib(0,"cycles_gsplat_srgb",chi("srgb"),"set");'
)
transform = geo.createNode("xform", "rotate_sop")
transform.setInput(0, animate)
transform.parm("ry").set(60)
pack = geo.createNode("pack")
pack.setInput(0, animate)
packed_transform = geo.createNode("xform", "rotate_packed")
packed_transform.setInput(0, pack)
packed_transform.parm("ry").set(60)
choice = geo.createNode("switch")
choice.setInput(0, animate)
choice.setInput(1, transform)
choice.setInput(2, packed_transform)
targets = geo.createNode("attribwrangle", "instance_targets")
targets.parm("class").set(0)
targets.addSpareParmTuple(hou.IntParmTemplate("count", "Instances", 1, default_value=(2,)))
targets.parm("snippet").set('for(int i=0;i<chi("count");++i) addpoint(0,set(i*0.55,0,0));')
copies = geo.createNode("copytopoints::2.0", "packed_pair")
copies.setInput(0, pack)
copies.setInput(1, targets)
choice.setInput(3, copies)
out = geo.createNode("null", "OUT")
out.setInput(0, choice)
out.setDisplayFlag(True)
out.setRenderFlag(True)
cam = root.createNode("cam")
cam.setParms(dict(tz=5, projection=1, orthowidth=3, shutter=0.5))
rop = hou.node("/out").createNode("cycles_ipr", "gsplat_contracts")
set_devices(rop, args.device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=root.path(),
        vobject="splats",
        lights="",
        camera=cam.path(),
        resx=96,
        resy=96,
        samples=256,
        ipr_samples=256,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        film_exposure=1,
        use_texture_cache=0,
        render_threads=2,
        use_material_assignments=1,
        allowmotionblur=0,
        geometry_motion="deformation",
        motion_samples=3,
    )
)
rop.parmTuple("background").set((0, 0, 0))
results = []


def srgb_to_linear(x):
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def expected_ratio(z):
    # Independent textbook L0/L1 evaluation before display conversion. The
    # engine's 8-bit higher-band packing allows a small explicit tolerance.
    red = srgb_to_linear(0.5 + 0.282094792 * 0.3 + 0.488602512 * 0.35 * z)
    green = srgb_to_linear(0.5 + 0.282094792 * 0.1)
    return red / green


with ch.Session(
    device=args.device, width=96, height=96, samples=256, threads=2, collect_diagnostics=True, use_texture_cache=False
) as session:

    def render(label, expected_z, warning=False):
        update = native_scene.capture(rop)
        frame = session.wait(session.apply(update))
        status = session.status()
        assert not status["error"], status
        image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(96, 96, 4).copy()
        np.save(dest / (label + ".npy"), image)
        current = out.geometry()
        (dest / (label + "-source.json")).write_text(
            json.dumps(
                dict(
                    points=[{a.name(): p.attribValue(a) for a in current.pointAttribs()} for p in current.points()],
                    status=status,
                ),
                indent=2,
            )
        )
        assert np.isfinite(image).all() and image[:, :, :3].sum() > 1, (label, status)
        rgb = image[:, :, :3].sum(axis=(0, 1))
        ratio = float(rgb[0] / rgb[1])
        expected = expected_ratio(expected_z)
        assert abs(ratio - expected) < 0.07, (label, ratio, expected, status["source_diagnostics"])
        codes = [d["code"] for d in status["source_diagnostics"]]
        assert ("geometry.gsplat_sh_motion" in codes) == warning, (label, codes)
        np.save(dest / (label + ".npy"), image)
        results.append(
            dict(
                name=label,
                ratio=ratio,
                expected=expected,
                energy=rgb.tolist(),
                diagnostics=codes,
                geometry_conversions=status["geometry_conversions"],
                packed_instances=status["packed_instances"],
            )
        )
        return image, status

    base, _ = render("file-bake-dc-sh", -1)
    # SOP rotation must rotate higher bands by orient * inverse(restorient).
    # Avoid the upstream emission closure's exact-grazing (Ng dot wi == 0)
    # zero-PDF case; a 60-degree view has a known nonzero SH response.
    choice.parm("input").set(1)
    sop, _ = render("sop-restorient-rotation", -0.5)
    choice.parm("input").set(0)
    geo.parm("ry").set(60)
    obj, _ = render("obj-rotation", -0.5)
    geo.parm("ry").set(0)
    choice.parm("input").set(2)
    packed, _ = render("packed-rotation", -0.5)
    # Moving the camera through the inverse rotation is the same SH viewpoint.
    choice.parm("input").set(0)
    _, restored_status = render("restore-direct-source", -1)
    cam.setParms(dict(tx=5 * math.sin(math.pi / 3), tz=2.5, ry=60))
    _, camera_status = render("camera-rotation", -0.5)
    assert camera_status["geometry_conversions"] == restored_status["geometry_conversions"], (
        "Camera edit recaptured/rotated Gaussian data"
    )
    cam.setParms(dict(tx=0, tz=5, ry=0))
    # A deformation sample changing the SH frame is an actual upstream limit;
    # the accepted session must expose the warning rather than claim SH blur.
    animate.parm("rate").set(180)
    rop.parm("allowmotionblur").set(1)
    render("deformation-sh-warning", -1, warning=True)
    animate.parm("rate").set(0)
    rop.parm("allowmotionblur").set(0)
    render("warning-clears-after-edit", -1)
    targets.parm("count").set(1)
    choice.parm("input").set(3)
    _, single_packed = render("shared-packed-single", -1)
    targets.parm("count").set(2)
    pair, pair_status = render("shared-packed-pair", -1)
    assert pair_status["geometry_conversions"] == single_packed["geometry_conversions"], (
        "Adding an instance reconverted the unchanged prototype"
    )
    assert pair[:, :, :3].sum() > base[:, :, :3].sum() * 1.3, (pair[:, :, :3].sum(), base[:, :, :3].sum(), pair_status)
    _, cached_pair = render("shared-packed-pair-reuse", -1)
    assert cached_pair["geometry_conversions"] == pair_status["geometry_conversions"], (
        "Unchanged packed pair reconverted geometry"
    )
    choice.parm("input").set(0)
    bake.parm("sphcoeff").set(0)
    render("scene-linear-dc-only", 0)
    targets.parm("count").set(1)
    choice.parm("input").set(3)
    _, dc_single = render("dc-only-shared-packed-single", 0)
    targets.parm("count").set(2)
    _, dc_pair = render("dc-only-shared-packed-pair", 0)
    assert dc_single["geometry_conversions"] == dc_pair["geometry_conversions"], (
        "DC-only instance addition reconverted prototype"
    )
    choice.parm("input").set(0)
    bake.parm("linearize").set(0)
    animate.parm("srgb").set(1)
    render("srgb-dc-explicit-encoding", 0)
    bake.setParms(dict(sphcoeff=1, linearize=1))
    animate.parm("srgb").set(0)
    animate.parm("size").set(0.5)
    half, _ = render("pscale-multiplies-axis-scale", -1)
    assert abs(float(half[:, :, :3].sum() / base[:, :, :3].sum()) - 0.25) < 0.03
    animate.parm("size").set(1)
    hou.hipFile.save(str(dest / "file-bake-gsplats.hip"))

(dest / "report.json").write_text(json.dumps(dict(result="passed", device=args.device, checks=results), indent=2))
print(json.dumps(dict(result="passed", device=args.device, checks=len(results))))
