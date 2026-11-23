# SPDX-License-Identifier: Apache-2.0
"""Binding-local UV/Cd recovery, displacement dependencies and live repairs."""

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
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/recovery"
dest.mkdir(parents=True, exist_ok=True)
texture = dest / ("attribute-" + str(os.getpid()) + ".exr")
out = OIIO.ImageOutput.create(str(texture))
assert out.open(str(texture), OIIO.ImageSpec(2, 2, 3, OIIO.FLOAT))
assert out.write_image(np.broadcast_to(np.array((0.2, 0.6, 0.3), np.float32), (2, 2, 3)).copy())
assert out.close()
objects = []
for index, x in enumerate((-1, 1)):
    obj = hou.node("/obj").createNode("geo", "attribute_binding_" + str(index), run_init_scripts=False)
    obj.parm("tx").set(x)
    stash = obj.createNode("stash")
    objects.append((obj, stash))
camera = hou.node("/obj").createNode("cam", "attribute_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))


def geometry(broken=None, value=None, owner=hou.attribType.Point, kind="mesh", vertex_override=False):
    g = hou.Geometry()
    if kind == "mesh":
        poly = g.createPolygon()
        for xyz in ((-0.75, -0.6, 0), (-0.75, 0.6, 0), (0.75, 0.6, 0), (0.75, -0.6, 0)):
            p = g.createPoint()
            p.setPosition(xyz)
            poly.addVertex(p)
    elif kind == "points":
        g.addAttrib(hou.attribType.Point, "pscale", 0.35)
        g.createPoint().setPosition((0, 0, 0))
    else:
        g.addAttrib(hou.attribType.Point, "width", 0.35)
        poly = g.createPolygon(is_closed=False)
        for y in (-0.6, 0.6):
            p = g.createPoint()
            p.setPosition((0, y, 0))
            poly.addVertex(p)
    for name, default in (("uv", (0.5, 0.5, 0.0)), ("Cd", (0.2, 0.6, 0.3))):
        target = owner if broken == name else hou.attribType.Point
        if name == broken and value is None:
            continue
        val = value if name == broken else default
        a = g.addAttrib(target, name, val)
        items = g.points() if target == hou.attribType.Point else [v for prim in g.prims() for v in prim.vertices()]
        for item in items:
            item.setAttribValue(a, val)
        if vertex_override:
            g.addAttrib(hou.attribType.Vertex, name, default)
    return g


def node(key, kind, **parameters):
    return dict(id=key, definition="cycles/" + kind + "/1", parameters=parameters)


def ep(key, socket):
    return dict(node=key, socket=socket)


def link(a, o, b, i):
    return {"from": ep(a, o), "to": ep(b, i)}


def material(mode="uv", shared=False, normal=False):
    source = (
        node("input", "image_texture", filename=str(texture), colorspace="data")
        if mode == "uv"
        else node("input", "attribute", attribute="Cd")
    )
    if mode == "vertex":
        source = node("input", "vertex_color", layer_name="Cd")
    graph = dict(
        nodes=[
            source,
            node("surface", "emission", strength=1.0),
            node("height", "displacement", height=0.25, midlevel=0.0, scale=1.0, space="object"),
        ],
        links=[link("input", "color", "surface", "color")],
        surface=ep("surface", "emission"),
        displacement=ep("height", "displacement"),
        displacement_method="true",
    )
    if shared:
        graph["nodes"].append(node("bw", "rgb_to_bw"))
        graph["links"] += [link("input", "color", "bw", "color"), link("bw", "val", "height", "height")]
    if normal:
        graph["nodes"] = [
            source,
            node("map", "normal_map", space="object"),
            node("surface", "diffuse_bsdf", color=(0.2, 0.6, 0.3)),
        ]
        graph["links"] = [link("input", "color", "map", "color"), link("map", "normal", "surface", "normal")]
        graph["surface"] = ep("surface", "BSDF")
        graph.pop("displacement")
        graph.pop("displacement_method")
    return dict(key="shared", kind="graph", cryptomatte_name="/mat/attribute_shared", graph=graph)


def scene(mat):
    return dict(
        geometry=[ch.geometry_request(obj, sop, material="shared") for obj, sop in objects],
        materials=[mat],
        camera_path=camera.path(),
        background=(0, 0, 0),
    )


def pixels(frame, name="combined"):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


def check_pair(frame, height=0.25):
    rgb = pixels(frame)
    assert np.allclose(rgb[32, 32, :3], (0.2, 0.6, 0.3), atol=1e-5), rgb[32, 32]
    assert np.allclose(rgb[32, 96, :3], (1, 0, 1), atol=1e-5), rgb[32, 96]
    assert np.allclose(pixels(frame, "position")[32, [32, 96], 2], height, atol=1e-5)


checks = []
with ch.Session(
    device=device, width=128, height=64, samples=16, interactive=False, passes=["combined", "position", "normal"]
) as session:
    for mode, attribute in (("uv", "uv"), ("cd", "Cd")):
        objects[0][1].parm("stash").set(geometry())
        for owner in (hou.attribType.Point, hou.attribType.Vertex):
            for value in (1.0, "invalid", (1, 0, 0), (float("nan"), 0.5, 0.3), (0.5, float("inf"), 0.3)):
                objects[1][1].parm("stash").set(geometry(attribute, value, owner))
                frame = session.wait(session.apply(scene(material(mode))))
                check_pair(frame)
                issues = session.status()["source_diagnostics"]
                assert len(issues) == 1 and issues[0]["code"] == (
                    "material.missing_uv" if mode == "uv" else "material.missing_color"
                ), issues
                assert objects[1][1].path() in issues[0]["message"], issues
                assert session.status()["geometry_resources"] == 2 and not session.status()["error"]
        checks.append(
            attribute
            + ": malformed type/tuple, NaN and infinity recover only the affected binding; independent displacement survives"
        )
        conversions = session.status()["geometry_conversions"]
        frame = session.wait(session.apply(dict(camera_path=camera.path())))
        check_pair(frame)
        assert session.status()["source_diagnostics"] == issues
        assert session.status()["geometry_conversions"] == conversions
        frame = session.wait(session.apply(dict(materials=[material(mode, shared=True)])))
        assert pixels(frame, "position")[32, 32, 2] > 0.15
        assert abs(pixels(frame, "position")[32, 96, 2]) < 1e-5
        objects[1][1].parm("stash").set(geometry())
        frame = session.wait(session.apply(scene(material(mode, shared=True))))
        assert np.allclose(pixels(frame)[32, [32, 96], :3], (0.2, 0.6, 0.3), atol=1e-5)
        assert np.allclose(pixels(frame, "position")[32, 32, 2], pixels(frame, "position")[32, 96, 2], atol=1e-5)
        assert not session.status()["source_diagnostics"]
        checks.append(
            attribute
            + ": shared color/height dependency disables only affected displacement; repair restores it and clears warnings"
        )
    # Valid vertex data takes precedence over invalid, unused point data.
    for attribute in ("uv", "Cd"):
        objects[1][1].parm("stash").set(geometry(attribute, "unused", vertex_override=True))
        frame = session.wait(session.apply(scene(material("uv" if attribute == "uv" else "vertex"))))
        assert not session.status()["source_diagnostics"]
        assert np.allclose(pixels(frame)[32, [32, 96], :3], (0.2, 0.6, 0.3), atol=1e-5)
    checks.append(
        "Vertex UV/color override unused malformed point attributes; Color Attribute node uses the same recovery policy"
    )
    # Float2 UVs are valid, including on meshes; unused UV.z is not interpreted.
    for value in ((0.5, 0.5), (0.5, 0.5, float("nan"))):
        objects[1][1].parm("stash").set(geometry("uv", value))
        frame = session.wait(session.apply(scene(material())))
        assert not session.status()["source_diagnostics"]
        assert np.allclose(pixels(frame)[32, 96, :3], (0.2, 0.6, 0.3), atol=1e-5)
    checks.append("Two-component UVs work and an unused third UV component is ignored")
    constant = dict(key="shared", kind="emission", color=(0.2, 0.6, 0.3), strength=1.0)
    objects[1][1].parm("stash").set(geometry("Cd", float("nan")))
    frame = session.wait(session.apply(scene(constant)))
    assert not session.status()["source_diagnostics"]
    assert np.allclose(pixels(frame)[32, 96, :3], (0.2, 0.6, 0.3), atol=1e-5)
    checks.append("Unused malformed Cd is ignored quietly")
    constant.update(color=(1, 1, 1), color_attribute="Cd")
    frame = session.wait(session.apply(scene(constant)))
    check_pair(frame, height=0.0)
    checks.append("Legacy Cd-driven material also recovers locally")
    # Independent dependencies need all three missing-attribute combinations.
    mat = material("uv")
    mat["graph"]["nodes"] += [node("cd", "attribute", attribute="Cd"), node("bw", "rgb_to_bw")]
    mat["graph"]["links"] += [link("cd", "color", "bw", "color"), link("bw", "val", "height", "height")]
    for bad_uv, bad_cd in ((True, True), (False, True), (True, False), (False, False)):
        g = geometry("Cd", "invalid") if bad_cd else geometry()
        if bad_uv:
            g.findPointAttrib("uv").destroy()
            g.addAttrib(hou.attribType.Point, "uv", "invalid")
        objects[1][1].parm("stash").set(g)
        frame = session.wait(session.apply(scene(mat)))
        assert np.allclose(pixels(frame)[32, 96, :3], (1, 0, 1) if bad_uv else (0.2, 0.6, 0.3), atol=1e-5)
        assert pixels(frame, "position")[32, 32, 2] > 0.15
        assert (
            abs(pixels(frame, "position")[32, 96, 2] - (0 if bad_cd else pixels(frame, "position")[32, 32, 2])) < 1e-5
        )
        codes = {i["code"] for i in session.status()["source_diagnostics"]}
        assert codes == ({"material.missing_uv"} if bad_uv else set()) | (
            {"material.missing_color"} if bad_cd else set()
        ), codes
    checks.append("Simultaneous UV/Cd failures and partial repairs keep independent color/height dependencies separate")
    objects[1][1].parm("stash").set(geometry("Cd", (0.2, float("nan"), 0.3)))
    mat = material("cd", normal=True)
    update = scene(mat)
    update["lights"] = [
        dict(
            key="normal_light",
            kind="point",
            radius=0.0,
            strength=(1000, 1000, 1000),
            transform=(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 4),
        )
    ]
    frame = session.wait(session.apply(update))
    shaded = pixels(frame).copy()
    assert any(i["code"] == "material.missing_color" for i in session.status()["source_diagnostics"])
    mat["graph"]["nodes"] = mat["graph"]["nodes"][2:]
    mat["graph"]["links"] = []
    reference = session.wait(session.apply(dict(materials=[mat])))
    assert np.allclose(shaded[32, 96], pixels(reference)[32, 96], atol=1e-5)
    assert not session.status()["source_diagnostics"]
    checks.append(
        "Cd-driven normal map failure matches independent geometric-normal shading, without diagnostic values entering normals"
    )
    # Diagnostic shader instances keep the source Cryptomatte material name.
    update = scene(material("cd"))
    update["cryptomatte"] = dict(object=True, material=True, asset=False, levels=2)
    frame = session.wait(session.apply(update))
    check_pair(frame)
    metadata = session.output_metadata()
    for key, value in metadata.items():
        if key.endswith("/name") and value == "CryptoMaterial":
            manifest = json.loads(metadata[key[:-4] + "manifest"])
            assert "/mat/attribute_shared" in manifest and not any("binding_marker" in name for name in manifest), (
                manifest
            )
            ids = pixels(frame, "CryptoMaterial00")
            assert ids[32, 32, 0] == ids[32, 96, 0]
            break
    else:
        raise AssertionError("Missing material Cryptomatte manifest")
    checks.append("Color fallback retains the original Cryptomatte material identity")
    objects[1][1].parm("stash").set(geometry("uv", (0.5, float("nan"), 0.0)))
    update = scene(material())
    update["passes"] = ["combined", "position", "normal", "uv"]
    frame = session.wait(session.apply(update))
    check_pair(frame)
    assert len(session.status()["source_diagnostics"]) == 1, session.status()
    assert np.allclose(pixels(frame, "uv")[32, 32, :2], (0.5, 0.5), atol=1e-5)
    assert np.allclose(pixels(frame, "uv")[32, 96], 0, atol=1e-5)
    constant = dict(key="shared", kind="emission", color=(0.2, 0.6, 0.3), strength=1.0)
    session.wait(session.apply(dict(materials=[constant])))
    assert [i["code"] for i in session.status()["source_diagnostics"]] == ["geometry.invalid_uv"]
    session.wait(session.apply(dict(passes=["combined", "position", "normal"])))
    assert not session.status()["source_diagnostics"]
    checks.append("Invalid UV data pass is zero, never magenta; warnings deduplicate and unused UV is quiet")
    for kind in ("points", "curves"):
        for attribute, mode in (("Cd", "cd"), ("uv", "uv")):
            mat = material(mode)
            mat["graph"].pop("displacement")
            mat["graph"].pop("displacement_method")
            for index, (_, sop) in enumerate(objects):
                sop.parm("stash").set(
                    geometry(attribute, (float("nan"), 0.5, 0.3), kind=kind) if index else geometry(kind=kind)
                )
            frame = session.wait(session.apply(scene(mat)))
            rgb = pixels(frame)
            assert np.allclose(rgb[32, 32, :3], (0.2, 0.6, 0.3), atol=1e-5), kind
            assert np.allclose(rgb[32, 96, :3], (1, 0, 1), atol=1e-5), kind
            assert len(session.status()["source_diagnostics"]) == 1
            objects[1][1].parm("stash").set(geometry(kind=kind))
            session.wait(session.apply(scene(mat)))
            assert not session.status()["source_diagnostics"]
        checks.append(kind + ": broken UV/Cd preserve geometry and repair clears warnings")
    obj, source = objects[1]
    source.parm("stash").set(geometry("Cd", "invalid"))
    small = obj.createNode("xform")
    small.setInput(0, source)
    small.parm("scale").set(0.3)
    targets = obj.createNode("grid")
    targets.setParms(dict(rows=2, cols=2, orient=0))
    targets.parmTuple("size").set((1, 1))
    copies = obj.createNode("copytopoints")
    copies.setInput(0, small)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    update = scene(material("cd"))
    update["geometry"] = [ch.geometry_request(obj, copies, material="shared")]
    frame = session.wait(session.apply(update))
    status = session.status()
    assert status["object_resources"] == 4 and status["geometry_resources"] == 1, status
    assert len(status["source_diagnostics"]) == 1, status
    rgb = pixels(frame)
    assert ((rgb[..., 0] > 0.8) & (rgb[..., 2] > 0.8)).sum() > 50
    source.parm("stash").set(geometry())
    session.wait(session.apply(update))
    assert not session.status()["source_diagnostics"]
    assert session.status()["geometry_resources"] == 1
    checks.append("Packed copies keep a shared prototype through color failure and repair")
report = dict(result="passed", device=device, checks=checks)
(dest / "houdini-shading-attribute-recovery-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
