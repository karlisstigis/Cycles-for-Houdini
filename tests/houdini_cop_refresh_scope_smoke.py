# SPDX-License-Identifier: Apache-2.0
"""COP demand, observer scope, OBJ context and live image repair regression."""

from support.paths import artifacts_root

import argparse
import json
import os
from pathlib import Path
import traceback

import hou
import cycles_houdini as ch
from cycles_houdini import _native, native_scene
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
destination = Path(
    os.environ.get(
        "CH_COP_SCOPE_REPORT",
        artifacts_root() / "stage3/textures/cop-scope.json",
    )
)
destination.parent.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[])
f = Fixture(args.device)
owner = hou.node("/obj").createNode("geo", "cop_project_owner", run_init_scripts=False)
modern = owner.createNode("copnet", "project")
modern.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
color = modern.createNode("constant", "color")
color.parm("signature").set("f3")
color.parmTuple("f3").set((0.25, 0.5, 0.75))
out = modern.createNode("null", "out")
out.setInput(0, color)
source = "op:" + out.path()
f.texture.setParms(dict(filename=source, colorspace="data", alpha_type="ignore"))
f.texture.bypass(True)
events = []
observer = None

try:
    with ch.Session(device=args.device, width=64, height=64, samples=4, threads=2, use_texture_cache=False) as session:
        assert session.native_cop_sources() is None

        def render(expected, initial=False):
            payload = dict(materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder))])
            if initial:
                payload.update(
                    geometry=[ch.geometry_request(f.geo, f.sop, material="surface")],
                    camera_path=f.camera.path(),
                    background=(0, 0, 0),
                )
            frame = session.wait(session.apply(payload), timeout=120)
            assert not session.status()["error"], session.status()
            f.expect(frame, expected)
            if observer:
                observer.refresh()
            return _native.cop_capture_stats(session._handle)["captures"]

        render((0.6, 0.6, 0.6), True)
        observer = native_scene.SceneObserver(
            f.rop,
            lambda: events.append("full"),
            lambda: (f.builder.path(),),
            camera_callback=lambda: events.append("camera"),
            cop_sources=session.native_cop_sources,
        )
        watched = lambda: {n for n, _ in observer.nodes}
        assert session.native_cop_sources() == []
        assert out not in watched() and color not in watched()
        color.parm("f3r").setExpression('0.3+ch("' + f.camera.path() + '/tx")', hou.exprLanguage.Hscript)
        observer.refresh()
        assert f.camera not in observer.non_camera_dependencies
        events.clear()
        color.parm("f3g").set(0.5)
        assert not events, events
        events.clear()
        f.camera.parm("tx").set(0.05)
        assert events and set(events) == {"camera"}, events
        assert _native.cop_capture_stats(session._handle)["captures"] == 0
        report["checks"].append(
            "Disconnected/bypassed image has no COP subscriptions, captures or render updates on unused image edits; camera remains camera-only"
        )

        color.parm("f3r").deleteAllKeyframes()
        color.parm("f3r").set(0.3)
        f.texture.bypass(False)
        f.output.setNamedInput("surface", f.texture, "color")
        captures = render((0.3, 0.5, 0.75))
        assert captures == 1 and session.native_cop_sources() == [source]
        assert out in watched() and color in watched()
        assert owner not in observer.non_camera_dependencies
        before = _native.cop_image_source_info(source)
        cooks = color.cookCount()
        owner.parm("tx").set(1)
        assert _native.cop_image_source_info(source) == before
        assert render((0.3, 0.5, 0.75)) == captures and color.cookCount() == cooks
        f.sop.parm("snippet").set(f.sop.evalParm("snippet") + "\n@P.x+=0.01;")
        session.wait(session.apply(dict(geometry=[ch.geometry_request(f.geo, f.sop, material="surface")])), timeout=120)
        assert render((0.3, 0.5, 0.75)) == captures and color.cookCount() == cooks
        report["checks"].append(
            "Active image survives enclosing OBJ transforms and geometry edits without raster acquisition/cooking"
        )

        events.clear()
        color.parm("f3g").set(0.8)
        assert "full" in events, events
        assert render((0.3, 0.8, 0.75)) == captures + 1
        captures += 1
        modern.parmTuple("res").set((24, 12))
        assert render((0.3, 0.8, 0.75)) == captures + 1
        captures += 1
        report["checks"].append("Actual COP color and project resolution edits invalidate the owned image")

        # Removing incidental ancestors must retain explicit expression references.
        owner.addSpareParmTuple(hou.FloatParmTemplate("cop_value", "COP Value", 1, default_value=(0.4,)))
        color.parm("f3r").setExpression('ch("../../cop_value")', hou.exprLanguage.Hscript)
        render((0.4, 0.8, 0.75))
        captures = _native.cop_capture_stats(session._handle)["captures"]
        assert owner in observer.non_camera_dependencies
        events.clear()
        owner.parm("cop_value").set(0.7)
        assert "full" in events, events
        assert render((0.7, 0.8, 0.75)) == captures + 1
        report["checks"].append(
            "Explicit COP expression dependency on a parent OBJ remains observed and refreshes pixels"
        )

        f.rop.addSpareParmTuple(hou.FloatParmTemplate("cop_value", "COP Value", 1, default_value=(0.4,)))
        color.parm("f3r").setExpression('ch("' + f.rop.path() + '/cop_value")', hou.exprLanguage.Hscript)
        render((0.4, 0.8, 0.75))
        captures = _native.cop_capture_stats(session._handle)["captures"]
        assert f.rop.sessionId() in _native.cop_image_source_info(source)["nodes"]
        events.clear()
        f.rop.parm("cop_value").set(0.6)
        assert "full" in events, events
        assert render((0.6, 0.8, 0.75)) == captures + 1
        report["checks"].append("Authored COP references to ROP parameters still invalidate and update pixels")

        color.parm("f3r").setExpression('0.3+ch("' + f.camera.path() + '/tx")', hou.exprLanguage.Hscript)
        render((0.35, 0.8, 0.75))
        captures = _native.cop_capture_stats(session._handle)["captures"]
        assert f.camera in observer.non_camera_dependencies
        events.clear()
        f.camera.parm("tx").set(0.1)
        assert "full" in events and "camera" not in events, events
        assert render((0.4, 0.8, 0.75)) == captures + 1
        report["checks"].append(
            "A genuinely camera-driven COP stays on the full update path and changes rendered pixels"
        )

        color.parm("f3r").deleteAllKeyframes()
        color.parm("f3r").set(0.7)
        render((0.7, 0.8, 0.75))
        out.destroy()
        frame = session.wait(
            session.apply(dict(materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder))])),
            timeout=120,
        )
        f.expect(frame, (1, 0, 1))
        observer.refresh()
        assert session.native_cop_sources() == [source]
        events.clear()
        out = modern.createNode("null", "out")
        out.setInput(0, color)
        assert events, events
        render((0.7, 0.8, 0.75))
        assert not session.status()["source_diagnostics"]
        report["checks"].append(
            "Missing active COP remains in resource demand; recreation automatically triggers repair"
        )

        f.texture.bypass(True)
        render((0, 0, 0))
        assert session.native_cop_sources() == [] and color not in watched()
        events.clear()
        color.parm("f3b").set(0.1)
        assert not events, events
        captures = _native.cop_capture_stats(session._handle)["captures"]
        f.texture.bypass(False)
        assert events
        render((0.7, 0.8, 0.1))
        assert _native.cop_capture_stats(session._handle)["captures"] == captures + 1
        report["checks"].append("Bypass drops COP observation/resources; re-enable acquires the latest pixels")
        report.update(result="passed", execution=session.execution, stats=_native.cop_capture_stats(session._handle))
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if observer:
        observer.close()
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
