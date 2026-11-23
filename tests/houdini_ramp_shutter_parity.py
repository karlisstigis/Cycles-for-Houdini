# SPDX-License-Identifier: Apache-2.0
"""Compare an animated Houdini ramp driving true displacement with the same animated scalar."""

import argparse
import json
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene
from displacement_motion_recovery_fixture import Fixture

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
fixture = Fixture(args.device)
hou.setFrame(2)
fixture.rop.parm("vobject").set(fixture.obj.path())
fixture.obj.parm("rx").set(45)
fixture.sop.parm("tx").setExpression("0.01 * $T", hou.exprLanguage.Hscript)
fixture.recovery_switch.parm("input").deleteAllKeyframes()
fixture.recovery_switch.parm("input").set(0)
fixture.material.node("surface_output").setNamedInput("displacement", fixture.scalar, "displacement")
fixture.scalar.setInput(fixture.scalar.inputIndex("height"), None)
ramp = fixture.material.createNode("cycles::color_ramp::1", "shutter_ramp")
ramp.parm("alpha_ramp").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.0, 1.0)))
ramp.parm("factor").set(0.5)
endpoint = ramp.parm("alpha_ramp2value")
endpoint.setExpression("2 * ($FF - 1)", hou.exprLanguage.Hscript)
fixture.scalar.parm("height").setExpression("$FF - 1", hou.exprLanguage.Hscript)


def render():
    with ch.Session(device=args.device, width=192, height=128, samples=64, execution="worker") as session:
        frame = session.wait(session.apply(native_scene.capture(fixture.rop), at_time=hou.time()))
        status = session.status()
        assert status["render_device_ids"] == [args.device], status["render_device_ids"]
        assert not status["error"] and not status["source_diagnostics"], status
        return np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).copy().reshape(128, 192, 4)


reference = render()
fixture.scalar.setNamedInput("height", ramp, "alpha")
animated = render()
endpoint.deleteAllKeyframes()
endpoint.set(2)
frozen = render()
signal = np.max(np.abs(animated - frozen), axis=2) > 0.01
assert np.all(np.isfinite(animated)) and signal.sum() > 10, int(signal.sum())
reference_error = float(np.abs(animated[signal] - reference[signal]).mean())
freeze_error = float(np.abs(animated[signal] - frozen[signal]).mean())
assert reference_error < freeze_error * 0.1, (reference_error, freeze_error)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(
    json.dumps(
        dict(
            device=args.device,
            pixels=int(signal.sum()),
            animated_reference_mae=reference_error,
            frozen_mae=freeze_error,
            result="passed",
        ),
        indent=2,
    )
)
