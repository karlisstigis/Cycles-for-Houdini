# SPDX-License-Identifier: Apache-2.0
"""Per-face energy ordering, signed hue edits and repairs on native/packed IPR."""

from support.paths import artifacts_root
import argparse, json, os, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/light-tree-energy"
dest.mkdir(exist_ok=True)
path = dest / "face-report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[], batch=[])
previous = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
scene = hou.node("/obj").createNode("subnet", "energy_faces")
obj = scene.createNode("geo", "emitter", run_init_scripts=False)
obj.parm("tz").set(1.5)
grid = obj.createNode("grid", "two_faces")
grid.setParms(dict(type=0, orient=0, rows=2, cols=3))
grid.parmTuple("size").set((1.4, 0.8))
materials = [ch.create_material(name="energy_face_" + str(i), shader="emission") for i in range(2)]
assigned = obj.createNode("attribwrangle", "face_bindings")
assigned.setInput(0, grid)
assigned.parm("class").set(1)
assigned.parm("snippet").set(
    "s@shop_materialpath=(@primnum%2)?" + json.dumps(materials[1].path()) + ":" + json.dumps(materials[0].path()) + ";"
)
pack = obj.createNode("pack", "packed_faces")
pack.setInput(0, assigned)
pack.parm("pivot").set("origin")
targets = obj.createNode("grid", "targets")
targets.setParms(dict(type=0, orient=0, rows=2, cols=2))
targets.parmTuple("size").set((1.8, 1.1))
copies = obj.createNode("copytopoints", "four_packed_emitters")
copies.setInput(0, pack)
copies.setInput(1, targets)
receiver = scene.createNode("geo", "receiver", run_init_scripts=False)
ground = receiver.createNode("grid", "ground")
ground.setParms(dict(type=0, orient=0, rows=2, cols=2))
ground.parmTuple("size").set((5, 5))
front = receiver.createNode("reverse", "face_lights")
front.setInput(0, ground)
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
width, height = 64, 48
source = assigned
seen = 0
low = (0.125, 0.25, 0.5)
high = (0.25, 0.5, 1.0)


def colors(a, b, strengths=(1, 1)):
    for material, color, strength in zip(materials, (a, b), strengths):
        node = material.node("surface_shader")
        node.parmTuple("color").set(color)
        node.parm("strength").set(strength)


def update():
    emitter = ch.geometry_request(obj, source, material="fallback", material_mode="native")
    emitter["visibility"] = 254
    return dict(
        geometry=[ch.geometry_request(receiver, front, material="receiver", material_mode="uniform"), emitter],
        materials=[
            dict(key="receiver", kind="diffuse", color=(0.8, 0.8, 0.8)),
            dict(key="fallback", kind="diffuse", color=(1, 1, 1)),
        ],
        material_resolver=native_scene.resolve_materials,
        camera_path=camera.path(),
        background=(0, 0, 0),
        rendering=dict(
            samples=256, adaptive_sampling=False, max_bounce=0, diffuse_bounce=0, seed=13, animate_seed=False
        ),
    )


def new_session():
    return ch.Session(
        device=device,
        width=width,
        height=height,
        samples=256,
        threads=2,
        execution="worker",
        interactive=True,
        collect_diagnostics=True,
        use_texture_cache=False,
        passes=["combined", "depth", "normal", "position"],
    )


try:
    with new_session() as session:

        def check(label, policy=None):
            global seen
            frame = session.wait(session.apply(update(), reuse_if_unchanged=True), timeout=120)
            status = session.status()
            assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
            log = session.diagnostics()["controller"]["worker_log"]
            assert log
            records = [
                json.loads(line.split(" ", 1)[1])
                for line in Path(log).read_text(errors="replace").splitlines()
                if line.startswith("CH_LIGHT_TREE_CACHE ")
            ]
            recent = [r for r in records[seen:] if r.get("complete")]
            seen = len(records)
            if policy == "hit":
                assert recent and all(r["hits"] > 0 and r["misses"] == 0 for r in recent), (label, recent)
            if policy == "miss":
                assert recent and any(r["misses"] > 0 for r in recent), (label, recent)
            previous_disabled = os.environ.get("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE")
            os.environ["CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE"] = "1"
            try:
                with new_session() as reference:
                    expected = reference.wait(reference.apply(update()), timeout=120)
                closed = reference.status()
                assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
            finally:
                if previous_disabled is None:
                    os.environ.pop("CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE", None)
                else:
                    os.environ["CYCLES_HOUDINI_DISABLE_LIGHT_TREE_CACHE"] = previous_disabled
            differences = {}
            for name in ("combined", "depth", "normal", "position"):
                actual = np.frombuffer(frame["planes"][name]["pixels"], np.float32)
                wanted = np.frombuffer(expected["planes"][name]["pixels"], np.float32)
                assert np.isfinite(actual).all() and np.isfinite(wanted).all(), name
                differences[name] = float(np.max(np.abs(actual - wanted)))
                assert differences[name] < 2e-5, (label, name, differences[name])
            report["checks"].append(dict(label=label, status=status, cache=recent, differences=differences))
            path.write_text(json.dumps(report, indent=2))
            return status

        for label, node, gain in (("native faces", assigned, 1), ("packed faces", copies, 2)):
            # Geometry ownership can survive native-to-packed transitions. Use
            # new exact energies so the distribution-miss check cannot match
            # a valid tree already retained by the earlier native context.
            l, h = (tuple(v * gain for v in color) for color in (low, high))
            hue_l, hue_h = (tuple((c[2], c[0], c[1])) for c in (l, h))
            signed_l = (-hue_l[0], hue_l[1], hue_l[2])
            signed_h = (hue_h[0], -hue_h[1], -hue_h[2])
            source = node
            colors(l, h)
            check(label + " cold")
            colors(hue_l, hue_h)
            check(label + " exact-energy hue", "hit")
            colors(signed_l, signed_h)
            check(label + " exact absolute-energy signs", "hit")
            colors(signed_l, signed_h, (-1, 1))
            check(label + " negative strength preserves absolute energy", "hit")
            colors(h, l)
            check(label + " face energy swap preserves total but rebuilds its distribution", "miss")
            colors(l, h)
            check(label + " per-face distribution repair", "hit")
        for frame in range(1, 6):
            hou.setFrame(frame)
            colors(hue_l if frame % 2 else l, hue_h if frame % 2 else h)
            check("Ordered hue frame " + str(frame), "hit")
            output = dest / ("frame-" + str(frame) + ".exr")
            session.write_exr(str(output))
            assert output.is_file() and output.stat().st_size > 1024
            report["batch"].append(dict(frame=frame, path=str(output)))
    report.update(result="passed", closed=session.status())
    assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if previous is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = previous
    path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]), batch=len(report["batch"]))))
