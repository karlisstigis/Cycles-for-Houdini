# SPDX-License-Identifier: Apache-2.0
"""Prove host COP acquisition and owned off-thread pixel reads before rendering."""

from support.paths import artifacts_root
import json
from pathlib import Path
import sys
import threading
import time

import hou
import numpy as np
from cycles_houdini import _native

root = Path(__file__).resolve().parents[1]
output = artifacts_root() / "stage3/textures/cop-ownership"
output.mkdir(parents=True, exist_ok=True)
report = dict(
    result="running",
    scope="Native acquisition/ownership contract only; renderer op: image loading is not claimed.",
    checks=[],
    images=[],
)


def capture(path, at):
    started = time.perf_counter()
    owned = _native.freeze_cop_image(path, at)
    info = _native.cop_image_info(owned)
    info["freeze_seconds"] = time.perf_counter() - started
    report["images"].append(info)
    return owned, info


def pixels(owned, info):
    values = np.frombuffer(_native.cop_image_pixels(owned), np.float32)
    return values.reshape(info["height"], info["width"], info["channels"])


try:
    hou.setFps(24)
    hou.setFrame(1)
    original_time = hou.time()
    legacy = hou.node("/img").createNode("img", "owned_legacy")
    color = legacy.createNode("color", "source")
    color.setParms(dict(overridesize=1, size1=16, size2=8))
    color.parmTuple("color").set((0.25, 0.5, 2.0, 0.5))
    before, info = capture("op:" + color.path(), original_time)
    assert (info["width"], info["height"], info["channels"]) == (16, 8, 4), info
    np.testing.assert_array_equal(pixels(before, info), np.broadcast_to([0.25, 0.5, 2.0, 0.5], (8, 16, 4)))
    report["checks"].append("Legacy non-square HDR RGBA values, without a display transform")

    color.parm("colorr").setExpression(".25+$F*.125", hou.exprLanguage.Hscript)
    first, i1 = capture("op:" + color.path(), hou.frameToTime(1))
    third, i3 = capture("op:" + color.path(), hou.frameToTime(3))
    pinned, ip = capture("op:" + color.path() + "[2]", hou.frameToTime(5))
    np.testing.assert_allclose(pixels(first, i1)[..., 0], 0.375)
    np.testing.assert_allclose(pixels(third, i3)[..., 0], 0.625)
    np.testing.assert_allclose(pixels(pinned, ip)[..., 0], 0.5)
    assert hou.time() == original_time
    report["checks"].append("Legacy requested-time and explicit-frame sources preserve the Houdini timeline")

    modern = hou.node("/img").createNode("copnet", "owned_copernicus")
    modern.setParms(dict(setres=1, res1=16, res2=8, setprecision=1, precision=1))
    constant = modern.createNode("constant", "source")
    menu = constant.parm("signature").menuItems()
    report["signature_menu"] = list(menu)
    constant.parm("signature").set("f3")
    constant.parmTuple("f3").set((0.25, 0.5, 2.0))
    current, ic = capture("op:" + constant.path(), original_time)
    np.testing.assert_allclose(pixels(current, ic)[..., :3], np.broadcast_to([0.25, 0.5, 2.0], (8, 16, 3)))
    report["checks"].append("Copernicus non-square RGB HDR values survive native raster acquisition")

    constant.parm("f3r").setExpression(".25+$F*.125", hou.exprLanguage.Hscript)
    mf, m1 = capture("op:" + constant.path(), hou.frameToTime(1))
    mt, m3 = capture("op:" + constant.path(), hou.frameToTime(3))
    np.testing.assert_allclose(pixels(mf, m1)[..., 0], 0.375)
    np.testing.assert_allclose(pixels(mt, m3)[..., 0], 0.625)
    assert hou.time() == original_time
    report["checks"].append("Copernicus requested-frame acquisition does not change global frame")

    for parent, node, field in [(legacy, color, "colorr"), (modern, constant, "f3r")]:
        target = parent.createNode("null", "metadata_output")
        target.setInput(0, node)
        metadata_before = _native.cop_image_source_info("op:" + target.path())
        assert metadata_before["node_id"] == target.sessionId(), metadata_before
        assert node.sessionId() in metadata_before["dependencies"]
        count = node.cookCount()
        node.parm(field).deleteAllKeyframes()
        node.parm(field).set(0.875)
        changed = _native.cop_image_source_info("op:" + target.path())
        assert node.cookCount() == count, "Metadata invalidation cooked image pixels"
        assert changed["dependencies"] != metadata_before["dependencies"]
        again = _native.cop_image_source_info("op:" + target.path())
        assert again == changed and node.cookCount() == count
        if parent == modern:
            parent.parmTuple("res").set((24, 8))
            assert _native.cop_image_source_info("op:" + target.path())["dependencies"] != changed["dependencies"]
            assert node.cookCount() == count
        fingerprint = _native.cop_image_source_info("op:" + target.path())
        hou.setFps(48)
        assert _native.cop_image_source_info("op:" + target.path())["dependencies"] != fingerprint["dependencies"]
        assert node.cookCount() == count
        hou.setFps(24)
    report["checks"].append("Uncooked upstream COP parameter edits invalidate metadata without reading/cooking pixels")
    report["checks"].append("Project resolution and frame-rate changes invalidate image context without cooking")

    # The original nodes/raster may be modified or deleted before conversion.
    color.parmTuple("color").deleteAllKeyframes()
    color.parmTuple("color").set((1, 0, 1, 1))
    constant.parmTuple("f3").deleteAllKeyframes()
    constant.parmTuple("f3").set((1, 0, 1))
    legacy.destroy()
    modern.destroy()
    result = {}

    def read_owned():
        try:
            result["legacy"] = pixels(first, i1).copy()
            result["modern"] = pixels(mt, m3).copy()
            try:
                _native.freeze_cop_image("op:/img/missing", 0.0)
            except RuntimeError as error:
                result["wrong_thread"] = str(error)
        except BaseException as error:
            result["error"] = error

    worker = threading.Thread(target=read_owned)
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and "error" not in result, result
    np.testing.assert_allclose(result["legacy"][..., 0], 0.375)
    np.testing.assert_allclose(result["modern"][..., 0], 0.625)
    assert "host thread" in result["wrong_thread"], result
    report["checks"].append("Owned pixels remain stable after source edits/deletion and read on another thread")
    report["checks"].append("A background thread cannot cook/resolve host COP sources")

    for path, at in [("op:/img/no_such_cop", 0.0), ("not-an-op", 0.0), ("op:/img/missing", float("nan"))]:
        try:
            _native.freeze_cop_image(path, at)
        except RuntimeError:
            pass
        else:
            raise AssertionError((path, at))
    report["checks"].append("Missing paths, non-op sources and invalid evaluation times fail explicitly")
    report["result"] = "passed"
except BaseException as error:
    import traceback

    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
