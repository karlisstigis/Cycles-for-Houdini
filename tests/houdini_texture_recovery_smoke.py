# SPDX-License-Identifier: Apache-2.0
"""Texture failures recover locally, preserving independent displacement."""

from support.paths import artifacts_root
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
dest = artifacts_root() / "stage3/materials/recovery"
assets = dest / ("texture-assets-" + str(os.getpid()))
assets.mkdir(parents=True, exist_ok=True)
obj = hou.node("/obj").createNode("geo", "texture_recovery", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
grid.parmTuple("size").set((8, 8))
reverse = obj.createNode("reverse")
reverse.setInput(0, grid)
uv = obj.createNode("attribwrangle")
uv.setInput(0, reverse)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
cam = hou.node("/obj").createNode("cam", "texture_recovery_camera")
cam.setParms(dict(tz=5, projection=1, orthowidth=4))
color, height = assets / "color.exr", assets / "height.exr"


def image(path, value):
    pixels = np.broadcast_to(np.array(value, dtype=np.float32), (4, 4, 3)).copy()
    writer = OIIO.ImageOutput.create(str(path))
    assert writer
    assert writer.open(str(path), OIIO.ImageSpec(4, 4, 3, OIIO.FLOAT))
    assert writer.write_image(pixels)
    assert writer.close()
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10000000))


def node(key, kind, **parameters):
    return dict(id=key, definition=f"cycles/{kind}/1", parameters=parameters)


def endpoint(key, socket):
    return dict(node=key, socket=socket)


def link(a, output, b, input):
    return {"from": endpoint(a, output), "to": endpoint(b, input)}


def material(displacement=False):
    graph = dict(
        nodes=[
            node("color", "image_texture", filename=str(color), colorspace="data"),
            node("surface", "emission", strength=1.0),
        ],
        links=[link("color", "color", "surface", "color")],
        surface=endpoint("surface", "emission"),
    )
    if displacement:
        graph["nodes"] += [
            node("height", "image_texture", filename=str(height), colorspace="data"),
            node("bw", "rgb_to_bw"),
            node("displace", "displacement", space="object", midlevel=0.0, scale=1.0),
        ]
        graph["links"] += [link("height", "color", "bw", "color"), link("bw", "val", "displace", "height")]
        graph.update(displacement=endpoint("displace", "displacement"), displacement_method="true")
    return dict(key="surface", kind="graph", graph=graph)


def pixel(frame, plane):
    data = frame["planes"][plane]
    return np.frombuffer(data["pixels"], np.float32).reshape(frame["height"], frame["width"], data["channels"])[24, 32]


checks = []
with ch.Session(
    device=device, width=64, height=48, samples=4, interactive=False, passes=["combined", "position"]
) as session:
    initial = dict(
        geometry=[ch.geometry_request(obj, uv, material="surface")],
        materials=[material()],
        camera_path=cam.path(),
        background=(0, 0, 0),
    )
    frame = session.wait(session.apply(initial), timeout=120)
    assert np.allclose(pixel(frame, "combined")[:3], (1, 0, 1), atol=1e-5)
    assert session.status()["source_diagnostics"][0]["code"] == "texture.unavailable"
    session.write_exr(dest / "missing-color.exr")
    reader = OIIO.ImageInput.open(str(dest / "missing-color.exr"))
    assert reader
    metadata = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics"))
    assert metadata[0]["resource"] == "surface" and metadata[0]["severity"] == "warning"
    reader.close()
    checks.append("First render with missing image is magenta and writes warning metadata")

    image(color, (0.2, 0.4, 0.6))
    frame = session.wait(session.apply({}))
    assert np.allclose(pixel(frame, "combined")[:3], (0.2, 0.4, 0.6), atol=1e-5), pixel(frame, "combined")
    assert not session.status()["source_diagnostics"]
    checks.append("Creating a missing image restores it without resubmitting the material")

    image(height, (0.3, 0.3, 0.3))
    frame = session.wait(session.apply(dict(materials=[material(True)])))
    assert abs(pixel(frame, "position")[2] - 0.3) < 1e-5
    builds = session.status()["displacement_rebuilds"]
    color.unlink()
    frame = session.wait(session.apply({}))
    assert np.allclose(pixel(frame, "combined")[:3], (1, 0, 1), atol=1e-5)
    assert abs(pixel(frame, "position")[2] - 0.3) < 1e-5
    assert session.status()["displacement_rebuilds"] == builds
    checks.append("Missing color does not disable or rebuild independently valid displacement")

    image(color, (0.2, 0.4, 0.6))
    height.unlink()
    frame = session.wait(session.apply({}))
    assert np.allclose(pixel(frame, "combined")[:3], (0.2, 0.4, 0.6), atol=1e-5)
    assert abs(pixel(frame, "position")[2]) < 1e-5, pixel(frame, "position")
    assert any("displacement" in d["action"] for d in session.status()["source_diagnostics"])
    image(height, (0.6, 0.6, 0.6))
    frame = session.wait(session.apply({}))
    assert abs(pixel(frame, "position")[2] - 0.6) < 1e-5
    assert not session.status()["source_diagnostics"]
    checks.append("Missing height renders the base surface; repairing it restores displacement")

    bad_space = material()
    bad_space["graph"]["nodes"][0]["parameters"]["colorspace"] = "missing-test-space"
    frame = session.wait(session.apply(dict(materials=[bad_space])))
    assert np.allclose(pixel(frame, "combined")[:3], (1, 0, 1), atol=1e-5)
    assert any("colorspace" in d["message"] for d in session.status()["source_diagnostics"])
    checks.append("Unknown input color space produces a marker instead of guessing a transform")

    shared = material(True)
    shared["graph"]["nodes"] = [n for n in shared["graph"]["nodes"] if n["id"] != "height"]
    shared["graph"]["links"][1] = link("color", "color", "bw", "color")
    color.unlink()
    frame = session.wait(session.apply(dict(materials=[shared])))
    assert np.allclose(pixel(frame, "combined")[:3], (1, 0, 1), atol=1e-5)
    assert abs(pixel(frame, "position")[2]) < 1e-5
    image(color, (0.4, 0.4, 0.4))
    frame = session.wait(session.apply({}))
    assert np.allclose(pixel(frame, "combined")[:3], (0.4, 0.4, 0.4), atol=1e-5)
    assert abs(pixel(frame, "position")[2] - 0.4) < 1e-5
    assert not session.status()["source_diagnostics"]
    checks.append("A failed image shared by color and displacement disables the height branch; repair restores both")

    tiled = material()
    tiled["graph"]["nodes"][0]["parameters"].update(filename=str(assets / "tiles.<UDIM>.exr"), tiles=[1001])
    frame = session.wait(session.apply(dict(materials=[tiled])))
    assert np.allclose(pixel(frame, "combined")[:3], (1, 0, 1), atol=1e-5)
    image(assets / "tiles.1001.exr", (0.7, 0.2, 0.1))
    frame = session.wait(session.apply({}))
    assert np.allclose(pixel(frame, "combined")[:3], (0.7, 0.2, 0.1), atol=1e-5)
    assert not session.status()["source_diagnostics"]
    checks.append("Creating a missing UDIM tile restores the original input without resubmitting its graph")

report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-texture-recovery-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
