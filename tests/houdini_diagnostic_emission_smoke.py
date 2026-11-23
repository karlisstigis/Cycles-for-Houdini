# SPDX-License-Identifier: Apache-2.0
"""Diagnostic magenta stays visible to the camera without lighting the scene.

1. A recovered per-geometry binding (missing UV) uses the camera-only marker.
   Its triangles must not become sampled mesh-light emitters: the light tree
   matches a control where the same object has a plain magenta diffuse
   material, and so does the lamp-lit neighbouring card.
2. A missing texture feeding emission inputs is magenta to the camera, while
   an unlit neighbouring card stays black: with no lights and a black world,
   any light on the card can only be emission, not reflection.
3. Independent valid emission in the same material still lights the card;
   repairing the texture restores its emission everywhere.
"""

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
selection = parser.parse_args().device
device = json.loads(selection) if selection.startswith("[") else selection
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/materials/diagnostic-emission"
assets = dest / ("assets-" + str(os.getpid()))
assets.mkdir(parents=True, exist_ok=True)
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
width, height, samples = 128, 72, 16


def plane(name, size, rows, uv, **transform):
    obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    grid = obj.createNode("grid")
    grid.setParms(dict(type=0, orient=0, rows=rows, cols=rows))
    grid.parmTuple("size").set(size)
    node = obj.createNode("reverse")
    node.setInput(0, grid)  # Face +Z, toward the camera.
    if uv:
        wrangle = obj.createNode("attribwrangle")
        wrangle.setInput(0, node)
        wrangle.parm("snippet").set("v@uv=set(@P.x/%g+0.5,@P.y/%g+0.5,0);" % size)
        node = wrangle
    node.setRenderFlag(True)
    node.setDisplayFlag(True)
    obj.setParms(transform)
    return obj, node


subject, subject_no_uv = plane("diagnostic_subject", (1.6, 1.6), 41, False, tx=-1.0)
subject_uv_obj, subject_uv = plane("diagnostic_subject_uv", (1.6, 1.6), 41, True, tx=-1.0)
card, card_sop = plane("diagnostic_card", (1.2, 1.2), 2, False, tx=1.0, tz=1.0, ry=-45.0)
lamp = hou.node("/obj").createNode("geo", "diagnostic_lamp", run_init_scripts=False)
lamp_grid = lamp.createNode("grid")
lamp_grid.setParms(dict(type=0, orient=2, rows=2, cols=2))
lamp_grid.parmTuple("size").set((0.6, 0.6))
lamp_grid.setRenderFlag(True)
lamp.setParms(dict(tx=0.2, ty=1.6, tz=2.0, rx=180.0))  # Out of frame, facing down toward the card.
cam = hou.node("/obj").createNode("cam", "diagnostic_camera")
cam.setParms(dict(tz=5, projection=1, orthowidth=4.4, resx=width, resy=height))
color = assets / "color.exr"


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


def graph(key, nodes, links, surface):
    return dict(key=key, kind="graph", graph=dict(nodes=nodes, links=links, surface=surface))


textured = graph(
    "subject",
    [node("tex", "image_texture", filename=str(color), colorspace="data"), node("bsdf", "diffuse_bsdf")],
    [link("tex", "color", "bsdf", "color")],
    endpoint("bsdf", "BSDF"),
)
control = dict(key="subject", kind="diffuse", color=(1.0, 0.0, 1.0))
glow = graph(
    "subject",
    [node("tex", "image_texture", filename=str(color), colorspace="data"), node("glow", "emission", strength=1.0)],
    [link("tex", "color", "glow", "color")],
    endpoint("glow", "emission"),
)
principled = graph(
    "subject",
    [
        node("tex", "image_texture", filename=str(color), colorspace="data"),
        node("bsdf", "principled_bsdf", base_color=[0.05, 0.05, 0.05], emission_strength=1.0),
    ],
    [link("tex", "color", "bsdf", "emission_color")],
    endpoint("bsdf", "BSDF"),
)
mixed = graph(
    "subject",
    [
        node("tex", "image_texture", filename=str(color), colorspace="data"),
        node("glow", "emission", strength=1.0),
        node("valid", "emission", color=[0.0, 1.0, 0.0], strength=1.0),
        node("sum", "add_closure"),
    ],
    [
        link("tex", "color", "glow", "color"),
        link("glow", "emission", "sum", "closure1"),
        link("valid", "emission", "sum", "closure2"),
    ],
    endpoint("sum", "closure"),
)


def scene(material, *, uv, lit):
    geometry = [
        ch.geometry_request(
            subject_uv_obj if uv else subject,
            subject_uv if uv else subject_no_uv,
            material="subject",
            material_mode="uniform",
        ),
        ch.geometry_request(card, card_sop, material="card", material_mode="uniform"),
    ]
    if lit:
        geometry.append(ch.geometry_request(lamp, lamp_grid, material="lamp", material_mode="uniform"))
    return dict(
        geometry=geometry,
        materials=[
            material,
            dict(key="card", kind="diffuse", color=(0.8, 0.8, 0.8)),
            dict(key="lamp", kind="emission", color=(1.0, 1.0, 1.0), strength=20.0),
        ],
        camera_path=cam.path(),
        background=(0, 0, 0),
        rendering=dict(samples=samples, adaptive_sampling=False, light_tree=True, seed=7, animate_seed=False),
    )


def planes(frame):
    result = {}
    for name, data in frame["planes"].items():
        result[name] = np.frombuffer(data["pixels"], np.float32).reshape(
            frame["height"], frame["width"], data["channels"]
        )
    return result


def regions(frame):
    p = planes(frame)
    hit = np.isfinite(p["depth"][..., 0]) & (p["depth"][..., 0] > 0) & (p["depth"][..., 0] < 1e6)
    x = p["position"][..., 0]

    def interior(mask, radius=2):
        # Exclude filtered edge pixels that blend surface and background.
        result = mask.copy()
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                result &= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
        return result

    subject_mask, card_mask = interior(hit & (x < -0.3)), interior(hit & (x > 0.5))
    assert subject_mask.sum() > 200 and card_mask.sum() > 100, (subject_mask.sum(), card_mask.sum())
    rgb = p["combined"][..., :3]
    return rgb[subject_mask].mean(axis=0), rgb[card_mask].mean(axis=0)


def emitters(session):
    log = session.diagnostics()["controller"]["worker_log"]
    records = [
        json.loads(line.split("CH_LIGHT_TREE_PHASES ", 1)[1])
        for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith("CH_LIGHT_TREE_PHASES ")
    ]
    complete = [r for r in records if r.get("complete")]
    assert complete, records
    return complete[-1]["emitters"]


def render(session, update):
    frame = session.wait(session.apply(update), timeout=300)
    status = session.status()
    assert status["state"] == "complete" and not status["error"], status
    return frame, status


def new_session():
    return ch.Session(
        device=device,
        width=width,
        height=height,
        samples=samples,
        interactive=False,
        execution="worker",
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined", "depth", "position"],
    )


checks, failures = [], []


def check(name, passed, **detail):
    # Record every outcome before failing, so a run against a host without
    # these fixes shows each defect it reproduces, not only the first.
    checks.append(dict(name=name, passed=bool(passed), **detail))
    if not passed:
        failures.append(name)


def values(array):
    return [float(v) for v in array]


image(color, (0.2, 0.4, 0.6))
with new_session() as session:
    frame, _ = render(session, scene(control, uv=False, lit=True))
    control_emitters = emitters(session)
    _, control_card = regions(frame)
assert 1 <= control_emitters <= 8, control_emitters
assert control_card.max() > 1e-3, control_card
with new_session() as session:
    frame, status = render(session, scene(textured, uv=False, lit=True))
    codes = {d["code"] for d in status["source_diagnostics"]}
    diagnostic_emitters = emitters(session)
    subject_rgb, card_rgb = regions(frame)
check("Missing UV is diagnosed for the recovered binding", "material.missing_uv" in codes, codes=sorted(codes))
check(
    "Recovered binding is magenta to the camera",
    np.allclose(subject_rgb, (1, 0, 1), atol=2e-3),
    subject=values(subject_rgb),
)
check(
    "Recovered missing-UV binding adds no mesh-light emitters",
    diagnostic_emitters == control_emitters,
    control_emitters=control_emitters,
    diagnostic_emitters=diagnostic_emitters,
)
check(
    "Lamp-lit card matches the plain-magenta control",
    np.allclose(card_rgb, control_card, rtol=0.05, atol=1e-4),
    card=values(card_rgb),
    control_card=values(control_card),
)

color.unlink()
with new_session() as session:
    for name, material in (("emission", glow), ("principled", principled)):
        frame, status = render(session, scene(material, uv=True, lit=False))
        actions = [d["action"] for d in status["source_diagnostics"] if d["code"] == "texture.unavailable"]
        subject_rgb, card_rgb = regions(frame)
        check(
            f"Missing texture into {name} emission is diagnosed as camera-only",
            any("camera only" in a for a in actions),
            actions=actions,
        )
        check(
            f"Missing texture into {name} emission is magenta to the camera",
            subject_rgb[0] > 0.5 and subject_rgb[2] > 0.5 and subject_rgb[1] < 0.1,
            subject=values(subject_rgb),
        )
        check(
            f"Missing texture into {name} emission leaves an unlit card black",
            card_rgb.max() < 1e-5,
            unlit_card=values(card_rgb),
        )
    frame, status = render(session, scene(mixed, uv=True, lit=False))
    subject_rgb, card_rgb = regions(frame)
    check(
        "Independent valid emission still lights the card; the magenta branch does not",
        card_rgb[1] > 1e-3 and card_rgb[0] < 0.01 * card_rgb[1] and card_rgb[2] < 0.01 * card_rgb[1],
        card=values(card_rgb),
    )
    image(color, (0.2, 0.4, 0.6))
    frame, status = render(session, scene(glow, uv=True, lit=False))
    subject_rgb, card_rgb = regions(frame)
    check(
        "Repairing the texture clears its diagnostic",
        not status["source_diagnostics"],
        diagnostics=status["source_diagnostics"],
    )
    check(
        "Repairing the texture restores its emission into the scene",
        np.allclose(subject_rgb, (0.2, 0.4, 0.6), atol=2e-3) and card_rgb[2] > 1e-3 and card_rgb[2] > card_rgb[0],
        subject=values(subject_rgb),
        card=values(card_rgb),
    )

report = dict(result="failed" if failures else "passed", device=device, scope=__doc__, failures=failures, checks=checks)
(dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
assert not failures, failures
