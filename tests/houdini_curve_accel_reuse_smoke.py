# SPDX-License-Identifier: Apache-2.0
"""An accel-only change of a curve SOP is never answered with the retained curves.

Acceleration blur reads point accel beside v. A wrangle changes only accel (a
parameter scales it; P and v are not written, so their data ids stay), for
curves alone (full-detail selection), curves beside a closed polygon (a partial
selection, which the content signature checks), and the curves packed. After
the change, what the worker wrote into the engine's hair arrays
(CYCLES_HOUDINI_HASH_CURVES=1, core/session_strands.cpp) must equal a fresh
session's result for the new accel, and differ from the old one.
"""

from support.paths import artifacts_root
import argparse
import glob
import json
import os
import tempfile
import time
import traceback
import hou
from cycles_houdini import device_selection, native_scene
from cycles_houdini.ipr import registry
from cycles_houdini.scene import object_properties

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
dest = artifacts_root() / "stage2/hair/accel-reuse"
dest.mkdir(parents=True, exist_ok=True)
LOGDIR = tempfile.mkdtemp(prefix="curve_accel_")
os.environ["CYCLES_HOUDINI_WORKER_LOG_DIR"] = LOGDIR
os.environ["CYCLES_HOUDINI_HASH_CURVES"] = "1"
report = dict(result="running", device=device, cases={})
SEEN = {}
PARTS = {}


def differing(a, b):
    x, y = PARTS.get(a, {}), PARTS.get(b, {})
    return sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))


def hair_hashes():
    out = []
    for path in sorted(glob.glob(os.path.join(LOGDIR, "*"))):
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        for line in lines[SEEN.get(path, 0) :]:
            if line.startswith("CH_CURVE_HASH "):
                record = json.loads(line[len("CH_CURVE_HASH ") :])
                record.pop("used_shaders", None)  # Material keys are per process.
                PARTS[record["hash"]] = record
                out.append(record["hash"])
        SEEN[path] = len(lines)
    return out


def curves_object(name, variant):
    obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    line = obj.createNode("line")
    line.parm("points").set(8)
    line.parmTuple("dir").set((0, 1, 0))
    line.parm("dist").set(0.5)
    grid = obj.createNode("grid")
    grid.parmTuple("size").set((4, 4))
    grid.parm("rows").set(50)
    grid.parm("cols").set(50)
    copy = obj.createNode("copytopoints::2.0")
    copy.setInput(0, line)
    copy.setInput(1, grid)
    shape = obj.createNode("attribwrangle", "shape")
    shape.setInput(0, copy)
    shape.parm("snippet").set("float u = (@ptnum % 8) / 7.0; v@v = set(0.2, 0, 0) * u; f@width = 0.02;")
    motion = obj.createNode("attribwrangle", "accel_only")
    motion.setInput(0, shape)
    group = motion.parmTemplateGroup()
    group.append(hou.FloatParmTemplate("k", "k", 1, default_value=(1.0,)))
    motion.setParmTemplateGroup(group)
    # Writes only accel: P and v keep their data ids when k changes.
    motion.parm("snippet").set('float u = (@ptnum % 8) / 7.0; v@accel = set(0, 4.0 * ch("k"), 1.0) * u;')
    last = motion
    if variant == "partial":
        box = obj.createNode("box")
        box.parmTuple("t").set((0, -1, 0))
        merge = obj.createNode("merge")
        merge.setInput(0, motion)
        merge.setInput(1, box)
        last = merge
    elif variant == "packed":
        pack = obj.createNode("pack")
        pack.setInput(0, motion)
        last = pack
    last.setDisplayFlag(True)
    last.setRenderFlag(True)
    object_properties.install(obj)
    return obj, motion


def render(rop, entry=None):
    capture = native_scene.capture(rop)
    if entry is None:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
    entry.session.wait(entry.apply(capture, hou.time()))
    status = entry.session.status()
    assert not status.get("error"), status.get("error")
    return entry


try:
    hou.setFrame(1)
    camera = hou.node("/obj").createNode("cam", "accel_camera")
    camera.setParms(dict(tx=0, ty=0.3, tz=8, resx=64, resy=48))
    rop = hou.node("/out").createNode("cycles_ipr", "accel_rop")
    device_selection.set_devices(rop, [device] if device != "CPU" else "CPU")
    rop.setParms(
        dict(
            camera=camera.path(),
            resx=64,
            resy=48,
            ipr_samples=1,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            world_source="color",
            allowmotionblur=1,
        )
    )
    motion_parm = rop.parm("geometry_motion")
    motion_parm.set(next(i for i in motion_parm.parmTemplate().menuItems() if "accel" in i.lower()))
    for variant in ("full", "partial", "packed"):
        obj, wrangle = curves_object("accel_" + variant, variant)
        rop.parm("vobject").set(obj.name())
        # Session A: k = 1, then k = 2 on the same frame (only accel changes).
        registry.close_all()
        hair_hashes()
        wrangle.parm("k").set(1.0)
        entry = render(rop)
        time.sleep(0.2)
        before = hair_hashes()
        wrangle.parm("k").set(2.0)
        render(rop, entry)
        registry.close_all()
        time.sleep(0.3)
        after = hair_hashes()
        # Session B: a fresh session at k = 2.
        entry = render(rop)
        registry.close_all()
        time.sleep(0.3)
        fresh = hair_hashes()
        report["cases"][variant] = dict(before=before, after=after, fresh=fresh)
        assert before and fresh, (variant, before, fresh)
        assert after, (variant, "the accel change was not applied: the retained curves were reused")
        # Hair node settings (motion steps, motion blur flag) are compared apart: the first
        # update of a session records them before the integrator's blur setting is applied.
        geometry_parts = [
            k for k in differing(after[-1], fresh[-1]) if k not in ("hash", "settings", "settings_values")
        ]
        assert not geometry_parts, (variant, after, fresh, geometry_parts)
        assert "attribute:P" in differing(after[-1], before[-1]), (variant, before, after)
        obj.destroy()
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    print(report["error"], flush=True)
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, default=str), flush=True)
hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True)
