# SPDX-License-Identifier: Apache-2.0
"""Native Houdini Interrupt, ROP/background Cancel and successful restart."""

import json
import faulthandler
import os
from pathlib import Path
import shutil
from threading import Event, Thread
import time
import traceback

import hou
import OpenImageIO as oiio
from PySide6 import QtCore, QtWidgets
from cycles_houdini import background_render as bg, device_selection, final_render, passes, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.stack_log = self.path.with_suffix(".stacks.log").open("w")
        faulthandler.dump_traceback_later(30, repeat=True, file=self.stack_log)
        self.report = dict(result="running", checks=[], physical_mouse_certification=False)
        self.began = time.monotonic()
        self.busy = self.finished = False
        self.phase = "foreground-interrupt"
        self.cancel_mode = None
        self.sessions = []
        self.writer_mode = os.environ.get("CYCLES_HOUDINI_TEST_WRITER_INTERRUPT") == "1"
        if self.writer_mode:
            self.phase = "writer-prepare"
        self.writer_armed = False
        self.writer_retry = False
        self.external_interrupt = bool(os.environ.get("CYCLES_HOUDINI_NATIVE_INTERRUPT_EXTERNAL"))
        self.native_operation = hou.InterruptableOperation

        def operation(*args, **kwargs):
            if (
                self.cancel_mode == "native"
                and not self.external_interrupt
                and args
                and str(args[0]).startswith("Cycles final frame")
            ):
                # Houdini's documented timeout checks use the same native
                # interruption/cleanup path as Escape. The RE Interrupt panel
                # is not a regular Qt dialog. Physical input is certified in
                # the separate externally driven run, never faked with Qt.
                kwargs["timeout_ms"] = 300
                self.record_click()
            if self.writer_armed and args and args[0] == "Saving Cycles output":
                kwargs["timeout_ms"] = 300
                self.record_click()
            return self.native_operation(*args, **kwargs)

        hou.InterruptableOperation = operation
        self.factory = registry.session_from_configuration

        def record(*args, **kwargs):
            session = self.factory(*args, **kwargs)
            if kwargs.get("interactive") is False:
                assert session.execution == "worker" and session.disk_only
                self.sessions.append(session)

                def no_pixels(*args, **kwargs):
                    raise AssertionError("Final progress copied full image buffers")

                session.frame = no_pixels
                if self.writer_armed:
                    self.delay_writer_acknowledgement(session)
                if self.writer_retry:
                    self.writer_retry_session_started = time.monotonic()
            return session

        registry.session_from_configuration = record
        hou.putenv("CYCLES_HOUDINI_BACKGROUND_ROOT", str(self.path.parent / "Private Background Jobs"))
        geo = hou.node("/obj").createNode("geo", "cancel_subject", run_init_scripts=False)
        box = geo.createNode("box")
        box.setDisplayFlag(True)
        box.setRenderFlag(True)
        cam = hou.node("/obj").createNode("cam", "cancel_camera")
        cam.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "cancel_test")
        self.rop.setParms(
            dict(
                vobject=geo.path(),
                camera=cam.path(),
                resx=256,
                resy=192,
                samples=1048576,
                use_denoise=0,
                adaptive_sampling=0,
                render_threads=2,
                allowmotionblur=0,
                use_material_assignments=0,
                material_kind="emission",
                emission_strength=1,
                trange=1,
                persistent_data=1,
            )
        )
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        for parm in self.rop.parmTuple("f"):
            parm.deleteAllKeyframes()
        self.rop.parmTuple("f").set((1, 3, 1))
        self.timer = QtCore.QTimer(hou.qt.mainWindow())
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)
        # Schedule input from the existing render-progress hook: Houdini may
        # suppress arbitrary timers during a synchronous native ROP action.
        # Posted mouse input must still traverse the real Qt event dispatcher.
        self.observer = final_render.observe(self.progress)
        self.observer.__enter__()

    def delay_writer_acknowledgement(self, session):
        begin, finished, finish = session.begin_write_exr, session.write_finished, session.finish_write_exr

        def begin_write(filename, options):
            assert not self.writer_dispatches, "A second file was dispatched after the delayed acknowledgement"
            path = Path(filename)
            assert path == self.winner_path and not path.exists(), path
            # Publish a valid competing file after GUI preflight and before the
            # actual worker's Skip check, then hold only the reply's visibility.
            shutil.copyfile(self.writer_seed, path)
            self.writer_dispatches.append(path.name)
            self.writer_delay_end = time.monotonic() + 1.8
            self.writer_event = begin(filename, options)
            real, proxy, delay_end = self.writer_event, Event(), self.writer_delay_end
            proxy.error = None
            self.writer_proxy = proxy

            def wait_for_reply():
                deadline = time.monotonic() + 20.0
                try:
                    while not finished(real) and time.monotonic() < deadline:
                        real.wait(0.05)
                    if not finished(real):
                        raise TimeoutError("The native worker never acknowledged its real EXR write")
                    time.sleep(max(0.0, delay_end - time.monotonic()))
                except Exception as error:
                    proxy.error = error
                finally:
                    proxy.set()

            Thread(target=wait_for_reply, name="Cycles GUI fixture writer acknowledgement", daemon=True).start()
            return proxy

        def finish_write(proxy):
            assert proxy is self.writer_proxy and proxy.is_set()
            if proxy.error is not None:
                raise proxy.error
            result = finish(self.writer_event)
            self.writer_replied = True
            return result

        session.begin_write_exr = begin_write
        session.write_finished = lambda proxy: proxy.is_set()
        session.finish_write_exr = finish_write

    @staticmethod
    def writer_center_rgb(path):
        image = oiio.ImageInput.open(str(path))
        assert image is not None, oiio.geterror()
        try:
            spec = image.spec()
            pixels = image.read_image(format=oiio.FLOAT)
            assert pixels is not None, image.geterror()
            return [float(pixels[spec.height // 2, spec.width // 2, spec.channelnames.index(name)]) for name in "RGB"]
        finally:
            image.close()

    def writer_tick(self):
        if self.phase == "writer-prepare":
            self.writer_seed = self.path.parent / "WriterSeed.exr"
            passes.add(self.rop, "normal", name="N")
            self.rop.setParms(dict(samples=4, trange=0, picture=str(self.writer_seed), output_separate_passes=1))
            self.rop.parm("execute").pressButton()
            assert self.writer_seed.is_file()
            self.winner_bytes = self.writer_seed.read_bytes()
            center = self.writer_center_rgb(self.writer_seed)
            assert max(center) > 0.1, center
            self.writer_dispatches = []
            self.writer_replied = False
            self.winner_path = self.path.parent / "WriterRace.0001.exr"
            self.rop.setParms(
                dict(trange=1, picture=str(self.path.parent / "WriterRace.$F4.exr"), output_existing="skip")
            )
            self.writer_armed = True
            self.clicked = None
            try:
                self.rop.parm("execute").pressButton()
            except hou.OperationFailed:
                pass
            finally:
                self.writer_armed = False

            summary = self.rop.cachedUserData(final_render._summary_key)
            assert self.clicked is not None and time.monotonic() < self.writer_delay_end, summary
            assert summary["cancelled"] and summary["completed_frames"] == 0, summary
            assert not final_render.active_sequences()
            assert sorted(p.name for p in self.path.parent.glob("WriterRace*.exr")) == [self.winner_path.name]
            assert self.winner_path.read_bytes() == self.winner_bytes
            self.old_writer = self.sessions[-1]
            self.report["checks"].append(
                dict(case="interrupt-during-write", summary=summary, cancel_seconds=time.monotonic() - self.clicked)
            )

            # A different ROP reuses the exact destination while the old
            # acknowledgement is held. Its first submission must follow drain.
            other = hou.copyNodesTo((self.rop,), hou.node("/out"))[0]
            other.setParms(dict(output_existing="overwrite", emission_strength=0.25))
            other.parmTuple("f").set((1, 1, 1))
            retry_started = time.monotonic()
            self.writer_retry = True
            try:
                other.parm("execute").pressButton()
            finally:
                self.writer_retry = False
            retry = other.cachedUserData(final_render._summary_key)
            assert self.writer_replied and not self.writer_event.error, retry
            assert retry_started < self.writer_delay_end <= self.writer_retry_session_started, retry
            assert retry["completed_frames"] == 1 and not retry["error"] and not retry["cancelled"], retry
            assert (self.path.parent / "WriterRace.N.0001.exr").is_file()
            assert self.winner_path.read_bytes() != self.winner_bytes
            retry_center = self.writer_center_rgb(self.winner_path)
            assert all(abs(actual - 0.25 * before) < 0.002 for before, actual in zip(center, retry_center)), (
                center,
                retry_center,
            )
            self.report["checks"].append(dict(case="other-rop-same-output-retry", summary=retry))
            self.phase = "writer-retired"
        elif self.phase == "writer-retired" and self.old_writer.status()["state"] == "closed":
            assert not list(self.path.parent.glob(".cycles-*.tmp.exr"))
            self.finish()

    def progress(self, event, value):
        if event == "progress":
            self.press_cancel()

    def press_cancel(self):
        if self.cancel_mode is None or self.finished:
            return
        try:
            state = final_render._active.get(self.rop.sessionId())
            if state is None or state.session is None:
                return
            if self.cancel_mode == "rop" and state.completed_frames != 1:
                return
            metadata = state.session.status()
            if metadata["state"] in ("failed", "closed", "cancelled"):
                return
            windows = [w for w in QtWidgets.QApplication.topLevelWidgets() if w.isVisible()]
            assert not any(
                w.objectName() == "cycles_final_render_progress" or w.windowTitle().startswith("Cycles Render to Disk")
                for w in windows
            )
            assert state.session.frame_info() is None, "A disk-only render sent a display image to Houdini"
            self.cancel_metadata = metadata
            if self.cancel_mode == "native":
                if self.external_interrupt and not self.report.get("native_input_ready"):
                    self.report["native_input_ready"] = True
                    self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
                return
            self.cancel_mode = None
            self.record_click()
            self.rop.parm("ch_cancel_render").pressButton()
        except BaseException:
            self.callback_error = traceback.format_exc()
            final_render.request_cancel(self.rop)

    def record_click(self):
        self.clicked = time.monotonic()

    def check_closed(self):
        assert not final_render.active_sequences(), final_render.active_sequences()
        assert self.sessions and all(s.status()["state"] == "closed" for s in self.sessions)
        assert not any(
            w.isVisible() and w.objectName() == "cycles_final_render_progress"
            for w in QtWidgets.QApplication.topLevelWidgets()
        )

    def tick(self):
        if self.busy or self.finished:
            return
        self.busy = True
        try:
            self.report["phase"] = self.phase
            self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            assert time.monotonic() - self.began < (280 if self.external_interrupt else 110), self.phase
            if self.writer_mode:
                self.writer_tick()
                return
            if self.phase in ("foreground-interrupt", "foreground-rop"):
                preserve = self.phase == "foreground-rop"
                self.output = self.path.parent / (self.phase + ".$F4.exr")
                self.rop.parm("picture").set(str(self.output))
                if preserve:
                    self.rop.parm("samples").setExpression("if($F==1,4,1048576)", hou.exprLanguage.Hscript)
                self.cancel_mode = "rop" if preserve else "native"
                self.clicked = None
                self.callback_error = None
                error = None
                try:
                    self.rop.parm("execute").pressButton()
                except hou.OperationFailed as exception:
                    error = str(exception)
                assert not self.callback_error, self.callback_error
                if not preserve and self.external_interrupt:
                    marker = self.path.parent / "interrupt.sent"
                    assert marker.exists(), "External interrupt was not requested"
                    self.clicked = time.monotonic() - (time.time() - marker.stat().st_mtime)
                    self.report["physical_mouse_certification"] = marker.read_text().strip() == "mouse"
                    self.report["physical_keyboard_certification"] = marker.read_text().strip() == "keyboard"
                self.cancel_mode = None
                # The native Execute action may consume the SOHO interruption;
                # RopNode.render raises it. Verify the real lifecycle/output.
                assert self.clicked is not None
                summary = self.rop.cachedUserData(final_render._summary_key)
                self.report["last_summary"] = summary
                self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
                assert summary["cancelled"] and summary["reason"] == "cancelled", summary
                assert summary["completed_frames"] == int(preserve), summary
                self.check_closed()
                for frame in (1, 2, 3):
                    path = Path(str(self.output).replace("$F4", f"{frame:04d}"))
                    assert path.exists() == (preserve and frame == 1), path
                self.report["checks"].append(
                    dict(
                        case=self.phase,
                        summary=summary,
                        cancel_seconds=time.monotonic() - self.clicked,
                        operation_error=error,
                        status_at_cancel=self.cancel_metadata,
                    )
                )
                self.phase = "foreground-rop" if not preserve else "launch-background"
                return
            if self.phase == "launch-background":
                self.rop.parm("samples").deleteAllKeyframes()
                self.rop.parm("samples").set(1048576)
                self.output = self.path.parent / "Background.$F4.exr"
                self.rop.parm("picture").set(str(self.output))
                self.rop.parm("ch_render_background").pressButton()
                self.job = bg.last_job(self.rop)
                assert self.job is not None
                self.phase = "cancel-background"
                return
            if self.phase == "cancel-background":
                status = self.job.status()
                assert status["state"] not in bg._terminal, status
                if not status.get("generation"):
                    return
                self.rop.parm("ch_cancel_render").pressButton()
                self.clicked = time.monotonic()
                assert (self.job.directory / "stop").is_file()
                self.phase = "wait-background"
                return
            if self.phase == "wait-background":
                status = self.job.status()
                if status["state"] not in bg._terminal or self.job.poll() is None:
                    return
                assert status["state"] == "cancelled" and status["completed_frames"] == 0, status
                assert not list(self.path.parent.glob("Background.*.exr"))
                self.report["checks"].append(
                    dict(case="background-rop", status=status, cancel_seconds=time.monotonic() - self.clicked)
                )
                self.rop.setParms(dict(samples=4, trange=0, picture=str(self.path.parent / "Restart.exr")))
                self.rop.parm("execute").pressButton()
                summary = self.rop.cachedUserData(final_render._summary_key)
                assert not summary["error"] and not summary["cancelled"] and summary["completed_frames"] == 1, summary
                assert (self.path.parent / "Restart.exr").is_file()
                self.check_closed()
                self.report["checks"].append(dict(case="restart", summary=summary))
                self.finish()
        except BaseException:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.timer.stop()
        self.observer.__exit__(None, None, None)
        faulthandler.cancel_dump_traceback_later()
        self.stack_log.close()
        registry.session_from_configuration = self.factory
        hou.InterruptableOperation = self.native_operation
        if error and hasattr(self, "job") and self.job.poll() is None:
            self.job.stop()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.ui.addEventLoopCallback(self.quit)

    def quit(self):
        if self.quit in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.quit)
        hou.exit(1 if self.report["error"] else 0, suppress_save_prompt=True)


workflow = Workflow()
