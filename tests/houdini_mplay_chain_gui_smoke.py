# SPDX-License-Identifier: Apache-2.0
"""Progressive chained MPlay, saved EXRs, cancellation and recovery through native ROPs."""

import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
import OpenImageIO as oiio
from PySide6 import QtCore
from cycles_houdini import device_selection, registry
from cycles_houdini.rop import final_render, render_actions, output
from support.paths import artifacts_root

path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
output_root = artifacts_root() / "mp-chain" / str(os.getpid())
scene = os.environ.get("CYCLES_HOUDINI_MPLAY_SCENE")
if scene:
    hou.hipFile.load(scene, suppress_save_prompt=True, ignore_load_warnings=True)
    upstream, downstream = hou.node("/out/cam_1"), hou.node("/out/cam_2")
    assert upstream in downstream.inputs()
else:
    geo = hou.node("/obj").createNode("geo", "chain_subject", run_init_scripts=False)
    geo.createNode("sphere").parm("type").set(2)
    upstream = hou.node("/out").createNode("cycles_ipr", "cam_1")
    downstream = hou.node("/out").createNode("cycles_ipr", "cam_2")
    for index, rop in enumerate((upstream, downstream)):
        camera = hou.node("/obj").createNode("cam", f"chain_camera_{index}")
        camera.setParms(dict(tz=5, tx=index * 0.5, projection=1, orthowidth=4))
        rop.setParms(
            dict(
                vobject=geo.path(),
                camera=camera.path(),
                use_material_assignments=0,
                material_kind="emission",
                emission_strength=1,
            )
        )
        rop.parmTuple("basecolor").set((0.2 + index * 0.3, 0.4, 0.8))
    downstream.setInput(0, upstream)

device = os.environ["CYCLES_HOUDINI_GUI_DEVICE"]
nodes = (upstream, downstream)
tile_size = int(os.environ.get("CYCLES_HOUDINI_TEST_TILE_SIZE", "0"))
for rop in nodes:
    rop.setParms(
        dict(
            resx=96,
            resy=64,
            resolution_percent=100,
            samples=1048576 if not scene else 2,
            time_limit=2 if not scene else 0,
            adaptive_sampling=0,
            use_denoise=0,
            render_threads=2,
            allowmotionblur=0,
            trange=1,
            persistent_data=1,
            output_existing="overwrite",
        )
    )
    for parm in rop.parmTuple("f"):
        parm.deleteAllKeyframes()
    rop.parmTuple("f").set((1, 2, 1))
    device_selection.set_devices(rop, [device])
    if tile_size:
        rop.setParms(dict(use_tiling=1, tile_size=tile_size))

report = dict(result="running", scene=scene, device=device, tile_size=tile_size, previews=[], events=[], labels=[])
mode = "success"
cancel_at = None
original_progress = render_actions.MPlayOutput.progress
original_prepare = render_actions.MPlayOutput.prepare


def prepare(self, session, at_time):
    report["labels"].append(dict(rop=self.rop.path(), label=self.label, frame=hou.timeToFrame(at_time)))
    return original_prepare(self, session, at_time)


def progress(self, session, frame, generation):
    global cancel_at
    result = original_progress(self, session, frame, generation)
    if frame and frame["generation"] == generation and not frame["complete"]:
        record = dict(mode=mode, rop=self.rop.path(), generation=generation)
        if record not in report["previews"]:
            report["previews"].append(record)
        state = final_render._active.get(upstream.sessionId())
        if mode == "cancel" and self.rop == upstream and state and state.completed_frames == 1:
            cancel_at = time.monotonic()
            final_render.request_cancel(upstream)
    return result


def record(node, event, at_time):
    if event == hou.ropRenderEventType.PostFrame:
        report["events"].append((mode, node.name(), round(hou.timeToFrame(at_time))))


for node in nodes:
    node.addRenderEventCallback(record)


def outputs(stage):
    for node in nodes:
        node.parm("picture").set(str(output_root / stage / (node.name() + ".$F4.exr")))


def check_images(stage):
    written = []
    for node in nodes:
        for frame in (1, 2):
            for job in output.plan(node, hou.frameToTime(frame)):
                filename = job["filename"]
                image = oiio.ImageInput.open(filename)
                assert image is not None, filename
                assert (image.spec().width, image.spec().height) == (96, 64)
                if tile_size:
                    pixels = image.read_image(format=oiio.FLOAT)
                    assert np.isfinite(pixels).all(), filename
                    alpha = image.spec().channelnames.index("A")
                    assert np.all(pixels[:, :, alpha] > 0.999), (filename, "Missing tiled coverage")
                image.close()
                written.append(filename)
    report.setdefault("written_files", {})[stage] = written


def clean():
    assert not final_render.active_sequences(), final_render.active_sequences()
    assert not final_render._frame_outputs, final_render._frame_outputs


def run():
    global mode
    try:
        render_actions.MPlayOutput.progress = progress
        render_actions.MPlayOutput.prepare = prepare
        outputs("success")
        assert render_actions.render_mplay(downstream) is True
        clean()
        assert [(node, frame) for stage, node, frame in report["events"] if stage == "success"] == [
            ("cam_1", 1),
            ("cam_1", 2),
            ("cam_2", 1),
            ("cam_2", 2),
        ]
        assert len({row["label"] for row in report["labels"]}) == 2
        for node in nodes:
            summary = node.cachedUserData(final_render._summary_key)
            assert summary["completed_frames"] == 2 and not summary["error"], summary
        check_images("success")
        if not scene:
            assert {row["rop"] for row in report["previews"] if row["mode"] == "success"} == {
                n.path() for n in nodes
            }, report
            mode = "cancel"
            outputs("cancel")
            assert render_actions.render_mplay(downstream) is False
            assert cancel_at is not None
            report["cancel_return_seconds"] = time.monotonic() - cancel_at
            assert report["cancel_return_seconds"] < 3, report
            clean()
            summary = upstream.cachedUserData(final_render._summary_key)
            assert summary["cancelled"] and summary["completed_frames"] == 1, summary
            assert (output_root / "cancel/cam_1.0001.exr").is_file()
            assert not (output_root / "cancel/cam_1.0002.exr").exists()
            assert not (output_root / "cancel/cam_2.0001.exr").exists()
            report["upstream_cancel_without_traceback"] = True
            mode = "retry"
            outputs("retry")
            for node in nodes:
                node.setParms(dict(samples=2, time_limit=0))
            assert render_actions.render_mplay(downstream) is True
            clean()
            check_images("retry")
            report["same_session_retry"] = True
            # Native cancellation can also arrive during SOHO capture, before
            # render_frame has acquired its session and classified the error.
            from cycles_houdini.rop import soho_adapter

            original_update = soho_adapter._update

            def interrupt_capture(*args, **kwargs):
                raise hou.OperationInterrupted("Capture cancelled")

            try:
                soho_adapter._update = interrupt_capture
                assert render_actions.render_mplay(downstream) is False
                clean()
                assert upstream.cachedUserData(final_render._summary_key)["cancelled"]
                report["capture_cancel_without_traceback"] = True
            finally:
                soho_adapter._update = original_update
            # A real upstream output error must not be mistaken for cancellation.
            upstream.parm("output_existing").set("error")
            try:
                render_actions.render_mplay(downstream)
            except hou.OperationFailed:
                report["genuine_upstream_error_preserved"] = True
            else:
                raise AssertionError("Genuine upstream output failure was hidden")
            clean()
        report["result"] = "passed"
    except BaseException:
        report.update(
            result="failed",
            error=traceback.format_exc(),
            source_summaries={node.path(): node.cachedUserData(final_render._summary_key) for node in nodes},
        )
    finally:
        render_actions.MPlayOutput.progress = original_progress
        render_actions.MPlayOutput.prepare = original_prepare
        registry.close_all()
        path.write_text(json.dumps(report, indent=2))
        hou.ui.postEventCallback(lambda: hou.exit(0 if report["result"] == "passed" else 1, suppress_save_prompt=True))


hou.ui.postEventCallback(run)
