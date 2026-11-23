# SPDX-License-Identifier: Apache-2.0
"""One shared material, two bindings: only one has the required UVs."""

from support.paths import artifacts_root
import argparse
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
dest.mkdir(parents=True, exist_ok=True)
texture = dest / ("binding-color-" + str(os.getpid()) + ".exr")
writer = OIIO.ImageOutput.create(str(texture))
assert writer.open(str(texture), OIIO.ImageSpec(4, 4, 3, OIIO.FLOAT))
assert writer.write_image(np.broadcast_to(np.array((0.2, 0.6, 0.3), np.float32), (4, 4, 3)).copy())
assert writer.close()

objects = []
for index, x in enumerate((-2, 2)):
    obj = hou.node("/obj").createNode("geo", "uv_binding_" + str(index), run_init_scripts=False)
    obj.parm("tx").set(x)
    grid = obj.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((3, 3))
    reverse = obj.createNode("reverse")
    reverse.setInput(0, grid)
    uv = obj.createNode("attribwrangle")
    uv.setInput(0, reverse)
    uv.parm("snippet").set("v@uv=set(@P.x/3+0.5,@P.y/3+0.5,0);")
    objects.append((obj, reverse, uv))
camera = hou.node("/obj").createNode("cam", "binding_camera")
camera.setParms(dict(tz=6, projection=1, orthowidth=8))


def node(key, kind, **parameters):
    return dict(id=key, definition=f"cycles/{kind}/1", parameters=parameters)


def ep(key, socket):
    return dict(node=key, socket=socket)


def link(a, output, b, input):
    return {"from": ep(a, output), "to": ep(b, input)}


def material(displaced=False):
    graph = dict(
        nodes=[
            node("image", "image_texture", filename=str(texture), colorspace="data"),
            node("surface", "emission", strength=1.0),
        ],
        links=[link("image", "color", "surface", "color")],
        surface=ep("surface", "emission"),
    )
    if displaced:
        graph["nodes"] += [node("height", "displacement", height=0.4, midlevel=0.0, scale=1.0, space="object")]
        graph.update(displacement=ep("height", "displacement"), displacement_method="true")
    return dict(key="shared", kind="graph", cryptomatte_name="/mat/shared", graph=graph)


def geometry(repair=False):
    return [
        ch.geometry_request(obj, uv if index == 0 or repair else base, material="shared")
        for index, (obj, base, uv) in enumerate(objects)
    ]


def pixels(frame, name="combined"):
    plane = frame["planes"][name]
    return np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])


def check_pair(frame):
    image = pixels(frame)
    assert np.allclose(image[32, 32, :3], (0.2, 0.6, 0.3), atol=1e-5), image[32, 32]
    assert np.allclose(image[32, 96, :3], (1, 0, 1), atol=1e-5), image[32, 96]


checks = []
with ch.Session(
    device=device,
    width=128,
    height=64,
    samples=4,
    interactive=False,
    passes=["combined", "position", "normal"],
    collect_diagnostics=True,
) as session:
    frame = session.wait(
        session.apply(
            dict(
                geometry=geometry(),
                materials=[material()],
                camera_path=camera.path(),
                background=(0, 0, 0),
                cryptomatte=dict(object=True, material=True, asset=False, levels=2),
            )
        )
    )
    check_pair(frame)
    issues = session.status()["source_diagnostics"]
    assert len(issues) == 1 and issues[0]["code"] == "material.missing_uv", issues
    assert issues[0]["source"] == "/mat/shared"
    metadata = session.output_metadata()
    for key, value in metadata.items():
        if key.endswith("/name") and value == "CryptoMaterial":
            manifest = json.loads(metadata[key[:-4] + "manifest"])
            assert "/mat/shared" in manifest and not any("binding_marker" in name for name in manifest), manifest
            crypto = pixels(frame, "CryptoMaterial00")
            assert crypto[32, 32, 0] == crypto[32, 96, 0], crypto[32, [32, 96]]
            break
    else:
        raise AssertionError("No material Cryptomatte manifest")
    checks.append(
        "Shared material remains textured on valid binding; other binding is magenta with the same Cryptomatte identity"
    )

    conversions = session.status()["geometry_conversions"]
    camera.parm("ty").set(0.1)
    frame = session.wait(session.apply(dict(camera_path=camera.path())))
    check_pair(frame)
    assert session.status()["source_diagnostics"] == issues
    assert session.status()["geometry_conversions"] == conversions
    checks.append("Camera edits preserve the variant and warning without geometry conversion")

    frame = session.wait(session.apply(dict(materials=[material(True)])))
    check_pair(frame)
    assert np.allclose(pixels(frame, "position")[32, [32, 96], 2], 0.4, atol=1e-5)
    checks.append("A missing color UV does not disable independent constant displacement")

    shared = material(True)
    shared["graph"]["nodes"] += [node("bw", "rgb_to_bw")]
    shared["graph"]["links"] += [link("image", "color", "bw", "color"), link("bw", "val", "height", "height")]
    frame = session.wait(session.apply(dict(materials=[shared])))
    check_pair(frame)
    positions = pixels(frame, "position")
    assert positions[32, 32, 2] > 0.2 and abs(positions[32, 96, 2]) < 1e-5, positions[32, [32, 96]]
    checks.append("UV input shared by color and height disables displacement only on the affected binding")

    frame = session.wait(session.apply(dict(geometry=geometry(True))))
    assert np.allclose(pixels(frame)[32, [32, 96], :3], (0.2, 0.6, 0.3), atol=1e-5)
    assert not session.status()["source_diagnostics"]
    assert np.allclose(pixels(frame, "position")[32, 32, 2], pixels(frame, "position")[32, 96, 2], atol=1e-5)
    checks.append("Adding UVs restores the original shared material and displacement and clears warnings")

    constant = dict(
        key="shared", kind="emission", color=(0.1, 0.3, 0.8), emission_strength=1.0, cryptomatte_name="/mat/shared"
    )
    frame = session.wait(session.apply(dict(geometry=geometry(), materials=[constant])))
    assert np.allclose(pixels(frame)[32, [32, 96], :3], (0.1, 0.3, 0.8), atol=1e-5)
    assert not session.status()["source_diagnostics"]
    checks.append("Removing the UV dependency retires the fallback while retaining both objects")

    normal = dict(
        key="shared",
        kind="graph",
        graph=dict(
            nodes=[
                node("map", "normal_map", color=(1.0, 0.5, 1.0), strength=1.0),
                node("surface", "diffuse_bsdf", color=(0.2, 0.6, 0.3)),
            ],
            links=[link("map", "normal", "surface", "normal")],
            surface=ep("surface", "BSDF"),
        ),
    )
    frame = session.wait(
        session.apply(
            dict(
                materials=[normal],
                background=(0, 0, 0),
                lights=[
                    dict(
                        key="test_light",
                        kind="point",
                        radius=0.0,
                        strength=(1000, 1000, 1000),
                        transform=(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 4),
                    )
                ],
            )
        )
    )
    shaded = pixels(frame).copy()
    assert len(session.status()["source_diagnostics"]) == 1
    normal["graph"]["nodes"][1]["parameters"]["color"] = (0.4, 0.3, 0.2)
    session.wait(session.apply(dict(materials=[normal])))
    copied = session.diagnostics()["buffer_copy_requests"]
    assert not {"tri_verts", "tri_vindex"} & copied.keys(), copied
    normal["graph"]["nodes"][1]["parameters"]["color"] = (0.2, 0.6, 0.3)
    session.wait(session.apply(dict(materials=[normal])))
    checks.append("Color edits with an active normal map do not upload triangle positions or indices")
    normal["graph"]["nodes"] = normal["graph"]["nodes"][1:]
    normal["graph"]["links"] = []
    baseline = session.wait(session.apply(dict(materials=[normal])))
    assert np.allclose(shaded[32, 96], pixels(baseline)[32, 96], atol=1e-5)
    assert not np.allclose(shaded[32, 32], pixels(baseline)[32, 32], atol=0.01), (
        shaded[32, [32, 96]],
        pixels(baseline)[32, [32, 96]],
    )
    assert not session.status()["source_diagnostics"]
    checks.append("Unavailable tangent normal input uses geometric normals only on the binding without UVs")

    missing_normal = texture.with_name("missing-normal-" + str(os.getpid()) + ".exr")
    for kind, socket in (("normal_map", "color"), ("bump", "height")):
        broken = dict(
            key="shared",
            kind="graph",
            graph=dict(
                nodes=[
                    node("image", "image_texture", filename=str(missing_normal), colorspace="data"),
                    node("effect", kind),
                    node("surface", "diffuse_bsdf", color=(0.2, 0.6, 0.3)),
                ],
                links=[link("image", "color", "effect", socket), link("effect", "normal", "surface", "normal")],
                surface=ep("surface", "BSDF"),
            ),
        )
        frame = session.wait(session.apply(dict(materials=[broken])))
        assert np.allclose(pixels(frame)[32, [32, 96]], pixels(baseline)[32, [32, 96]], atol=1e-5)
        assert any("geometric normals" in d["action"] for d in session.status()["source_diagnostics"])
    checks.append(
        "Missing normal and bump images reproduce the independent bare-diffuse reference without magenta normal values"
    )

report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-binding-recovery-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
