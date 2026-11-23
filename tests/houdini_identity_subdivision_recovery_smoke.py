# SPDX-License-Identifier: Apache-2.0
"""Optional identity/subdivision metadata never removes otherwise valid geometry."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
exec((root / "tests/identity_subdivision_fixture.py").read_text())
parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
hou.setFps(24)
hou.setFrame(1)
fixtures = {kind: identity_fixture(kind) for kind in ("mesh", "points", "curves", "packed", "subdivision")}
camera = hou.node("/obj").createNode("cam", "identity_camera")
camera.setParms(dict(tz=6, projection=1, orthowidth=6))
material = dict(key="surface", kind="emission", color=(0.2, 0.7, 0.1), strength=1.0)
checks = []


def pixels(frame, name="combined"):
    return np.frombuffer(frame["planes"][name]["pixels"], np.float32).copy()


with ch.Session(
    device=device, width=192, height=96, samples=32, interactive=False, passes=["combined", "position"]
) as session:

    def render(kind, mode, motion="none", subdiv=True, now=0):
        obj, node = fixtures[kind]
        node.parm("mode").set(mode)
        settings = (
            dict(type="catmull_clark", space="object", dicing_rate=0.05, max_level=6)
            if kind == "subdivision" and subdiv
            else dict(type="none")
        )
        request = ch.geometry_request(
            obj,
            node,
            material="surface",
            motion=motion if motion != "none" else "transform",
            subdivision=settings,
            at_time=now,
        )
        frame = session.wait(
            session.apply(
                dict(
                    geometry=[request],
                    materials=[material],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    motion=dict(enabled=motion != "none", fps=24.0, shutter_frames=1.0, samples=3, offset=0),
                ),
                at_time=now,
            ),
            timeout=120,
        )
        status = session.status()
        assert status["object_resources"] == (4 if kind == "packed" else 1), status
        assert status["geometry_resources"] == 1, status
        assert np.count_nonzero(pixels(frame).reshape(-1, 4)[:, 1] > 0.1) > 100, (kind, mode, motion)
        return frame

    for kind in ("mesh", "points", "curves", "packed"):
        for motion in ("none",) if kind == "packed" else ("none", "velocity", "deformation"):
            baseline = render(kind, 0, motion)
            assert not session.status()["source_diagnostics"]
            for mode in range(1, 7):
                actual = render(kind, mode, motion)
                assert not session.status()["source_diagnostics"], (kind, mode, motion, session.status())
                assert np.array_equal(pixels(actual), pixels(baseline)), (kind, mode, motion)
                assert np.array_equal(pixels(actual, "position"), pixels(baseline, "position")), (kind, mode, motion)
                repaired = render(kind, 0, motion)
                assert np.array_equal(pixels(repaired), pixels(baseline))
                checks.append(dict(kind=kind, mode=mode, motion=motion))
    for enabled in (False, True):
        baseline = render("subdivision", 0, subdiv=enabled)
        for mode in range(1, 8):
            actual = render("subdivision", mode, subdiv=enabled)
            status = session.status()
            issues = status["source_diagnostics"]
            assert bool(issues) == enabled, (mode, issues)
            assert len(issues) == (2 if mode == 7 else 1) if enabled else not issues
            assert np.array_equal(pixels(actual), pixels(baseline)), (enabled, mode)
            assert np.array_equal(pixels(actual, "position"), pixels(baseline, "position")), (enabled, mode)
            count = status["geometry_conversions"]
            session.wait(session.apply(dict(camera_path=camera.path())))
            assert session.status()["geometry_conversions"] == count
            assert session.status()["source_diagnostics"] == issues
            repaired = render("subdivision", 0, subdiv=enabled)
            assert not session.status()["source_diagnostics"]
            assert np.array_equal(pixels(repaired), pixels(baseline))
            checks.append(dict(kind="subdivision", mode=mode, enabled=enabled, diagnostics=issues))
    # Real Merge SOPs retain overlapping IDs/names from separate input branches.
    for kind in ("mesh", "points", "curves", "packed"):
        obj, node = fixtures[kind]
        shifted = obj.createNode("xform", "second_sim")
        shifted.setInput(0, node)
        shifted.parm("ty").set(0.8)
        merged = obj.createNode("merge", "merged_sims")
        merged.setInput(0, node)
        merged.setInput(1, shifted)

        def merged_frame(mode):
            node.parm("mode").set(mode)
            frame = session.wait(
                session.apply(
                    dict(geometry=[ch.geometry_request(obj, merged, material="surface")], motion=dict(enabled=False))
                )
            )
            assert not session.status()["source_diagnostics"], session.status()
            assert session.status()["geometry_resources"] == 1
            assert session.status()["object_resources"] == (8 if kind == "packed" else 1)
            return pixels(frame)

        expected = merged_frame(0)
        for mode in (1, 3, 4):
            assert np.array_equal(merged_frame(mode), expected), (kind, mode)
            checks.append(dict(kind=kind, merged_duplicate_metadata=mode))
    output = artifacts_root() / "stage3/materials/recovery/identity-subdivision-frames"
    output.mkdir(parents=True, exist_ok=True)
    for frame, mode in enumerate((0, 7, 0, 7, 0), 1):
        hou.setFrame(frame)
        render("subdivision", mode, now=hou.time())
        session.write_exr(output / ("%s-%d.exr" % ("cpu" if device == "CPU" else "optix", frame)))
    checks.append("Five ordered outputs survive metadata edits and repairs")
path = artifacts_root() / "stage3/materials/recovery/houdini-identity-subdivision-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(dict(result="passed", device=device, checks=checks), indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
