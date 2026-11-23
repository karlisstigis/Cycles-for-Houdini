# SPDX-License-Identifier: Apache-2.0
"""Check the Cycles Render View's failed-job status without a Houdini window."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from support.package_loader import load_module, prepare_package

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "first_error,kind,headline",
    (
        (
            "Illegal address in CUDA queue copy_from_device",
            "illegal_address",
            "Render Stopped - Illegal GPU Memory Access.",
        ),
        ("System is out of GPU memory", "out_of_memory", "Render Stopped - Out of GPU Memory."),
    ),
)
def test_final_view_shows_fatal_gpu_warning_and_preserves_diagnostics(
    monkeypatch, tmp_path, first_error, kind, headline
):
    package = ModuleType("test_final_warning_fixture")
    prepare_package(package)
    monkeypatch.setitem(sys.modules, "hou", SimpleNamespace())
    package.rop.output = SimpleNamespace()
    package.scene.native_scene = SimpleNamespace()
    package.ipr.registry = SimpleNamespace()
    monkeypatch.setitem(sys.modules, package.__name__ + ".rop.background_jobs", SimpleNamespace(TERMINAL={"failed"}))
    try:
        final_render = load_module(package, "final_render")
        view_type = load_module(package, "viewer_final").FinalView
        view = view_type.__new__(view_type)
        panel = Mock()
        panel.canvas.isVisible.return_value = True
        panel.displaying_live.return_value = True
        panel._stop_requested = True
        view.show_mode = Mock()
        view.panel = panel
        view.rop = Mock()
        view.running = True
        view.entry = SimpleNamespace(session=Mock())
        view.entry.session.status.return_value = {"state": "failed", "progress": 0.25}
        details = "System is out of GPU and shared host memory"
        worker_log = tmp_path / "worker.log"
        worker_log.write_text(f"{first_error}\n{details}\n", encoding="utf-8")
        fault = final_render._render_gpu_failure(None, {"error": details, "worker_log": str(worker_log)}).info
        assert fault["kind"] == kind
        summary = {"gpu_failure": fault}
        view.entry.session.job_status.return_value = {
            "state": "failed",
            "error": fault["details"],
            "summary": summary,
            "total_frames": 2,
            "completed_frames": 1,
            "frame_index": 2,
        }

        view.poll()

        assert not view.running and not panel._stop_requested
        view.rop.setCachedUserData.assert_called_once_with(final_render._summary_key, summary)
        panel._record_saved.assert_not_called()
        panel.viewer_status.update_status.assert_called_once_with({"state": "failed", "progress": 0.625})
        panel.info.setText.assert_called_once()
        warning = panel.info.setText.call_args
        assert warning.args == (headline,)
        assert all(fault[key] in warning.kwargs["tooltip"] for key in ("first_error", "details", "worker_log"))
    finally:
        for name in tuple(sys.modules):
            if name == package.__name__ or name.startswith(package.__name__ + "."):
                sys.modules.pop(name, None)
