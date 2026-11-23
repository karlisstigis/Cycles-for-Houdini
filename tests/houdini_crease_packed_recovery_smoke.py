# SPDX-License-Identifier: Apache-2.0
"""Local crease recovery and packed shutter recovery, verified with independent references."""

from support.paths import artifacts_root
import argparse
import json
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
exec((root / "tests/recovery_geometry_fixture.py").read_text())
parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
hou.setFps(24)
hou.setFrame(1)
camera = hou.node("/obj").createNode("cam", "recovery_camera")
camera.setParms(dict(tz=6, projection=1, orthowidth=6))
material = dict(key="surface", kind="emission", color=(0.2, 0.7, 0.1), strength=1.0)
obj, weights = crease_fixture()
packed, source, targets, prototype = packed_fixture()
checks = []


def plane(frame, name):
    p = frame["planes"][name]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])


with ch.Session(
    device=device, width=192, height=96, samples=64, interactive=False, passes=["combined", "position"]
) as session:

    def render_crease(mode):
        weights.parm("mode").set(mode)
        request = ch.geometry_request(
            obj,
            weights,
            material="surface",
            subdivision=dict(type="catmull_clark", space="object", dicing_rate=0.05, max_level=6),
        )
        frame = session.wait(
            session.apply(
                dict(
                    geometry=[request],
                    materials=[material],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    motion=dict(enabled=False),
                )
            )
        )
        assert session.status()["object_resources"] == 1 and session.status()["subdivision_control_faces"] == 6
        return frame

    sharp = render_crease(0)
    assert not session.status()["source_diagnostics"]
    for bad, reference in ((1, 2), (3, 4), (5, 2), (8, 9)):
        broken = render_crease(bad)
        issues = session.status()["source_diagnostics"]
        assert len(issues) == 1 and issues[0]["code"].startswith("geometry.invalid_"), issues
        count = session.status()["geometry_conversions"]
        session.wait(session.apply(dict(camera_path=camera.path())))
        assert session.status()["source_diagnostics"] == issues and session.status()["geometry_conversions"] == count
        expected = render_crease(reference)
        assert np.array_equal(plane(broken, "position"), plane(expected, "position")), (bad, reference)
        assert np.array_equal(plane(broken, "combined"), plane(expected, "combined")), (bad, reference)
        assert not session.status()["source_diagnostics"]
        repaired = render_crease(0)
        assert np.array_equal(plane(repaired, "position"), plane(sharp, "position"))
        checks.append(dict(case="crease", bad=bad, reference=reference, diagnostics=issues))
    unused = render_crease(6)
    assert not session.status()["source_diagnostics"]
    assert np.array_equal(plane(unused, "position"), plane(sharp, "position"))
    checks.append("Valid vertex weights ignore malformed lower-priority primitive weights")

    def render_packed(mode, speed=24, offset=0, time=0):
        targets.setParms(dict(mode=mode, speed=speed))
        request = ch.geometry_request(packed, source, material="surface", motion="transform", at_time=time)
        return session.wait(
            session.apply(
                dict(
                    geometry=[request],
                    materials=[material],
                    camera_path=camera.path(),
                    background=(0, 0, 0),
                    motion=dict(enabled=True, fps=24.0, shutter_frames=1.0, samples=3, offset=offset),
                ),
                at_time=time,
            ),
            timeout=120,
        )

    healthy = render_packed(0)
    assert not session.status()["source_diagnostics"]
    for mode in (1, 2, 3, 4, 5):
        for offset in (0, 0.25) if mode == 5 else (0,):
            broken = render_packed(mode, offset=offset)
            status = session.status()
            issues = status["source_diagnostics"]
            assert status["object_resources"] == 4 and status["geometry_resources"] == 1, status
            assert any(i["code"] == "motion.packed_unavailable" for i in issues), issues
            assert not source.errors(), source.errors()
            assert np.count_nonzero(plane(broken, "combined")[:, :, 1] > 0.1) > 300, (mode, offset)
            count = status["geometry_conversions"]
            session.wait(session.apply(dict(camera_path=camera.path())))
            assert (
                session.status()["source_diagnostics"] == issues and session.status()["geometry_conversions"] == count
            )
            reference = render_packed(0, speed=0, offset=offset)
            if mode != 2:
                assert np.array_equal(plane(broken, "combined"), plane(reference, "combined")), mode
            else:
                # Stable authored IDs allow surviving instances to retain their
                # animation; only the missing instance must match the static reference.
                p = targets.geometry().points()[0].position()
                x = round((p[0] / 6 + 0.5) * 192)
                y = round((p[1] / 3 + 0.5) * 96)
                region = np.s_[y - 14 : y + 14, x - 22 : x + 22, :]
                assert np.array_equal(plane(broken, "combined")[region], plane(reference, "combined")[region])
                assert not np.array_equal(plane(broken, "combined"), plane(reference, "combined"))
            repaired = render_packed(0)
            assert not session.status()["source_diagnostics"]
            assert np.array_equal(plane(repaired, "combined"), plane(healthy, "combined"))
            checks.append(dict(case="packed", mode=mode, offset=offset, diagnostics=issues))
    # Ambiguous local motion must not remove independently valid OBJ motion.
    packed.parm("tx").setExpression("$T*12", hou.exprLanguage.Hscript)
    broken = render_packed(1)
    reference = render_packed(0, speed=0)
    assert np.array_equal(plane(broken, "combined"), plane(reference, "combined"))
    packed.parm("tx").deleteAllKeyframes()
    packed.parm("tx").set(0)
    checks.append("Packed recovery preserves independent OBJ motion")
    # A changed prototype is captured once and shared by all retained copies.
    before = render_packed(0, speed=0)
    prototype.parm("depth").set(0.8)
    changed = render_packed(0, speed=0)
    assert session.status()["geometry_resources"] == 1 and session.status()["object_resources"] == 4
    mask = plane(changed, "combined")[:, :, 1] > 0.69
    assert np.max(np.abs(plane(changed, "position")[:, :, 2][mask] - 0.4)) < 1e-5
    assert not np.array_equal(plane(before, "position"), plane(changed, "position"))
    prototype.parm("depth").set(0.4)
    repaired = render_packed(0, speed=0)
    assert np.array_equal(plane(before, "position"), plane(repaired, "position"))
    checks.append("Changing shared prototype updates every copy and repairs without losing sharing")
    output = artifacts_root() / "stage3/materials/recovery/crease-packed-frames"
    output.mkdir(parents=True, exist_ok=True)
    for frame, mode in enumerate((0, 1, 0, 1, 0), 1):
        hou.setFrame(frame)
        render_packed(mode, speed=0, time=hou.time())
        session.write_exr(output / ("%s-%d.exr" % ("cpu" if device == "CPU" else "optix", frame)))
        assert bool(session.status()["source_diagnostics"]) == (mode == 1)
    checks.append("Five ordered frames are written through failures and repairs")
report = dict(result="passed", device=device, checks=checks)
path = artifacts_root() / "stage3/materials/recovery/houdini-crease-packed-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(checks))))
