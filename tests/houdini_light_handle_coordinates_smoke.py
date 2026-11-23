# SPDX-License-Identifier: Apache-2.0
"""Check dynamic handle values against Houdini's native OBJ parent context.

These are headless binding-math checks, supplemented by actual native mouse
drags in houdini_parented_light_ipr_smoke.py. The expected world frame comes
from the public OBJ matrices, independently of the handle adapter.
"""

from support.paths import artifacts_root
import argparse
import json
import math
import os
from pathlib import Path

import hou
from cycles_houdini import light_properties, light_viewer_state as states

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/light-properties/handle-coordinates"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", cases=[], scope="Headless binding values evaluated in the native OBJ parent transform")


def near(a, b, label):
    if isinstance(a, hou.Vector3):
        error = (a - b).length()
    else:
        error = max(abs(x - y) for x, y in zip(a, b))
    assert error < 2e-5, (label, error, a, b)
    return error


for nested in (False, True):
    manager = hou.node("/obj")
    if nested:
        manager = manager.createNode("subnet", "handle_container")
        manager.parmTuple("t").set((-2, 3, 1))
        manager.parmTuple("r").set((10, -15, 25))
    parent = manager.createNode("null", "handle_parent")
    light = manager.createNode(light_properties.LIGHT_TYPE, "handle_light")
    light.setInput(0, parent)
    light.parmTuple("t").set((3, 1, 4))
    light.parmTuple("r").set((15, 35, -10))
    light.setPreTransform(hou.hmath.buildRotate((5, 10, 15)) * hou.hmath.buildTranslate((1, -2, 0.5)))
    # Area Shape defaults to Square (Blender); the handles here edit both rectangle dimensions.
    light.setParms(dict(areasizex=2, areasizey=3, cycles_light_area_shape="rectangle"))
    light.parm("cycles_light_radius").set(0.4)
    state = object.__new__(states.LightState)
    state.node = light
    state.active = True
    for scale in ((1, 1, 1), (2, 2, 2), (0.5, 1.5, 2)):
        parent.parmTuple("t").set((-4, 2, 5))
        parent.parmTuple("r").set((20, 50, -30))
        parent.parmTuple("s").set(scale)
        context = light.preTransform() * light.parentAndSubnetTransform()
        for kind, handle in (("grid", states.RECTANGLE), ("point", states.RADIUS), ("spot", states.SPOT)):
            light.parm("cycles_light_type").set(kind)
            parms = {}
            state.onStateToHandle(dict(node=light, handle=handle, parms=parms))
            position_keys = ("tx", "ty", "tz") if handle == states.SPOT else ("centerx", "centery", "centerz")
            handle_position = hou.Vector3(tuple(parms[k] for k in position_keys)) * context
            error = near(handle_position, light.worldTransform().extractTranslates(), "Parent applied exactly once")
            record = dict(nested=nested, parent_scale=scale, kind=kind, world_position_error=error)
            rotation = hou.Matrix3(hou.hmath.buildRotate(tuple(parms["r" + axis] for axis in "xyz")))
            world_axes = [
                hou.Vector3(axis) * rotation * hou.Matrix3(context) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))
            ]
            if handle == states.RECTANGLE:
                expected = light_properties.area_size(light)
                actual = [parms["size" + axis] * world_axes[i].length() for i, axis in enumerate("xy")]
                record["world_size_error"] = near(actual, expected, "Area dimensions")
                updated = dict(parms, sizex=parms["sizex"] * 1.2)
                old = light.evalParm("areasizex")
                state.onHandleToState(
                    dict(node=light, handle=handle, parms=updated, prev_parms=parms, mod_parms=["sizex"])
                )
                assert math.isclose(light.evalParm("areasizex"), old * 1.2, rel_tol=1e-6)
            elif handle == states.RADIUS:
                record["world_diameter_error"] = near(
                    [parms["size" + axis] * world_axes[i].length() for i, axis in enumerate("xyz")],
                    [2 * light.evalParm("cycles_light_radius")] * 3,
                    "World-space emitter radius",
                )
                old = light.evalParm("cycles_light_radius")
                state.onHandleToState(
                    dict(
                        node=light,
                        handle=handle,
                        parms=dict(parms, sizex=parms["sizex"] * 1.2),
                        prev_parms=parms,
                        mod_parms=["sizex"],
                    )
                )
                assert math.isclose(light.evalParm("cycles_light_radius"), old * 1.2, rel_tol=1e-6)
            else:
                before = light.worldTransform()
                expected_delta = hou.Vector3(0.3, 0, 0) * hou.Matrix3(context)
                state.onHandleToState(
                    dict(
                        node=light,
                        handle=handle,
                        parms=dict(parms, tx=parms["tx"] + 0.3),
                        prev_parms=parms,
                        mod_parms=["tx"],
                    )
                )
                after = light.worldTransform()
                record["translation_error"] = near(
                    after.extractTranslates(),
                    before.extractTranslates() + expected_delta,
                    "Spot translation in its parent context",
                )
                near(after.asTuple()[:12], before.asTuple()[:12], "Spot translation retains orientation and scale")
            report["cases"].append(record)

report["result"] = "passed"
(destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", cases=len(report["cases"]))))
