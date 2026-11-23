# SPDX-License-Identifier: Apache-2.0
"""Copy diagnostics: the plain-text report (viewer/viewer_diagnostics.py, pure formatting)."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


def load():
    package = ModuleType("viewer_diagnostics_package")
    package.__path__ = [str(VIEWER)]
    sys.modules["viewer_diagnostics_package"] = package
    spec = importlib.util.spec_from_file_location(
        "viewer_diagnostics_package.viewer_diagnostics", VIEWER / "viewer_diagnostics.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAMPLE = dict(
    time="2026-11-14 12:00:00",
    build=dict(
        host="host-1a2b", houdini="22.0.466", python="3.13.1", os="Windows 11", source_fingerprint="0123456789abcdef"
    ),
    display=dict(mode="gpu", detail="viewer in its own native window, image drawn by OpenGL"),
    viewers=[
        dict(
            this=True,
            state="visible",
            rop="/out/cycles",
            session="rop:7",
            display="gpu",
            polling=True,
            auto=True,
            render_mode=True,
            stopped=False,
            pending=False,
        ),
        dict(
            this=False,
            state="released",
            rop="",
            session="",
            display="gpu",
            polling=False,
            auto=None,
            render_mode=False,
            stopped=False,
            pending=False,
        ),
        dict(
            this=False,
            state="hidden",
            rop="/out/cycles",
            session="rop:7",
            display="gpu",
            polling=False,
            auto=True,
            render_mode=True,
            stopped=False,
            pending=True,
        ),
    ],
    sessions=[
        dict(
            key="rop:7",
            rop="/out/cycles",
            views=2,
            native_ipr=False,
            paused=False,
            state="rendering",
            backend_state="rendering",
            generation=5,
            committed_generation=4,
            pending_update=True,
            capture_pending=False,
            renderer_pid=4242,
            backend_alive=True,
            execution="worker",
            render_device_ids=["CPU"],
            denoiser="oidn_cpu",
            elapsed_seconds=1.5,
            first_image_seconds=0.3,
            current_sample=12,
            device_allocation_bytes=2**21,
            owned_geometry_bytes=2**20,
            retired_shader_resources=3,
            error="",
        ),
    ],
    workers=[dict(pid=4242, alive=True, age=3725.2, cpu=12.5, closed=False, closing=False, epoch=1, starting=False)],
    registry=dict(
        polling=True,
        callback_registered=True,
        retire_requests=0,
        retiring_sessions=1,
        retirement_polling=True,
        local_final=False,
        local_final_jobs=0,
    ),
    updates=[
        dict(
            kind="render",
            inferred=False,
            triggers=1,
            outcome="complete",
            generation=1,
            total=14.97,
            loading=3.39,
            first_image=1.46,
            render=11.58,
            host=0.31,
            parts=dict(cook=0.29, extract=0.03, transfer=0.12),
            counts=dict(
                object_updates=20784, adapter_full_object_updates=20784, adapter_pose_updates=0, motion_cooks=8
            ),
        ),
        dict(
            kind="edit",
            inferred=False,
            triggers=3,
            outcome="no change",
            generation=None,
            total=None,
            loading=None,
            first_image=None,
            render=None,
            host=None,
            parts={},
            counts={},
        ),
        dict(
            kind="frame",
            inferred=True,
            triggers=1,
            outcome="in progress",
            generation=2,
            total=None,
            loading=1.0,
            first_image=None,
            render=None,
            host=None,
            parts={},
            counts={},
        ),
    ],
    errors=["workers: boom"],
)


def test_report_lists_every_section_in_plain_text():
    text = load().format_report(SAMPLE)
    assert "\t" not in text
    lines = text.splitlines()
    assert lines[0] == "Cycles for Houdini diagnostics  2026-11-14 12:00:00"
    assert lines[1].startswith("Build: development build · host host-1a2b · source 0123456789ab · Houdini 22.0.466")
    assert lines[2].startswith("Display: gpu (")
    assert "Viewers: 3 (1 hidden, 1 released, 1 visible)" in text
    assert "1. this · visible · ROP /out/cycles · session rop:7" in text
    assert "IPR sessions: 1" in text and "generation 5/4 · pending yes" in text
    assert "worker pid 4242 alive yes · worker · devices CPU" in text
    assert "Renderer processes: 1" in text and "pid 4242 · alive yes · age 1:02:05 · CPU 12.50 s" in text
    assert "Registry: polling yes (callback yes) · retire requests 0 · retiring sessions 1" in text
    assert "Updates (last 3, newest last; seconds):" in text
    assert (
        "  render · complete · gen 1 · total 14.97 · loading 3.39 · first image 1.46 · render 11.58"
        " · cook 0.29 extract 0.03 transfer 0.12 · host capture 0.31 · objects 20784 · full objects 20784"
        " · poses 0 · motion cooks 8"
    ) in lines
    assert "  edit x3 · no change · gen - · total - · loading - · first image - · render -" in lines
    assert "  frame (seen late) · in progress · gen 2 · total - · loading 1.00" in text
    assert lines[-1] == "Not collected: workers: boom"


def test_empty_report_is_still_well_formed():
    text = load().format_report(dict(time="t"))
    assert "Viewers: 0" in text and "IPR sessions: 0" in text and "Renderer processes: 0" in text
    assert "Updates (last 0, newest last; seconds):" in text


def test_process_times_without_a_process_is_unknown():
    assert load().process_times(None) == (None, None)
