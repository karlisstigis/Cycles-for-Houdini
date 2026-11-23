# SPDX-License-Identifier: Apache-2.0
"""Rendered native curves versus independent polygon references, including motion."""

import argparse
import json
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from parametric_curve_fixture import Fixture, reference_geometry

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture()
report = dict(result="running", device=device, checks=[])
camera = hou.node("/obj").createNode("cam", "parametric_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
ref_obj = hou.node("/obj").createNode("geo", "independent_polygon_reference", run_init_scripts=False)
ref = ref_obj.createNode("stash", "reference")
material = dict(key="curves", kind="emission", color=(1, 1, 1), color_attribute="Cd")
session = ch.Session(device=device, width=240, height=160, samples=64)


def render(obj, node, divisions=16, shape="linear", motion="transform", enabled=False, count=1):
    request = ch.geometry_request(
        obj,
        node,
        material="curves",
        material_mode="uniform",
        at_time=hou.time(),
        curve_width=0.08,
        curve_segments=divisions,
        motion=motion,
    )
    request["curve_shape"] = shape
    payload = dict(
        geometry=[request],
        camera_path=camera.path(),
        background=(0, 0, 0),
        materials=[material],
        motion=dict(enabled=enabled, fps=hou.fps(), shutter_frames=0.5, offset=0, samples=3),
    )
    frame = session.wait(session.apply(payload, at_time=hou.time()))
    status = session.status()
    assert (
        status["curves"] == count
        and status["points"] == 0
        and not status["source_diagnostics"]
        and not status["motion_warnings"]
    ), status
    plane = frame["planes"]["combined"]
    pixels = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])
    assert np.isfinite(pixels).all() and pixels[:, :, :3].max() > 0.5
    return pixels.copy(), status


try:
    for name, node in f.attributes.items():
        for divisions in (4, 16):
            count = 2 if name == "discontinuous_nurbs" else 1
            native, status = render(f.obj, node, divisions, count=count)
            ref.parm("stash").set(reference_geometry(node.geometry().prims()[0], divisions))
            expected, _ = render(ref_obj, ref, divisions, count=count)
            difference = float(np.abs(native - expected).mean())
            assert difference < 0.0002, (name, divisions, difference)
            report["checks"].append(dict(name=name, divisions=divisions, mean_difference=difference, status=status))
    node = f.attributes["rational_nurbs"]
    for shape in ("thick", "ribbon"):
        native, status = render(f.obj, node, 16, shape)
        ref.parm("stash").set(reference_geometry(node.geometry().prims()[0], 16))
        expected, _ = render(ref_obj, ref, 16, shape)
        difference = float(np.abs(native - expected).mean())
        assert difference < 0.0002, (shape, difference)
        report["checks"].append(dict(name=shape, mean_difference=difference, status=status))
    _, before = render(f.obj, node)
    for x in (0.05, -0.05, 0):
        camera.parm("tx").set(x)
        session.wait(session.apply(dict(camera_path=camera.path())))
        status = session.status()
        assert all(status[k] == before[k] for k in ("extractions", "geometry_conversions")), status
    request = ch.geometry_request(
        f.obj, node, material="curves", material_mode="uniform", curve_width=0.08, curve_segments=16
    )
    for x in (0.1, -0.1, 0):
        f.obj.parm("tx").set(x)
        request = ch.geometry_request(
            f.obj, node, material="curves", material_mode="uniform", curve_width=0.08, curve_segments=16
        )
        session.wait(session.apply(dict(geometry=[request])))
        status = session.status()
        assert status["geometry_conversions"] == before["geometry_conversions"], status
    report["navigation"] = status
    # Time-dependent native control positions. The only Python SOP constructs
    # the independent reference; the artist source uses native Curve/Convert/
    # Wrangle SOPs. Its explicit time channel follows shutter cook contexts.
    node.parm("snippet").set(node.evalParm("snippet") + "\n@P+=@v*@Time;")
    animated_ref = ref_obj.createNode("python", "animated_independent_reference")
    animated_ref.addSpareParmTuple(hou.FloatParmTemplate("evaluation_time", "Time", 1))
    animated_ref.parm("evaluation_time").setExpression("$T", hou.exprLanguage.Hscript)
    animated_ref.parm("python").set(
        """import hou
from parametric_curve_fixture import reference_geometry
node=hou.pwd();time=node.evalParm('evaluation_time')
source=hou.node('"""
        + node.path()
        + """').geometryAtFrame(hou.timeToFrame(time))
reference_geometry(source.prims()[0],16,node.geometry())
"""
    )
    for frame in range(1, 6):
        hou.setFrame(frame)
        for mode in ("velocity", "deformation"):
            native, status = render(f.obj, node, motion=mode, enabled=True)
            reference, other = render(ref_obj, animated_ref, motion=mode, enabled=True)
            difference = float(np.abs(native - reference).mean())
            assert difference < 0.0003, (frame, mode, difference)
            report["checks"].append(
                dict(
                    name="motion",
                    frame=frame,
                    mode=mode,
                    mean_difference=difference,
                    status=status,
                    reference_status=other,
                )
            )
    session.write_exr(f.dest / ("parametric-" + ("cpu" if device == "CPU" else "optix") + ".exr"))
    session.close()
    closed = session.status()
    assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    session.close()
    (f.dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
