# SPDX-License-Identifier: Apache-2.0
"""Host ramp-control failures preserve independent shading and displacement."""

from support.paths import artifacts_root

import argparse, json, traceback
import hou, numpy as np
import cycles_houdini as ch
from hue_ramp_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
f = Fixture(device)
f.choose("hsl_near_red_blue")
f.displace()
destination = artifacts_root() / "stage3/materials/ramp-control-recovery"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
try:
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        passes=["combined", "position", "normal"],
        execution="worker",
        threads=2,
        use_texture_cache=False,
    ) as session:

        def render(label, expected=None, warning=False):
            frame = session.wait(session.apply(f.update()), timeout=120)
            status = session.status()
            f.inspect(frame, expected)
            assert bool(status["source_diagnostics"]) == warning and not status["error"], status
            if report["checks"]:
                assert status["renderer_pid"] == report["checks"][0]["status"]["renderer_pid"]
            report["checks"].append(dict(label=label, status=status))
            return f.planes(frame), status

        baseline, initial = render("Independent displacement baseline")
        position = baseline["position"].copy()
        normal = baseline["normal"].copy()

        def retained(frame, status, field_layout_change=False):
            np.testing.assert_array_equal(frame["position"], position)
            np.testing.assert_array_equal(frame["normal"], normal)
            assert status["displacement_rebuilds"] == initial["displacement_rebuilds"], status
            if not field_layout_change:
                assert status["geometry_conversions"] == initial["geometry_conversions"], status

        f.ramp.parm("ramp_color_space").set("not_a_mode")
        retained(*render("Invalid Color Mode remains local", (1, 0, 1), True))
        f.choose("hsl_near_red_blue")
        retained(*render("Color Mode repair"))
        f.ramp.parm("ramp_color_space").set("RGB")
        f.ramp.parm("ramp").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.4, 0.4), ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0))))
        retained(*render("Coincident native RGB stops recover locally", (1, 0, 1), True))
        f.choose("hsl_near_red_blue")
        retained(*render("Native RGB stop repair"))
        f.ramp.parm("ramp").set(
            hou.Ramp(
                (hou.rampBasis.Linear,) * 33,
                tuple(i / 32 for i in range(33)),
                tuple((i / 32, 0.0, 1 - i / 32) for i in range(33)),
            )
        )
        retained(*render("Excess authored stops use a bounded fault", (1, 0, 1), True))
        f.choose("hsl_near_red_blue")
        retained(*render("Stop count repair"))
        # Float Curve replaced Float Ramp: Range 0..1, Horizontal and Factor 1
        # map Value like the retired node mapped its Factor.
        scalar = f.mat.createNode("cycles::float_curve::1", "scalar_ramp")
        scalar.setNamedInput("value", f.attribute, "fac")
        scalar.parmTuple("curve_domain").set((0.0, 1.0))
        scalar.parm("curve_extension").set(1)
        scalar.parm("fac").set(1.0)

        def repair():
            scalar.parm("curve_value").set(hou.Ramp((hou.rampBasis.Linear,) * 2, (0.0, 1.0), (0.25, 0.5)))

        repair()
        f.emission.setNamedInput("color", scalar, "value")
        expected = 0.25 + np.asarray(f.factors)[:, None] * 0.25
        retained(*render("Scalar ramp uses the same owned-control policy", expected))
        scalar.parm("curve_value1value").set(float("nan"))
        retained(*render("Broken scalar stop preserves independent displacement", (1, 0, 1), True))
        repair()
        retained(*render("Scalar stop repair", expected))
        scalar.parmTuple("curve_domain").set((1.0, 0.0))
        retained(*render("Broken scalar Range remains local", (1, 0, 1), True))
        scalar.parmTuple("curve_domain").set((0.0, 1.0))
        retained(*render("Range repair", expected))
        f.emission.setNamedInput("color", f.ramp, "color")
        f.displacement.setNamedInput("height", scalar, "value")
        shared, _ = render("Valid scalar height alongside independent color")
        shared_position = shared["position"].copy()
        scalar.parm("curve_value1value").set(float("nan"))
        frame, _ = render("Broken scalar height omits displacement while color stays usable", warning=True)
        assert np.max(np.abs(frame["position"][:, :, 2])) < 1e-5
        repair()
        frame, _ = render("Scalar height repair")
        np.testing.assert_array_equal(frame["position"], shared_position)
        session.close(wait=True)
        closed = session.status()
        assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
