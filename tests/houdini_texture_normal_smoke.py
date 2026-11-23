# SPDX-License-Identifier: Apache-2.0
"""Independently specified tangent/world normals and an analytic bump gradient."""

from support.paths import artifacts_root
from array import array
import json
import math
import os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/textures/normals"
destination.mkdir(parents=True, exist_ok=True)
hou.hipFile.load(str(artifacts_root() / "stage3/textures/native-textures.hiplc"), suppress_save_prompt=True)
geo = hou.node("/obj/texture_receiver")
uv = geo.node("attribwrangle1")
camera = hou.node("/obj/texture_camera")
assert uv is not None


def node(id, type, **parameters):
    return dict(id=id, definition=f"cycles/{type}/1", parameters=parameters)


def ep(id, socket):
    return dict(node=id, socket=socket)


def link(a, output, b, input):
    return {"from": ep(a, output), "to": ep(b, input)}


def diagnostic(nodes, links, normal):
    return dict(
        key="surface",
        kind="graph",
        graph=dict(
            schema_version=1,
            nodes=nodes
            + [
                node("half", "vector_math", math_type="scale", scale=0.5),
                node("encode", "vector_math", math_type="add", vector2=[0.5, 0.5, 0.5]),
                node("emission", "emission", strength=1.0),
            ],
            links=links
            + [
                link(normal, "normal", "half", "vector1"),
                link("half", "vector", "encode", "vector1"),
                link("encode", "vector", "emission", "color"),
            ],
            surface=ep("emission", "emission"),
        ),
    )


def color(frame):
    data = array("f")
    data.frombytes(frame["planes"]["combined"]["pixels"])
    return np.asarray(data)[(32 * 64 + 32) * 4 : (32 * 64 + 32) * 4 + 3]


def expect(frame, normal, tolerance=1e-4):
    measured = color(frame)
    expected = np.asarray(normal) * 0.5 + 0.5
    assert np.allclose(measured, expected, rtol=0, atol=tolerance), (measured, expected)
    return measured.tolist()


def write(path, color):
    output = OIIO.ImageOutput.create(str(path))
    assert output.open(str(path), OIIO.ImageSpec(256, 256, 3, OIIO.FLOAT)), output.geterror()
    pixels = np.asarray(color, dtype=np.float32)
    if pixels.ndim == 1:
        pixels = np.broadcast_to(pixels, (256, 256, 3)).copy()
    assert output.write_image(pixels), output.geterror()
    assert output.close(), output.geterror()


reports = []
for device in ("CPU", ch.default_device()):
    label = "cpu" if device == "CPU" else "optix"
    path = destination / (label + "-normal.exr")
    write(path, (0.5, 0.75, (math.sqrt(0.75) + 1) * 0.5))
    graph = diagnostic(
        [
            node("image", "image_texture", filename=str(path), colorspace="data", interpolation="closest"),
            node("normal", "normal_map", space="tangent", convention="opengl"),
        ],
        [link("image", "color", "normal", "color")],
        "normal",
    )
    request = ch.geometry_request(geo, uv, material="surface")
    checks = []
    with ch.Session(device=device, width=64, height=64, samples=32, use_texture_cache=False) as session:
        frame = session.wait(
            session.apply(dict(geometry=[request], materials=[graph], camera_path=camera.path(), background=(0, 0, 0)))
        )
        checks.append(
            dict(
                name="OpenGL tangent normal in the independently defined UV basis",
                measured=expect(frame, (0, 0.5, math.sqrt(0.75))),
            )
        )
        graph["graph"]["nodes"][1]["parameters"]["convention"] = "directx"
        frame = session.wait(session.apply(dict(materials=[graph])))
        checks.append(
            dict(
                name="DirectX convention reverses the tangent green channel",
                measured=expect(frame, (0, -0.5, math.sqrt(0.75))),
            )
        )
        graph = diagnostic([node("normal", "normal_map", space="world", color=[1.0, 0.5, 0.5])], [], "normal")
        frame = session.wait(session.apply(dict(materials=[graph])))
        checks.append(dict(name="World normal follows the specified world X axis", measured=expect(frame, (1, 0, 0))))
        gradient = (np.arange(256, dtype=np.float32) + 0.5) / 256
        pixels = np.broadcast_to(gradient[None, :, None], (256, 256, 3)).copy()
        path = destination / (label + "-height.exr")
        write(path, pixels)
        graph = diagnostic(
            [
                node("image", "image_texture", filename=str(path), colorspace="data", interpolation="linear"),
                node("height", "rgb_to_bw"),
                node("bump", "bump", distance=1.0, strength=1.0, filter_width=1.0),
            ],
            [link("image", "color", "height", "color"), link("height", "val", "bump", "height")],
            "bump",
        )
        frame = session.wait(session.apply(dict(materials=[graph])))
        normal = np.asarray((-0.125, 0, 1.0))
        normal /= np.linalg.norm(normal)
        checks.append(
            dict(
                name="Texture bump agrees with height derivative du/dx = 1/8",
                measured=expect(frame, normal, tolerance=0.001),
            )
        )
        graph["graph"]["nodes"][2]["parameters"]["invert"] = True
        frame = session.wait(session.apply(dict(materials=[graph])))
        normal[0] *= -1
        checks.append(
            dict(name="Inverted bump reverses the analytic slope", measured=expect(frame, normal, tolerance=0.001))
        )
        session.write_exr(destination / (label + "-bump-normal.exr"))
        original_snippet = uv.parm("snippet").eval()
        for authored_normal in ((0.0, 0.0, 1.0), (0.3, 0.4, math.sqrt(0.75))):
            uv.parm("snippet").set(original_snippet + "\nv@N = set(%s,%s,%s);" % authored_normal)
            request = ch.geometry_request(geo, uv, material="surface")
            for basis in ("original", "displaced"):
                neutral = diagnostic([node("normal", "normal_map", color=[0.5, 0.5, 1.0], base=basis)], [], "normal")
                frame = session.wait(session.apply(dict(geometry=[request], materials=[neutral])))
                checks.append(
                    dict(
                        name="Static corner normals survive a neutral tangent normal map",
                        basis=basis,
                        authored_normal=authored_normal,
                        measured=expect(frame, authored_normal, tolerance=0.0001),
                    )
                )
        uv.parm("snippet").set(original_snippet)
        uv.parm("snippet").set(
            original_snippet
            + "\n@P.z += 0.1 * sin(@Time * 24.0) * @P.x;"
            + "\nv@N = normalize(set(0.25 + @Time, 0.1, 1.0));"
        )
        request = ch.geometry_request(geo, uv, material="surface", motion="deformation", at_time=hou.time())
        geometric = diagnostic([node("normal", "geometry")], [], "normal")
        moving_scene = dict(
            geometry=[request],
            materials=[geometric],
            motion=dict(enabled=True, fps=24.0, shutter_frames=0.5, offset=0.0, samples=3),
        )
        reference_frame = session.wait(session.apply(moving_scene))
        reference_color = color(reference_frame).copy()
        for basis in ("original", "displaced"):
            neutral = diagnostic([node("normal", "normal_map", color=[0.5, 0.5, 1.0], base=basis)], [], "normal")
            frame = session.wait(session.apply(dict(materials=[neutral])))
            measured = color(frame)
            assert np.isfinite(measured).all() and np.allclose(measured, reference_color, rtol=0, atol=0.0001), (
                measured,
                reference_color,
                basis,
            )
            checks.append(
                dict(
                    name="Moving corner normals preserve ray-time shading with a neutral map",
                    basis=basis,
                    measured=measured.tolist(),
                    reference=reference_color.tolist(),
                )
            )
        uv.parm("snippet").set(original_snippet)
    assert session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, result="passed", checks=checks))
report = dict(result="passed", devices=reports)
(destination / "houdini-texture-normal-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
