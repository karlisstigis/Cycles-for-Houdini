# SPDX-License-Identifier: Apache-2.0
"""Responsive viewer ownership of an ordered Houdini batch render."""

from ..rop import final_render
from ..scene import native_scene
from ..ipr import registry


class FinalView:
    def __init__(self, panel, rop, pause_token=None, previous_ipr=None):
        self.panel, self.rop = panel, rop
        self.pause_token = pause_token
        self.previous_ipr = previous_ipr
        self.entry = None
        self.running = True
        self.owned = False
        self.disposed = False
        self.error = ""
        self.job = None
        self.picture = rop.evalParm("picture")
        self.percent = rop.evalParm("resolution_percent")
        self.denoise = bool(rop.evalParm("use_denoise"))
        self.show_mode(1)

    def show_mode(self, index):
        menu = self.panel.mode_menu
        if menu.currentIndex() != index:
            blocked = menu.blockSignals(True)
            try:
                menu.setCurrentIndex(index)
            finally:
                menu.blockSignals(blocked)
        if index == 1:
            resolution = self.panel.preview_resolution
            text = str(self.percent) + "%"
            if resolution.currentText() != text:
                blocked = resolution.blockSignals(True)
                try:
                    resolution.setCurrentText(text)
                finally:
                    resolution.blockSignals(blocked)
            denoise = self.panel.preview_denoise
            blocked = denoise.blockSignals(True)
            try:
                denoise.setChecked(self.denoise)
            finally:
                denoise.blockSignals(blocked)

    def run(self):
        p = self.panel
        session = None
        try:
            self.rop.setCachedUserData(final_render._summary_key, None)
            from ..rop import background_render
            from ..rop.final_stream import RemoteSession

            self.job = background_render.launch(self.rop, viewer=True, _pause_token=self.pause_token)
            spec = background_render._read(self.job.directory / "job.json")
            session = RemoteSession(self.job, spec["viewer"])
            self.entry = registry.Entry(
                "viewer-final", native_scene.configuration(self.rop, interactive=False), session
            )
            self.owned = True
            p._attach(self.entry)
        except Exception as error:
            self.running = False
            self.error = str(error)
            if self.job is not None:
                self.job.stop()  # Keep the pause lease until the child actually terminates.
            elif self.pause_token is not None:
                registry.end_local_final(self.pause_token)
            # Registration can fail after the remote channel exists but before
            # an Entry/panel takes it. Do not strand its display thread or child.
            if session is not None:
                session.close()
                self.owned = False
            if not self.disposed:
                p.notify("Final render stopped: " + self.error)
        finally:
            if not self.disposed:
                p._in_render = False
                p._render_mode = False
                p._stopped = True
                p._stop_requested = False
                p._pending = p._render_queued = False
                p.update_render_controls()
                p.update_display_controls()

    def poll(self):
        p = self.panel
        self.show_mode(1)
        if self.entry is None:
            p.update_render_controls()
            return
        status = self.entry.session.status()
        p.poll_texture_inputs()
        job = self.entry.session.job_status()
        from ..rop.background_jobs import TERMINAL

        if self.running and job["state"] in TERMINAL:
            self.running = False
            self.error = job.get("error", "")
            p._stop_requested = False
            if job.get("summary") is not None:
                self.rop.setCachedUserData(final_render._summary_key, job["summary"])
            if job["state"] == "completed" and job.get("completed_frames"):
                p._record_saved(self.rop, job.get("last_picture", self.picture))
        p.refresh_color_policy()
        self.entry.session.set_feedback(id(p), p.canvas.isVisible() and p.displaying_live())
        p.present_available()
        p.update_render_controls(status)
        p.update_display_controls()
        p.update_save_control(status)
        count = job.get("total_frames", 1)
        done = job.get("completed_frames", 0) + job.get("skipped_frames", 0)
        frame_progress = (
            0.0
            if job.get("message") == "Cooking and capturing scene" or job.get("frame_index", 1) <= done
            else status["progress"]
        )
        overall = (
            1.0 if job["state"] == "completed" else min(1.0, (done + (frame_progress if done < count else 0)) / count)
        )
        p.viewer_status.update_status(dict(status, progress=overall))
        gpu_failure = (job.get("summary") or {}).get("gpu_failure") if job["state"] == "failed" else None
        if gpu_failure is not None:
            tooltip = gpu_failure["details"]
            first_error = gpu_failure.get("first_error")
            if first_error and first_error not in tooltip:
                tooltip = f"{first_error}\n\n{tooltip}"
            if gpu_failure.get("worker_log"):
                tooltip += "\n\nWorker log\n" + gpu_failure["worker_log"]
            p.info.setText(gpu_failure["message"], tooltip=tooltip)
            return
        message = self.error or status["error"] or status["message"]
        if not self.running and job["state"] == "completed":
            message = "Finished"
        if self.running and status["state"] == "complete":
            message = job.get("message") if job.get("message") == "Cooking and capturing scene" else "Writing output"
        if self.running and self.entry.session.stop_requested:
            message = "Stopping render"
        frame = job.get("frame")
        frame_text = f"{frame:.4f}".rstrip("0").rstrip(".") if frame is not None else ""
        label = f"Frame {frame_text} ({job.get('frame_index', 1)}/{count}) · " if frame is not None else ""
        from .viewer_timing import frame_seconds, text as timing_text

        timing = timing_text(frame_seconds(job), job.get("previous_frame_seconds"))
        p.info.setText(f"{status['state'].title()} · {overall:.1%} · {timing} · {label}Final Render · {message}")

    def cancel(self):
        if self.entry is not None and self.running:
            self.entry.session.cancel()

    def close(self):
        self.disposed = True
        if self.previous_ipr is not None:
            registry.detach(self.previous_ipr)
            self.previous_ipr = None
        try:
            if not self.panel._disposed:
                self.panel.sync_preview_controls()
                menu = self.panel.mode_menu
                blocked = menu.blockSignals(True)
                try:
                    menu.setCurrentIndex(0 if self.panel.preview_interactive.isChecked() else 1)
                finally:
                    menu.blockSignals(blocked)
        finally:
            if self.owned and self.entry is not None:
                self.entry.session.close()
                self.owned = False
