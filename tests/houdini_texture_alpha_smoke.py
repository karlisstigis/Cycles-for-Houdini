# SPDX-License-Identifier: Apache-2.0
"""Texture input color conversion must preserve separately authored opacity."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/color/transparency/texture-alpha"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_env = hou.getenv("OCIO")
config = OCIO.Config.CreateFromStream(original.serialize())
config.setWorkingDir(original.getWorkingDir())
input_space = OCIO.ColorSpace(name="Native Texture Alpha Input")
input_space.setTransform(
    OCIO.MatrixTransform(matrix=[0.5, 0, 0, 0, 0, 0.75, 0, 0, 0, 0, 0.25, 0, 0, 0, 0, 0.125]),
    OCIO.COLORSPACE_DIR_TO_REFERENCE,
)
config.addColorSpace(input_space)
config.validate()
path = destination / "input-alpha.ocio"
path.write_text(config.serialize(), encoding="utf-8")
hou.putenv("OCIO", str(path))
hou.Color.reloadOCIO()
ch.synchronize_color_configuration()
config = OCIO.GetCurrentConfig()
rgb = [0.6, 0.3, 0.1]
expected_rgb = config.getProcessor(input_space.getName(), OCIO.ROLE_SCENE_LINEAR).getDefaultCPUProcessor().applyRGB(rgb)
obj = hou.node("/obj").createNode("geo", "texture_alpha_geometry", run_init_scripts=False)
sop = obj.createNode("python", "plane")
sop.parm("python").set("""import hou
g=hou.pwd().geometry();uv=g.addAttrib(hou.attribType.Vertex,'uv',(0.,0.,0.));f=g.createPolygon()
for p,t in zip(((-3.,-3.,0.),(3.,-3.,0.),(3.,3.,0.),(-3.,3.,0.)),((0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.))):
    point=g.createPoint();point.setPosition(p);vertex=f.addVertex(point);vertex.setAttribValue(uv,t)
""")
camera = hou.node("/obj").createNode("cam", "texture_alpha_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
builder = ch.create_material(name="texture_alpha", shader="emission")
image = builder.createNode("cycles::image_texture::1", "image")
image.parm("colorspace").set(input_space.getName())
combine = builder.createNode("cycles::combine_xyz::1", "alpha_rgb")
for component in ("x", "y", "z"):
    combine.setNamedInput(component, image, "alpha")
emit = builder.node("surface_shader")
emit.parm("strength").set(1)
reports = []
try:
    for device in ("CPU", ch.default_device()):
        for cache in (False, True):
            checks = []
            with ch.Session(
                device=device, width=32, height=32, samples=8, use_texture_cache=cache, auto_texture_cache=cache
            ) as session:
                for mode in ("associated", "unassociated", "channel_packed"):
                    image.parm("alpha_type").set(mode)
                    for alpha in (0.0, 0.375, 1.0):
                        filename = destination / f"{mode}-{alpha}.exr"
                        payload = rgb if mode != "associated" or alpha == 0 else [v * alpha for v in rgb]
                        output = OIIO.ImageOutput.create(str(filename))
                        spec = OIIO.ImageSpec(256, 256, 4, OIIO.FLOAT)
                        spec.alpha_channel = 3
                        spec.attribute("oiio:UnassociatedAlpha", int(mode == "unassociated"))
                        assert output.open(str(filename), spec)
                        assert output.write_image(
                            np.broadcast_to(np.asarray([*payload, alpha], np.float32), (256, 256, 4)).copy()
                        )
                        assert output.close()
                        image.parm("filename").set(str(filename))
                        measured = {}
                        # Pinned Cycles unassociates Color when Alpha is used.
                        # Exercise both graph states explicitly; leaving Alpha
                        # unused deliberately retains premultiplication.
                        # Straight inputs are premultiplied on load, including
                        # at zero coverage. Associated inputs can already carry
                        # additive RGB at alpha zero and retain that energy.
                        associated_rgb = (
                            expected_rgb
                            if mode == "channel_packed" or (mode == "associated" and alpha == 0)
                            else [v * alpha for v in expected_rgb]
                        )
                        for channels, source, socket, expected in (
                            ("alpha", combine, "vector", [alpha] * 3),
                            ("color_with_alpha", image, "color", [v * alpha for v in expected_rgb]),
                            ("color_only", image, "color", associated_rgb),
                        ):
                            emit.setNamedInput("color", source, socket)
                            emit.setInput(emit.inputIndex("strength"), None)
                            if channels == "color_with_alpha":
                                emit.setNamedInput("strength", image, "alpha")
                            frame = session.wait(
                                session.apply(
                                    dict(
                                        geometry=[ch.geometry_request(obj, sop, material="input")],
                                        materials=[dict(key="input", kind="graph", graph=ch.material_graph(builder))],
                                        camera_path=camera.path(),
                                        background=(0, 0, 0),
                                    )
                                )
                            )
                            pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(32, 32, 4)
                            actual = pixels[16, 16, :3]
                            np.testing.assert_allclose(
                                actual,
                                expected,
                                rtol=0,
                                atol=0.001,
                                err_msg=f"{device}, cache={cache}, mode={mode}, alpha={alpha}, output={channels}",
                            )
                            measured[channels] = actual.tolist()
                        checks.append(dict(mode=mode, alpha=alpha, measured=measured))
            assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
            reports.append(dict(device=device, cache=cache, checks=checks))
    report = dict(
        result="passed",
        cases=reports,
        scope="RGB-only OCIO input processing, explicit texture alpha conventions and cached/uncached agreement",
    )
    (destination / "houdini-texture-alpha-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
finally:
    if original_env is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_env)
    hou.Color.reloadOCIO()
    ch.synchronize_color_configuration()
