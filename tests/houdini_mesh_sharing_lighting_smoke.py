# SPDX-License-Identifier: Apache-2.0
"""Shared emitter/blocker palettes against an independent Lambertian area integral.

Distinct emission colors, nonuniform/negative transforms, transparent shadow
bindings and light linking are edited in a retained worker. Data passes and
actual physical geometry counts are checked separately from illumination.
"""

from support.paths import artifacts_root
import argparse, json, os, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/mesh-sharing"
dest.mkdir(parents=True, exist_ok=True)
path = dest / "lighting-report.json"
report = dict(result="running", device=device, scope=__doc__, checks=[])
previous_profile = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
cache_seen = 0
width = height = 64
samples = 512
scene = hou.node("/obj").createNode("subnet", "sharing_lighting")


def grid(name, size, z, reverse=False):
    obj = scene.createNode("geo", name, run_init_scripts=False)
    obj.parm("tz").set(z)
    sop = obj.createNode("grid", "source")
    sop.setParms(dict(type=0, orient=0, rows=2, cols=2))
    sop.parmTuple("size").set((size, size))
    if reverse:
        output = obj.createNode("reverse", "front")
        output.setInput(0, sop)
    else:
        output = sop
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    return obj, output


receiver, receiver_sop = grid("receiver", 2, 0, True)
camera = scene.createNode("cam", "camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=2))
lights = [grid("emitter_" + str(i), 0.8, 3) for i in range(3)]
for index, (obj, sop) in enumerate(lights):
    obj.setParms(dict(tx=(index - 1) * 1.2, ry=180))
colors = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
state = dict(tree=True, exclude=-1, blocker=None, transparent=False)


def request(obj, sop, material):
    value = ch.geometry_request(obj, sop, material=material, material_mode="uniform")
    value["visibility"] = 254
    return value


def update():
    geometry = [ch.geometry_request(receiver, receiver_sop, material="receiver", material_mode="uniform")]
    geometry.extend(request(obj, sop, "light/" + str(i)) for i, (obj, sop) in enumerate(lights))
    materials = [dict(key="receiver", kind="diffuse", color=(0.8, 0.8, 0.8))]
    materials.extend(dict(key="light/" + str(i), kind="emission", color=colors[i], strength=2) for i in range(3))
    if state["blocker"]:
        geometry.append(request(*state["blocker"], "blocker"))
        materials.append(
            dict(
                key="blocker",
                kind="graph",
                graph=dict(
                    nodes=[dict(id="surface", definition="cycles/transparent_bsdf/1", parameters={})],
                    links=[],
                    surface=dict(node="surface", socket="BSDF"),
                ),
            )
            if state["transparent"]
            else dict(key="blocker", kind="diffuse", color=(0, 0, 0))
        )
    rules = (
        []
        if state["exclude"] < 0
        else [dict(objects=[receiver.path()], emitters=[lights[state["exclude"]][0].path()], exclude=True)]
    )
    return dict(
        geometry=geometry,
        materials=materials,
        camera_path=camera.path(),
        background=(0, 0, 0),
        linking=dict(receivers=rules, blockers=[]),
        rendering=dict(
            samples=samples,
            adaptive_sampling=False,
            light_tree=state["tree"],
            max_bounce=0,
            diffuse_bounce=0,
            seed=11,
            animate_seed=False,
        ),
    )


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(height, width, value["channels"])


def integral():
    if state["blocker"] and not state["transparent"]:
        return np.zeros(3)
    n = 128
    axis = ((np.arange(n) + 0.5) / n - 0.5) * 0.8
    u, v = np.meshgrid(axis, axis)
    local = np.stack((u.ravel(), v.ravel(), np.zeros(n * n)), axis=0)
    result = np.zeros(3)
    for i, (obj, sop) in enumerate(lights):
        if i == state["exclude"]:
            continue
        matrix = np.asarray(ch.matrix_from_houdini(obj.worldTransform()), np.float64).reshape(3, 4)
        points = (matrix[:, :3] @ local + matrix[:, 3:4]).T
        area = np.cross(matrix[:, 0], matrix[:, 1])
        jacobian = np.linalg.norm(area)
        normal = area / jacobian
        distance2 = np.sum(points * points, axis=1)
        direction = points / np.sqrt(distance2[:, None])
        transfer = np.maximum(direction[:, 2], 0) * np.abs(direction @ normal) / distance2
        result += np.asarray(colors[i]) * 2 * 0.8 * transfer.mean() * jacobian * 0.8 * 0.8 / np.pi
    return result


try:
    with ch.Session(
        device=device,
        width=width,
        height=height,
        samples=samples,
        threads=2,
        execution="worker",
        interactive=False,
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined", "depth", "normal", "position"],
    ) as session:

        def check(label, cache=None):
            global cache_seen
            frame = session.wait(session.apply(update(), reuse_if_unchanged=True), timeout=120)
            status = session.status()
            assert status["state"] == "complete" and not status["error"] and not status["source_diagnostics"], status
            assert status["mesh_variant_resources"] == 2, status
            assert status["geometry_resources"] == status["object_resources"] == 4 + int(bool(state["blocker"])), status
            rgb = plane(frame, "combined")[:, :, :3]
            assert np.isfinite(rgb).all()
            measured = rgb[28:36, 28:36].mean((0, 1))
            expected = integral()
            np.testing.assert_allclose(measured, expected, rtol=0.04, atol=0.0012, err_msg=label)
            positions = plane(frame, "position")
            normals = plane(frame, "normal")
            np.testing.assert_allclose(positions[28:36, 28:36, 2], 0, rtol=0, atol=2e-5)
            np.testing.assert_allclose(normals[28:36, 28:36], np.broadcast_to((0, 0, 1), (8, 8, 3)), rtol=0, atol=2e-5)
            log = session.diagnostics()["controller"]["worker_log"]
            assert log
            records = [
                json.loads(line.split(" ", 1)[1])
                for line in Path(log).read_text(errors="replace").splitlines()
                if line.startswith("CH_LIGHT_TREE_CACHE ")
            ]
            recent = [r for r in records[cache_seen:] if r.get("complete")]
            cache_seen = len(records)
            if cache == "hit":
                assert recent and all(r["hits"] > 0 and r["misses"] == 0 for r in recent), (label, recent)
            if cache == "miss":
                assert recent and any(r["misses"] > 0 for r in recent), (label, recent)
            report["checks"].append(
                dict(label=label, measured=measured.tolist(), expected=expected.tolist(), status=status, cache=recent)
            )
            path.write_text(json.dumps(report, indent=2))
            return status

        check("Different emission palettes share one emitter mesh")
        for label, color, policy in (
            ("Equal-energy green hue retains exact mesh-local sampling", (0, 1, 0), "hit"),
            ("Equal-energy blue hue retains exact mesh-local sampling", (0, 0, 1), "hit"),
            ("Negative emission with identical absolute energy retains sampling", (-1, 0, 0), "hit"),
            ("Changed absolute emission energy rebuilds the affected sampling", (-2, 0, 0), "miss"),
            ("Repairing absolute energy restores retained sampling", (-1, 0, 0), "hit"),
            ("A new signed RGB energy creates its own sampling", (-0.125, 0.25, -0.5), "miss"),
            ("Exact signed channel permutation retains the same sampling", (0.5, -0.125, 0.25), "hit"),
            ("Restoring red shading preserves its original sampling", (1, 0, 0), "hit"),
        ):
            colors[0] = color
            lights[0][0].parm("tx").set(lights[0][0].evalParm("tx") + 0.001)
            check(label, policy)
        lights[0][0].parm("tx").set(-1.35)
        check("Moving one shared emitter updates its illumination")
        lights[1][0].parmTuple("s").set((1.4, 0.7, 1))
        lights[1][0].parm("rz").set(25)
        check("Nonuniform rotated emitter retains its own measure")
        lights[2][0].parm("sx").set(-1)
        check("Negative scale retains the correct emission palette")
        colors[0] = (0.4, 0.9, 0.1)
        check("Emission edits update live per-object sampling")
        colors[0] = (1, 0, 0)
        check("Emission repair restores the independent integral")
        state["blocker"] = grid("blocker", 0.8, 1.5)
        state["blocker"][0].parmTuple("s").set((4, 4, 1))
        check("Opaque nonemissive instance casts shadows")
        state["transparent"] = True
        check("Transparent instance shares the emitter shape without opaque shadows")
        state["transparent"] = False
        check("Opaque shader repair restores shadows")
        state["blocker"][0].destroy()
        state["blocker"] = None
        check("Blocker deletion preserves shared emitters")
        state["exclude"] = 1
        check("Linking selects each shared emission palette independently")
        state["exclude"] = -1
        check("Link repair restores all three colors")
        state["tree"] = False
        check("Distribution sampling uses effective object shaders")
        state["exclude"] = 1
        check("Distribution sampling retains independent light linking")
        state["exclude"] = -1
        state["blocker"] = grid("blocker_again", 0.8, 1.5)
        state["blocker"][0].parmTuple("s").set((4, 4, 1))
        state["transparent"] = True
        check("Distribution sampling uses the transparent instance shadow shader")
        state["tree"] = True
        check("Light-tree restoration preserves the transparent instance")
    report.update(result="passed", closed=session.status())
    assert report["closed"]["owned_geometry_bytes"] == report["closed"]["device_allocation_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    if previous_profile is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = previous_profile
    path.write_text(json.dumps(report, indent=2))
assert report["result"] == "passed", report.get("error")
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
