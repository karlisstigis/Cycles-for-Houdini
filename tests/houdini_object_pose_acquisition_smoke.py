# SPDX-License-Identifier: Apache-2.0
"""Acquire accepted OBJ poses without touching a broken SOP or source adapter."""

from support.paths import artifacts_root
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import _native

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
parent = hou.node("/obj").createNode("null", "pose_parent")
obj = hou.node("/obj").createNode("geo", "pose_mixed", run_init_scripts=False)
obj.setInput(0, parent)
box = obj.createNode("box")
box.parmTuple("size").set((0.4, 0.45, 0.3))
line = obj.createNode("line")
line.parm("dist").set(0.5)
width = obj.createNode("attribwrangle")
width.setInput(0, line)
width.parm("snippet").set("@P.x+=.4; f@width=.07;")
point = obj.createNode("add")
point.setParms(dict(points=1, usept0=1, pt0x=-0.4))
radius = obj.createNode("attribwrangle")
radius.setInput(0, point)
radius.parm("snippet").set("f@pscale=.09;")
merge = obj.createNode("merge")
merge.setInput(0, box)
merge.setInput(1, width)
merge.setInput(2, radius)
targets = obj.createNode("grid")
targets.setParms(dict(rows=2, cols=2, orient=0))
targets.parmTuple("size").set((1.2, 0.9))
targets_move = obj.createNode("xform")
targets_move.setInput(0, targets)
copies = obj.createNode("copytopoints")
copies.setInput(0, merge)
copies.setInput(1, targets_move)
copies.parm("pack").set(1)
source = obj.createNode("attribwrangle", "source")
source.setInput(0, merge)
source.parm("snippet").set("@P+=0;")
source.setDisplayFlag(True)
source.setRenderFlag(True)
camera = hou.node("/obj").createNode("cam", "pose_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
now = 0.25
checks = []


def handle():
    return _native.create(device, 240, 144, 16, False, True, True, False, None, True)


def request(motion):
    return dict(
        geometry=[ch.geometry_request(obj, source, material="plain", material_mode="uniform", at_time=now)],
        materials=[dict(key="plain", kind="emission", color=(0.2, 0.7, 0.1), strength=1)],
        camera_path=camera.path(),
        motion=motion,
    )


def image(h, snapshot):
    engine = _native.engine_create(h)
    try:
        generation = _native.apply_snapshot(engine, snapshot)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = _native.status(engine)
            assert status["state"] != "failed", status
            result = _native.frame(engine)
            if result and result["generation"] == generation and result["complete"]:
                return np.frombuffer(result["planes"]["combined"]["pixels"], np.float32).copy()
            time.sleep(0.005)
        raise TimeoutError(status)
    finally:
        _native.close(engine)


for packed, enabled, samples, offset in (
    (packed, *settings) for packed in (False, True) for settings in ((False, 3, 0), (True, 3, 0), (True, 5, 0.5))
):
    source.setInput(0, copies if packed else merge)
    targets_move.parm("tx").deleteAllKeyframes()
    targets_move.parm("tx").set(0)
    if enabled:
        targets_move.parm("tx").setExpression("$T*.2", hou.exprLanguage.Hscript)
    camera.setInput(0, parent if enabled else None)
    parent.parm("tx").set(0)
    obj.parm("tx").deleteAllKeyframes()
    obj.parm("tx").setExpression("$T*.3", hou.exprLanguage.Hscript)
    motion = dict(enabled=enabled, fps=24.0, shutter_frames=0.5, offset=offset, samples=samples)
    h = handle()
    reference = handle()
    try:
        data = request(motion)
        baseline = _native.apply(h, data, now)
        nav = _native.prepare_navigation(h, baseline, data, now)
        header = dict(camera_path=camera.path(), motion=motion)
        assert _native.prepare_object_navigation(h, baseline, header, now, nav) is None
        source.parm("snippet").set("this is deliberately invalid VEX;")
        count = source.cookCount()
        stats = _native.native_stats(h)
        parent.parm("tx").set(0.35)
        acquired = _native.prepare_object_navigation(h, baseline, header, now, nav)
        assert acquired is not None
        assert source.cookCount() == count and _native.native_stats(h) == stats, (
            "Pose acquisition touched the source adapter/SOP"
        )
        assert _native.navigation_info(acquired)["root_revision"] > _native.navigation_info(nav)["root_revision"]
        assert _native.prepare_object_navigation(h, baseline, header, now, acquired) is None
        assert _native.prepare_object_navigation(h, baseline, header, now + 1, acquired) is None
        bad_motion = dict(motion, fps=30)
        assert _native.prepare_object_navigation(h, baseline, dict(header, motion=bad_motion), now, acquired) is None
        try:
            _native.prepare_object_navigation(h, baseline, data, now, acquired)
        except RuntimeError as error:
            assert "cannot acquire geometry/material" in str(error)
        else:
            raise AssertionError("Geometry payload was accepted by pose-only acquisition")
        with ThreadPoolExecutor(1) as pool:
            try:
                pool.submit(_native.prepare_object_navigation, h, baseline, header, now, acquired).result()
            except RuntimeError as error:
                assert "host thread" in str(error)
            else:
                raise AssertionError("Background host acquisition was accepted")
        candidate = _native.navigation_snapshot(baseline, acquired)
        assert candidate is not None
        source.parm("snippet").set("@P+=0;")
        expected = _native.apply(reference, request(motion), now)
        difference = float(np.abs(image(h, candidate) - image(reference, expected)).mean())
        assert difference < 0.002, difference
        # Invalid new roots cannot poison the accepted/previous valid overlay.
        # Houdini clamps a zero scale parameter to 1e-14. A singular
        # pretransform exercises the actual invalid-root contract exactly.
        parent.setPreTransform(hou.hmath.buildScale((0, 1, 1)))
        assert obj.worldTransform().determinant() == 0
        # Retain the accepted camera while independently acquiring OBJ roots.
        invalid = _native.prepare_object_navigation(h, baseline, dict(motion=motion), now, acquired)
        assert invalid is None, dict(
            packed=packed,
            enabled=enabled,
            transform=obj.worldTransform().asTuple(),
            determinant=obj.worldTransform().determinant(),
            accepted=_native.navigation_info(acquired),
            invalid=_native.navigation_info(invalid),
        )
        parent.setPreTransform(hou.Matrix4(1))
        parent.parm("tx").set(0.5)
        repaired = _native.prepare_object_navigation(h, baseline, header, now, acquired)
        assert repaired is not None
        checks.append(
            dict(
                packed=packed,
                enabled=enabled,
                samples=samples,
                offset=offset,
                mean_difference=difference,
                source_cooks_during_pose_acquisition=0,
                parented_camera=enabled,
            )
        )
    finally:
        _native.close(h)
        _native.close(reference)

destination = artifacts_root() / "stage1/early-poses"
destination.mkdir(parents=True, exist_ok=True)
report = dict(
    result="passed",
    device=device,
    checks=checks,
    scope="Owned pose acquisition, broken SOP isolation, contexts, repairs, host-thread enforcement and fresh-render images",
)
(destination / "houdini-object-pose-acquisition-smoke.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
