# SPDX-License-Identifier: Apache-2.0
"""Native COP pixels, frame context, resource deltas and local shader recovery."""

from support.paths import artifacts_root
import argparse, json, os, traceback, time
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import _native
from shader_bypass_preview_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
f = Fixture(args.device)
destination = artifacts_root() / "stage3/textures/cop-render"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=args.device, checks=[], packets=[])
original_encoder = _native.encode_snapshot


def encode(desired, ack=None):
    data = original_encoder(desired) if ack is None else original_encoder(desired, ack)
    info = _native.snapshot_info(desired) if ack is None else _native.snapshot_info(desired, ack)
    report["packets"].append(dict(bytes=len(data), **info))
    return data


_native.encode_snapshot = encode
legacy = hou.node("/img").createNode("img", "cop_render_legacy")
color = legacy.createNode("color", "color")
color.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
color.parmTuple("color").set((0.25, 0.5, 0.75, 1))
height = legacy.createNode("color", "height")
height.setParms(dict(overridesize=1, size1=32, size2=16, depth=4))
height.parmTuple("color").set((0.2, 0.2, 0.2, 1))
modern = hou.node("/img").createNode("copnet", "cop_render_modern")
modern.setParms(dict(setres=1, res1=32, res2=16, setprecision=1, precision=1))
constant = modern.createNode("constant", "color")
constant.parm("signature").set("f3")
constant.parmTuple("f3").set((0.6, 0.2, 0.4))
texture = f.texture
texture.setParms(dict(filename="op:" + color.path(), colorspace="data", alpha_type="ignore"))
f.output.setNamedInput("surface", texture, "color")
position = lambda frame: np.frombuffer(frame["planes"]["position"]["pixels"], np.float32).reshape(64, 64, 3)[32, 32, 2]
try:
    with ch.Session(
        device=args.device,
        width=64,
        height=64,
        samples=4,
        threads=2,
        passes=["combined", "position"],
        use_texture_cache=False,
        collect_diagnostics=True,
    ) as session:

        def render(name, expected, z=None, warning=False, at=None, update=None):
            at = hou.time() if at is None else at
            payload = (
                update
                if update is not None
                else dict(materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder, at_time=at))])
            )
            frame = session.wait(session.apply(payload, at_time=at), timeout=120)
            status = session.status()
            assert not status["error"] and status["render_device_ids"] == [args.device], status
            assert bool(status["source_diagnostics"]) == warning, status["source_diagnostics"]
            measured = f.expect(frame, expected)
            if z is not None:
                assert np.isclose(position(frame), z, atol=2e-5), (position(frame), z)
            report["checks"].append(
                dict(
                    name=name,
                    measured=measured,
                    z=float(position(frame)),
                    generation=status["generation"],
                    diagnostics=status["source_diagnostics"],
                    cop=_native.cop_capture_stats(session._handle),
                    geometry_conversions=status["geometry_conversions"],
                    displacement_rebuilds=status["displacement_rebuilds"],
                )
            )
            (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            return status

        initial = dict(
            geometry=[ch.geometry_request(f.geo, f.sop, material="surface")],
            camera_path=f.camera.path(),
            background=(0, 0, 0),
            materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder))],
        )
        render(
            "Legacy COP linear HDR values render through an owned native resource", (0.25, 0.5, 0.75), update=initial
        )
        captures = _native.cop_capture_stats(session._handle)["captures"]
        conversions = session.status()["geometry_conversions"]
        f.camera.parm("tx").set(0.1)
        render(
            "Camera-only edit retains COP pixels and native geometry",
            (0.25, 0.5, 0.75),
            update=dict(camera_path=f.camera.path()),
        )
        assert _native.cop_capture_stats(session._handle)["captures"] == captures
        if session.execution == "worker":
            assert report["packets"][-1]["image_pixel_bytes"] == 0 and report["packets"][-1]["image_upserts"] == 0
        color.parmTuple("color").set((0.125, 0.25, 2.0, 1))
        render("An upstream COP edit refreshes pixels without changing the shader definition", (0.125, 0.25, 2.0))
        assert session.status()["geometry_conversions"] == conversions
        texture.parm("colorspace").set("auto")
        render("Unqualified legacy linear metadata does not guess color primaries", (1, 0, 1), warning=True)
        texture.parm("colorspace").set("scene_linear")
        render("Selecting the render working space repairs legacy COP color", (0.125, 0.25, 2.0))
        texture.parm("filename").set("op:" + constant.path())
        render("COPernicus float3 values render without file export", (0.6, 0.2, 0.4))
        assert _native.cop_capture_stats(session._handle)["resources"] == 1
        texture.parm("filename").set("op:" + color.path())
        color.parm("colorr").setExpression(".1+$F*.1", hou.exprLanguage.Hscript)
        before = hou.time()
        for frame in range(1, 6):
            render(
                "Explicit animated COP frame " + str(frame), (0.1 + frame * 0.1, 0.25, 2.0), at=hou.frameToTime(frame)
            )
            image_path = destination / ("cop-frame-%04d.exr" % frame)
            session.write_exr(str(image_path))
            assert image_path.stat().st_size > 0
        assert hou.time() == before
        texture.parm("filename").set("op:" + color.path() + "[2]")
        render("Pinned COP frame overrides render frame", (0.3, 0.25, 2.0), at=hou.frameToTime(5))
        color.parm("colorr").deleteAllKeyframes()
        color.parm("colorr").set(0.25)
        texture.parm("filename").set("op:" + color.path())
        h = f.builder.createNode("cycles::image_texture::1", "cop_height")
        h.setParms(dict(filename="op:" + height.path(), colorspace="data", alpha_type="ignore"))
        h.parm("tiles").set("")
        bw = f.builder.createNode("cycles::rgb_to_bw::1", "height_scalar")
        bw.setNamedInput("color", h, "color")
        displacement = f.builder.createNode("cycles::displacement::1", "cop_displacement")
        displacement.setParms(dict(midlevel=0, scale=1, space="object"))
        displacement.setNamedInput("height", bw, "val")
        f.output.setNamedInput("displacement", displacement, "displacement")
        f.builder.parm("cycles_displacement_method").set("true")
        render("Independent COP height drives actual mesh displacement", (0.25, 0.25, 2.0), z=0.2)
        rebuilt = session.status()["displacement_rebuilds"]
        color.parm("colorr").set(0.5)
        render("Color-only COP edit retains independent displacement", (0.5, 0.25, 2.0), z=0.2)
        assert session.status()["displacement_rebuilds"] == rebuilt
        texture.parm("filename").set("op:/img/absent_color/source")
        render("Missing COP color remains magenta with valid displaced geometry", (1, 0, 1), z=0.2, warning=True)
        texture.parm("filename").set("op:" + color.path())
        render("Repairing COP color clears its diagnostic", (0.5, 0.25, 2.0), z=0.2)
        h.parm("filename").set("op:/img/absent_height/source")
        render("Missing COP displacement disables only that branch", (0.5, 0.25, 2.0), z=0, warning=True)
        h.parm("filename").set("op:" + height.path())
        render("Repairing COP displacement restores its geometry", (0.5, 0.25, 2.0), z=0.2)
        height.parmTuple("color").set((0.4, 0.4, 0.4, 1))
        render("Height-only COP edit updates actual displaced positions", (0.5, 0.25, 2.0), z=0.4)
        texture.parm("filename").set("op:" + constant.path())
        texture.parm("colorspace").set("auto")
        render("An unqualified COP color space reports recovery rather than guessing", (1, 0, 1), z=0.4, warning=True)
        texture.parm("colorspace").set("scene_linear")
        render("Explicit render-space selection repairs the COP color input", (0.6, 0.2, 0.4), z=0.4)
        constant.parm("signature").set("f1")
        constant.parm("f1").set(0.375)
        render("A scalar COP image expands to grayscale", (0.375, 0.375, 0.375), z=0.4)
        constant.parm("signature").set("f4")
        constant.parmTuple("f4").set((0.125, 0.25, 0.5, 0.375))
        texture.parm("alpha_type").set("unassociated")
        f.output.setNamedInput("surface", texture, "alpha")
        render("COP alpha reaches the Image Texture alpha output", (0.375, 0.375, 0.375), z=0.4)
        f.output.setNamedInput("surface", texture, "color")
        render("Color-only straight alpha follows pinned Cycles premultiplication", (0.046875, 0.09375, 0.1875), z=0.4)
        constant.parm("signature").set("f2")
        constant.parmTuple("f2").set((0.25, 0.5))
        texture.parm("alpha_type").set("ignore")
        render("COP float2 data keeps X and Y with zero Z", (0.25, 0.5, 0), z=0.4)
        constant.parm("signature").set("f3")
        constant.parmTuple("f3").set((0.8, 0.2, 0.4))
        before_pid = session.status().get("renderer_pid")
        bad = dict(
            camera=dict(near=-1), materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder))]
        )
        if session.execution == "worker":
            session.apply(bad)
            until = time.monotonic() + 15
            while True:
                status = session.status()
                if status.get("update_rejected"):
                    break
                assert time.monotonic() < until and status.get("backend_state") != "failed", status
                time.sleep(0.01)
            assert session.status()["renderer_pid"] == before_pid
        else:
            try:
                session.apply(bad)
            except _native.SceneUpdateRejected:
                pass
            else:
                raise AssertionError("Invalid camera update was accepted")
        render(
            "Rejected camera/image transaction retains the worker and repairs from its ACK",
            (0.8, 0.2, 0.4),
            z=0.4,
            update=dict(
                camera_path=f.camera.path(),
                materials=[dict(key="surface", kind="graph", graph=ch.material_graph(f.builder))],
            ),
        )
        session.write_exr(str(destination / "cop-textures.exr"))
        report.update(
            result="passed",
            execution=session.execution,
            status=session.status(),
            cop=_native.cop_capture_stats(session._handle),
        )
    assert _native.cop_capture_stats(session._handle)["pixel_bytes"] == 0
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    _native.encode_snapshot = original_encoder
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
