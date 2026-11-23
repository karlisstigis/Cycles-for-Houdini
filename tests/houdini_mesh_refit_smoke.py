# SPDX-License-Identifier: Apache-2.0
"""Refit Meshes While Interacting (ROP ipr_refit_meshes): refit decisions and images.

A deforming mesh and deforming curves render in a live session with the checkbox off and on.
Each state is compared with a fresh render of the same state (new session, built from scratch).
Checked: meshes refit only with the checkbox on, only when the triangles and motion steps are
unchanged, and at most 16 times in a row; curves are never refitted; images equal fresh renders in
both checkbox states; a final (non-interactive) session never refits. Scene in memory, never saved.
"""

import argparse, json, os
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, object_properties, registry
from support.paths import artifacts_root

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
os.environ.pop("CYCLES_HOUDINI_BVH_REFIT", None)
dest = artifacts_root() / "stage3/mesh-refit"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, scope=__doc__, checks=[])

obj = hou.node("/obj")
cam = obj.createNode("cam", "refit_cam")
cam.setParms(dict(ty=4, tz=7, rx=-30, resx=320, resy=180))
mesh_obj = obj.createNode("geo", "refit_mesh", run_init_scripts=False)
object_properties.install(mesh_obj)
grid = mesh_obj.createNode("grid")
grid.setParms(dict(sizex=6, sizey=6, rows=120, cols=120))
grid.parm("surftype").set(next(i for i in grid.parm("surftype").parmTemplate().menuItems() if "tri" in i.lower()))
wave = mesh_obj.createNode("attribwrangle")
wave.setInput(0, grid)
wave.parm("snippet").set("@P.y += 0.3 * sin(@P.x * 2 + @Time * 4); v@v = set(0, 1.2 * cos(@P.x * 2 + @Time * 4), 0);")
wave.setRenderFlag(True)
wave.setDisplayFlag(True)
hair = obj.createNode("geo", "refit_curves", run_init_scripts=False)
object_properties.install(hair)
line = hair.createNode("line")
line.setParms(dict(points=5, dist=0.5))
roots = hair.createNode("grid")
roots.setParms(dict(sizex=2, sizey=2, rows=30, cols=30, tx=2))
copy = hair.createNode("copytopoints::2.0")
copy.setInput(0, line)
copy.setInput(1, roots)
sway = hair.createNode("attribwrangle")
sway.setInput(0, copy)
sway.parm("snippet").set(
    "float u = (@ptnum % 5) / 4.0; @P.x += 0.2 * u * sin(@Time * 3); f@width = 0.02; v@v = set(0.6 * u * cos(@Time * 3), 0, 0);"
)
sway.setRenderFlag(True)
sway.setDisplayFlag(True)
rop = hou.node("/out").createNode("cycles_ipr", "refit_rop")
rop.setParms(
    dict(
        camera=cam.path(),
        resx=320,
        resy=180,
        ipr_samples=16,
        samples=16,
        ipr_adaptive_sampling=0,
        ipr_use_denoise=0,
        use_denoise=0,
        allowmotionblur=0,
        vobject=mesh_obj.name() + " " + hair.name(),
        world_source="color",
    )
)
gm = rop.parm("geometry_motion")
gm.set(next(i for i in gm.parmTemplate().menuItems() if "veloc" in i))
for name, value in (("seed", 1), ("animate_seed", 0), ("use_animated_seed", 0)):
    if rop.parm(name) is not None:
        rop.parm(name).set(value)
device_selection.set_devices(rop, device)
assert rop.parm("ipr_refit_meshes") is not None and rop.evalParm("ipr_refit_meshes") == 0, (
    "checkbox exists and is off by default"
)

# state: (name, frame, grid rows, motion blur, refit expected with the checkbox on)
STATES = [
    ("first", 1, 120, 0, 0),
    ("frame2", 2, 120, 0, 1),
    ("frame3", 3, 120, 0, 1),
    ("topology", 4, 121, 0, 0),
    ("frame5", 5, 121, 0, 1),
    ("blur_on", 6, 121, 1, 0),
    ("blur_frame7", 7, 121, 1, 1),
    ("blur_off", 8, 121, 0, 0),
]


def set_state(state):
    _, frame, rows, blur, _ = state
    hou.setFrame(frame)
    grid.parm("rows").set(rows)
    rop.parm("allowmotionblur").set(blur)


def render(entry):
    frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
    return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy(), int(
        entry.session.status()["mesh_refits"]
    )


def check(ok, name, **values):
    report["checks"].append(dict(name=name, ok=bool(ok), **values))
    assert ok, (name, values)


try:
    fresh = {}
    for state in STATES:
        set_state(state)
        registry.close_all()
        entry = registry.for_rop(rop, native_scene.configuration(rop))
        fresh[state[0]], _ = render(entry)
    for checkbox in (0, 1):
        rop.parm("ipr_refit_meshes").set(checkbox)
        registry.close_all()
        set_state(STATES[0])
        entry = registry.for_rop(rop, native_scene.configuration(rop))
        before = 0
        for state in STATES:
            set_state(state)
            image, refits = render(entry)
            delta, before = refits - before, refits
            check(
                delta == (state[4] if checkbox else 0),
                "refit decision",
                checkbox=checkbox,
                state=state[0],
                refits=delta,
            )
            error = float(np.abs(image - fresh[state[0]]).max())
            check(error < 1e-5, "image equals a fresh render", checkbox=checkbox, state=state[0], max_error=error)
    # At most 16 refits in a row: the 17th update of the same topology rebuilds, then refits resume.
    deltas = []
    for frame in range(9, 9 + 18):
        hou.setFrame(frame)
        _, refits = render(entry)
        deltas.append(refits - before)
        before = refits
    check(deltas == [1] * 16 + [0, 1], "rebuild after 16 refits", deltas=deltas)
    registry.close_all()
    # Final (non-interactive) sessions never refit, whatever the checkbox says.
    config = native_scene.configuration(rop, interactive=False)
    check(config[-1] is False, "final configuration carries no refit", value=config[-1])
    with ch.Session(device=device, width=320, height=180, samples=4, interactive=False, refit_meshes=True) as final:
        for frame in (30, 31, 32):
            hou.setFrame(frame)
            final.wait(final.apply(native_scene.capture(rop)))
        check(final.status()["mesh_refits"] == 0, "final session never refits", refits=final.status()["mesh_refits"])
    report["result"] = "passed"
finally:
    registry.close_all()
    (dest / "mesh-refit-report.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            dict(
                result=report["result"],
                checks=len(report["checks"]),
                failed=[c for c in report["checks"] if not c["ok"]],
            ),
            indent=1,
        )
    )
