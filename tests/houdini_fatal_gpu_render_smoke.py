# SPDX-License-Identifier: Apache-2.0
"""Inject a fatal GPU reply at a real CPU final worker, without a display or GPU fault."""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import hou
import OpenImageIO as oiio
from cycles_houdini import _native
from cycles_houdini.core import device_selection
from cycles_houdini.ipr import queued, registry
from cycles_houdini.rop import batch_render, final_render, render_actions
from support.paths import artifacts_root


def create_scene(destination):
    obj = hou.node("/obj").createNode("geo", "fault_subject", run_init_scripts=False)
    obj.createNode("box").setRenderFlag(True)
    camera = hou.node("/obj").createNode("cam", "fault_camera")
    camera.setParms({"tz": 5, "projection": 1, "orthowidth": 4})
    camera.parm("tx").setExpression("0.1*$F", hou.exprLanguage.Hscript)
    output = hou.node("/out")
    upstream = output.createNode("cycles_ipr", "upstream")
    downstream = output.createNode("cycles_ipr", "downstream")
    for rop in (upstream, downstream):
        rop.setParms(
            {
                "camera": camera.path(),
                "vobject": obj.path(),
                "use_material_assignments": 0,
                "material_kind": "emission",
                "emission_strength": 1,
                "transparent_background": 1,
                "resx": 32,
                "resy": 24,
                "samples": 2,
                "adaptive_sampling": 0,
                "use_denoise": 0,
                "render_threads": 2,
                "allowmotionblur": 0,
                "trange": 1,
                "persistent_data": 1,
                "output_existing": "overwrite",
                "picture": str(destination / (rop.name() + ".$F4.exr")),
            }
        )
        rop.parmTuple("basecolor").set((0.4, 0.2, 0.1))
        for parm in rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        rop.parmTuple("f").set((1, 2, 1))
        device_selection.set_devices(rop, "CPU")
    downstream.setInput(0, upstream)
    scene = destination / "fixture.hiplc"
    hou.hipFile.save(str(scene), save_to_recent_files=False)
    return scene, downstream.path()


def verify_image(path):
    image = oiio.ImageInput.open(str(path))
    assert image, path
    try:
        assert (image.spec().width, image.spec().height) == (32, 24)
        pixels = image.read_image(format=oiio.FLOAT)
        assert pixels is not None, path
        alpha = image.spec().channelnames.index("A")
        assert pixels[12, 16, alpha] > 0.99 and pixels[0, 0, alpha] < 0.01, path
    finally:
        image.close()


def run(destination):
    scene, rop_path = create_scene(destination)
    original_session = registry.Session
    original_apply = queued.ExternalBackend.apply
    workers = []
    sessions = []
    submissions = 0
    first_error = "Illegal address in CUDA queue copy_from_device (integrator_intersect_shadow)"
    later_error = "System is out of GPU and shared host memory"
    fault = (2, first_error)

    def cpu_worker(**kwargs):
        assert kwargs["device"] == ("CPU",), kwargs
        kwargs.update(execution="worker", disk_only=not kwargs.get("progressive_display"))
        session = original_session(**kwargs)
        sessions.append(session)
        return session

    def apply(backend, desired, acknowledged):
        nonlocal submissions
        if backend.process not in workers:
            workers.append(backend.process)
        reply = original_apply(backend, desired, acknowledged)
        submissions += 1
        if fault and submissions == fault[0]:
            backend.log.write(f"{fault[1]}\n{later_error}\n".encode())
            backend.log.flush()
            reply["status"] = dict(reply["status"], state="failed", message=later_error, error=later_error)
        return reply

    def cli(label):
        path = destination / (label + ".json")
        code = batch_render.main([str(scene), "--rop", rop_path, "--start", "1", "--end", "2", "--report", str(path)])
        return code, json.loads(path.read_text(encoding="utf-8"))

    with patch.object(registry, "Session", cpu_worker), patch.object(queued.ExternalBackend, "apply", apply):
        code, failed = cli("failure")
        assert code != 0 and failed["result"] == "failed", failed
        upstream = hou.node("/out/upstream").cachedUserData(final_render._summary_key)
        assert upstream["completed_frames"] == 1 and upstream["reason"] == "failed" and not upstream["cancelled"], (
            upstream
        )
        failure = upstream["gpu_failure"]
        assert failure["kind"] == "illegal_address" and failure["first_error"] == first_error, failure
        assert failure["details"] == later_error and first_error in Path(failure["worker_log"]).read_text(), failure
        assert len(workers) == 1 and workers[0].poll() is not None
        assert not final_render.active_sequences()
        verify_image(destination / "upstream.0001.exr")
        assert {path.name for path in destination.glob("*.exr")} == {"upstream.0001.exr"}
        assert not list(destination.glob(".cycles-*.tmp*"))

        fault = None
        code, recovered = cli("retry")
        assert code == 0 and recovered["result"] == "passed", recovered
        assert len(workers) == 3 and workers[0].pid != workers[1].pid
        assert all(worker.poll() is not None for worker in workers)
        assert not final_render.active_sequences()
        for name in ("upstream", "downstream"):
            for frame in (1, 2):
                verify_image(destination / f"{name}.{frame:04}.exr")

        # Houdini imports the SOHO API while executing the first ROP.
        from cycles_houdini.rop import soho_adapter

        rop = hou.node("/out/upstream")
        rop.parm("picture").set(str(destination / "rop.$F4.exr"))
        fault = (submissions + 1, first_error)
        graphical_soho = Mock(wraps=hou)
        graphical_soho.isUIAvailable.return_value = True
        with patch.object(soho_adapter, "hou", graphical_soho):
            try:
                rop.render(frame_range=(1, 1, 1))
            except hou.OperationFailed as error:
                rop_error = "\n".join((str(error), *rop.errors()))
            else:
                raise AssertionError("The graphical ROP reported success after a fatal error")
        assert "Render Stopped - Illegal GPU Memory Access." in rop_error and "Traceback" not in rop_error, rop_error
        assert first_error not in rop_error and later_error not in rop_error, rop_error
        assert not list(destination.glob("rop.*")) and not final_render.active_sequences()

        mplay = []
        warnings = []
        display_available = True
        real_hou = render_actions.hou
        ui = SimpleNamespace(setStatusMessage=lambda message, severity: warnings.append((message, severity)))
        ui_hou = SimpleNamespace(
            isUIAvailable=lambda: True,
            ui=ui,
            timeToFrame=real_hou.timeToFrame,
            severityType=SimpleNamespace(Message="message", Error="error"),
            OperationFailed=real_hou.OperationFailed,
            OperationInterrupted=real_hou.OperationInterrupted,
        )

        def display_status(handle, message):
            session = sessions[-1]
            assert handle is session._handle
            assert session.status()["state"] != "closed", "Warning arrived after retiring the display session"
            mplay.append(message)
            return display_available

        def no_display(*args, **kwargs):
            pass  # Keep the real ROP, worker and output ownership without opening MPlay.

        with (
            patch.object(render_actions, "hou", ui_hou),
            patch.object(queued.WorkerSession, "open_native_display", no_display),
            patch.object(queued.WorkerSession, "native_display_ready", lambda _: display_available),
            patch.object(queued.WorkerSession, "present_native", no_display),
            patch.object(_native, "set_native_display_status", display_status, create=True),
        ):
            rop.parm("picture").set(str(destination / "mplay.$F4.exr"))
            fault = (submissions + 1, later_error)
            assert render_actions.render_mplay(rop) is False
            assert mplay == ["Render Stopped - Out of GPU Memory."] and not warnings, (mplay, warnings)
            mplay_oom = rop.cachedUserData(final_render._summary_key)
            assert mplay_oom["gpu_failure"]["kind"] == "out_of_memory", mplay_oom
            assert not list(destination.glob("mplay.*")) and not final_render.active_sequences()

            display_available = False
            fault = (submissions + 1, first_error)
            assert render_actions.render_mplay(rop) is False
            assert mplay == ["Render Stopped - Out of GPU Memory.", "Render Stopped - Illegal GPU Memory Access."], (
                mplay
            )
            assert warnings == [(mplay[1], "error")], warnings
            mplay_illegal = rop.cachedUserData(final_render._summary_key)
            assert mplay_illegal["gpu_failure"]["kind"] == "illegal_address", mplay_illegal
            assert not list(destination.glob("mplay.*")) and not final_render.active_sequences()

            fault = None

            def cancel_on_progress(event, _):
                if event == "progress":
                    final_render.request_cancel(rop)

            with final_render.observe(cancel_on_progress):
                assert render_actions.render_mplay(rop) is False
            cancelled = rop.cachedUserData(final_render._summary_key)
            assert cancelled["cancelled"] and not cancelled.get("gpu_failure"), cancelled
            assert len(mplay) == 2 and warnings[-1][1] == "message", (mplay, warnings)
            assert not list(destination.glob("mplay.*")) and not final_render.active_sequences()
            assert not list(destination.glob(".cycles-*.tmp*"))

            rop.setParms({"picture": str(destination / "upstream.$F4.exr"), "output_existing": "error"})
            try:
                render_actions.render_mplay(rop)
            except hou.OperationFailed as error:
                assert "already exists" in str(error), error
            else:
                raise AssertionError("An ordinary output error was hidden")
            assert len(mplay) == 2 and len(warnings) == 2
            assert not final_render.active_sequences()

    return {
        "result": "passed",
        "worker_pids": [worker.pid for worker in workers],
        "failure": upstream,
        "retry": recovered,
        "rop_warning": rop_error,
        "mplay_delivery": mplay,
        "houdini_messages": warnings,
        "cancelled": cancelled,
    }


parser = argparse.ArgumentParser()
parser.add_argument("--report", type=Path)
args = parser.parse_args()
report = args.report or artifacts_root() / "stage3/fatal-gpu-render/report.json"
report.parent.mkdir(parents=True, exist_ok=True)
destination = report.parent / str(time.time_ns())
destination.mkdir()
report.write_text(json.dumps(run(destination), indent=2), encoding="utf-8")
