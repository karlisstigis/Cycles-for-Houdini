# SPDX-License-Identifier: Apache-2.0
"""Real ROP sequences: resource retention, analytic images and failure cleanup."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from cycles_houdini import passes
import argparse
import json
import os
from pathlib import Path
import time

import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import final_render, native_scene, registry


parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
root = artifacts_root() / "stage3/final-sequence"
root.mkdir(parents=True, exist_ok=True)
hou.setFps(24)
hou.setFrame(1)
scene = hou.node("/obj").createNode("subnet", "final_sequence_objects")
moving = scene.createNode("geo", "animated_mesh", run_init_scripts=False)
moving.parm("tx").setExpression("($F-1)*.2", hou.exprLanguage.Hscript)
shape = moving.createNode("box", "animated_shape")
shape.parmTuple("size").set((0.5, 0.7, 0.2))
shape.parm("sizez").setExpression(".2 + .1*($F-1)", hou.exprLanguage.Hscript)
shape.setRenderFlag(True)
static = scene.createNode("geo", "unchanged_mesh", run_init_scripts=False)
static.parm("tx").set(-1)
static_shape = static.createNode("box")
static_shape.parmTuple("size").set((0.5, 0.7, 0.2))
static_shape.setRenderFlag(True)
material = ch.create_material(name="animated_final_emission", shader="emission")
shader = material.node("surface_shader")
shader.parmTuple("color").set((0.2, 0.1, 0.05))
shader.parm("strength").set(1)
shader.parmTuple("color")[0].setExpression(".2 + .1*($F-1)", hou.exprLanguage.Hscript)
moving.parm("shop_materialpath").set(material.path())
static_material = ch.create_material(name="unchanged_final_emission", shader="emission")
static_material.node("surface_shader").parmTuple("color").set((0.05, 0.1, 0.8))
static_material.node("surface_shader").parm("strength").set(1)
static.parm("shop_materialpath").set(static_material.path())
camera = hou.node("/obj").createNode("cam", "final_sequence_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "final_sequence_rop")
passes.add(rop, "depth")
set_rop_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        camera=camera.path(),
        resx=128,
        resy=96,
        samples=8,
        transparent_background=1,
        persistent_data=1,
        use_denoise=0,
        adaptive_sampling=0,
    )
)
rop.parmTuple("background").set((0, 0, 0))
assert rop.parm("persistent_data").eval() == 1
assert rop.parm("persistent_data").description() == "Persistent Data"
final_render.register(rop)
final_render.register(rop)

factory = registry.session_from_configuration
original_wait = final_render.wait_for_completion
case = "persistent"
created = []
frames = []
events = []
reports = []


def record_factory(*args, **kwargs):
    session = factory(*args, **dict(kwargs, collect_diagnostics=True))
    if kwargs.get("interactive") is False:
        created.append(dict(case=case, session=session))

        # Final waits must never materialize all render planes into Python.
        def reject_pixels(*args, **kwargs):
            raise AssertionError("Final wait copied full render planes")

        session.frame = reject_pixels
    return session


def record_wait(session, generation, **kwargs):
    if case == "cancelled" and generation == 2:
        session.cancel()
    began = time.monotonic()
    result = original_wait(session, generation, timeout=90, **kwargs)
    frames.append(
        dict(
            case=case,
            generation=generation,
            seconds=time.monotonic() - began,
            status=session.status(),
            diagnostics=session.diagnostics(),
        )
    )
    return result


def record_event(node, kind, at_time):
    number = round(hou.timeToFrame(at_time))
    events.append(dict(case=case, event=str(kind), frame=number))
    if kind == hou.ropRenderEventType.PreFrame and number == 2:
        if case == "missing-source":
            node.setParms(dict(source_mode="sop", soppath="/obj/absent/OUT"))
        elif case == "missing-camera":
            node.parm("camera").set("/obj/absent_camera")
        elif case == "output-failure":
            node.parm("picture").set(str(root / "blocked-output.exr"))


def inspect(path, frame):
    image = OIIO.ImageInput.open(str(path))
    assert image, (path, OIIO.geterror())
    spec = image.spec()
    pixels = image.read_image(format=OIIO.FLOAT)
    image.close()
    assert np.isfinite(pixels).all()
    depth = pixels[:, :, spec.channelnames.index("depth.Z")]
    rgba = pixels[:, :, [spec.channelnames.index(name) for name in ("R", "G", "B", "A")]]
    # Independently specified orthographic projection and changing box thickness.
    x = round((spec.width - 1) / 2 + (frame - 1) * 0.2 * spec.width / 4)
    y = spec.height // 2
    color = rgba[y - 2 : y + 2, x - 2 : x + 2].mean((0, 1))
    expected = (0.2 + 0.1 * (frame - 1), 0.1, 0.05, 1)
    assert np.allclose(color, expected, atol=0.001, rtol=0), (frame, color, expected)
    expected_depth = 4.9 - 0.05 * (frame - 1)
    assert abs(float(depth[y, x]) - expected_depth) < 0.001, (frame, depth[y, x], expected_depth)
    static_x = round((spec.width - 1) / 2 - spec.width / 4)
    assert np.allclose(rgba[y, static_x], (0.05, 0.1, 0.8, 1), atol=0.001, rtol=0)
    assert abs(float(depth[y, static_x]) - 4.9) < 0.001
    return dict(frame=frame, width=spec.width, height=spec.height, color=color.tolist(), depth=float(depth[y, x]))


registry.session_from_configuration = record_factory
final_render.wait_for_completion = record_wait
rop.addRenderEventCallback(record_event, run_before_script=True)
try:
    for case in (
        "persistent",
        "per-frame",
        "configuration-change",
        "missing-source",
        "missing-camera",
        "output-failure",
        "cancelled",
    ):
        rop.parm("resx").deleteAllKeyframes()
        rop.setParms(
            dict(
                source_mode="objects",
                camera=camera.path(),
                resx=128,
                persistent_data=int(case != "per-frame"),
                picture=str(root / f"{case}.$F4.exr"),
            )
        )
        if case == "configuration-change":
            rop.parm("resx").setExpression("if($F < 2, 128, 160)", hou.exprLanguage.Hscript)
        if case == "output-failure":
            (root / "blocked-output.exr").mkdir(exist_ok=True)
        for number in (1, 2, 3):
            (root / f"{case}.{number:04d}.exr").unlink(missing_ok=True)
        error = None
        try:
            rop.render(frame_range=(1, 3, 1))
        except hou.OperationFailed as failure:
            error = str(failure)
        failing = case in ("missing-source", "missing-camera", "output-failure", "cancelled")
        assert bool(error) == failing, (case, error)
        assert not final_render.active_sequences(), (case, final_render.active_sequences())
        sessions = [value["session"] for value in created if value["case"] == case]
        expected_sessions = 3 if case == "per-frame" else 2 if case == "configuration-change" else 1
        assert len(sessions) == expected_sessions, (case, len(sessions))
        assert all(session.execution == "sync" for session in sessions)
        assert all(session.status()["state"] == "closed" for session in sessions)
        summary = rop.cachedUserData(final_render._summary_key)
        assert not summary["session_alive"]
        assert summary["created_sessions"] == expected_sessions
        assert summary["completed_frames"] == (1 if failing else 3), summary
        checks = [inspect(root / f"{case}.{number:04d}.exr", number) for number in ((1,) if failing else (1, 2, 3))]
        if failing:
            assert not (root / f"{case}.0002.exr").exists()
            assert not (root / f"{case}.0003.exr").exists()
        if case == "persistent":
            statuses = [value["status"] for value in frames if value["case"] == case]
            assert [value["generation"] for value in statuses] == [1, 2, 3]
            assert [value["geometry_conversions"] for value in statuses] == [2, 3, 4], statuses
            if "coalesced_updates" in statuses[-1]:
                assert statuses[-1]["coalesced_updates"] == 0
                assert len({value["renderer_pid"] for value in statuses}) == 1
        assert hou.frame() == 1
        reports.append(dict(case=case, error=error, summary=summary, images=checks))

    # HOM callbacks are not saved in HIP files; OnLoaded must re-register them.
    rop.setParms(dict(source_mode="objects", camera=camera.path(), resx=128, persistent_data=1))
    path = rop.path()
    hou.hipFile.save(str(root / "final-sequence-reload.hiplc"))
    hou.hipFile.load(str(root / "final-sequence-reload.hiplc"), suppress_save_prompt=True)
    rop = hou.node(path)
    case = "after-load"
    rop.parm("picture").set(str(root / "after-load.$F4.exr"))
    rop.render(frame_range=(1, 3, 1))
    summary = rop.cachedUserData(final_render._summary_key)
    assert summary["created_sessions"] == 1 and summary["completed_frames"] == 3
    assert not final_render.active_sequences()
    reports.append(
        dict(
            case=case,
            summary=summary,
            images=[inspect(root / f"after-load.{number:04d}.exr", number) for number in (1, 2, 3)],
        )
    )
finally:
    registry.session_from_configuration = factory
    final_render.wait_for_completion = original_wait
    final_render.close_all()
    registry.close_all()

report = dict(
    result="passed",
    device=device,
    execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"),
    cases=reports,
    frames=frames,
    events=events,
    runtime=str(Path(final_render.__file__).resolve()),
    scope="Real native ROP sequences; analytic projection/depth/color, unchanged geometry retention, ordered output, cancellation/failure cleanup and HIP reload",
)
(root / "houdini-final-sequence-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", cases=len(reports), final_sessions=len(created))))
