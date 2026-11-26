# SPDX-License-Identifier: Apache-2.0
"""Host dispatch of adaptive subdivision refinement after camera navigation."""

import atexit
import sys
from types import ModuleType, SimpleNamespace

import pytest

from support.package_loader import load_module, prepare_package


pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.fixture
def viewer(monkeypatch):
    now = 1.0
    clock = SimpleNamespace(value=100.0, monotonic=lambda: clock.value)
    mouse = SimpleNamespace(buttons=0)
    hou = SimpleNamespace(isUIAvailable=lambda: True)
    qt = SimpleNamespace(
        QtCore=SimpleNamespace(Qt=SimpleNamespace(NoButton=0)),
        QtWidgets=SimpleNamespace(QApplication=SimpleNamespace(mouseButtons=lambda: mouse.buttons)),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    monkeypatch.setitem(sys.modules, "PySide6", qt)
    package = ModuleType("test_camera_refinement_fixture")
    package.Session = object
    package._native = SimpleNamespace()
    package.synchronize_color_configuration = lambda: None
    prepare_package(package)
    package.scene.native_scene = SimpleNamespace(
        camera_signature=lambda update, at_time: update["camera_path"], light_basis=lambda *args: None
    )
    registry = load_module(package, "registry")
    monkeypatch.setattr(registry, "time", clock)
    monkeypatch.setattr(registry, "_poll_local_finals", lambda: None)
    uploads = []
    session = SimpleNamespace(state="rendering")
    session.status = lambda: {"state": session.state}
    session.apply = lambda update, **kwargs: uploads.append(dict(update)) or len(uploads)
    rop = SimpleNamespace(path=lambda: "/out/cycles", parm=lambda name: True, evalParm=lambda name: True)
    entry = registry.Entry("rop:1", ("CPU", 16, 16, 4), session, rop=rop)
    registry._entries[entry.key] = entry

    def camera_update(camera, **values):
        return entry.apply({"camera_path": camera, "motion": {}, **values}, now, change_serial=entry.change_serial)

    def poll(elapsed):
        clock.value += elapsed
        registry._last_texture_poll = clock.value
        registry._poll_host()

    try:
        yield SimpleNamespace(
            entry=entry, mouse=mouse, uploads=uploads, poll=poll, update=camera_update, time=now, session=session
        )
    finally:
        atexit.unregister(registry._shutdown_resources)
        for name in tuple(sys.modules):
            if name == package.__name__ or name.startswith(package.__name__ + "."):
                sys.modules.pop(name, None)


@pytest.fixture
def subdivided_viewer(viewer):
    geometry = [{"object_key": "obj", "geometry_key": "mesh", "subdivision": {"type": "linear"}}]
    viewer.update("start", geometry=geometry)
    return viewer


def test_release_refines_the_same_camera_without_requesting_a_scene_capture(subdivided_viewer):
    viewer = subdivided_viewer
    viewer.mouse.buttons = 1
    viewer.entry.changed(camera_only=True)
    viewer.update("drag")
    assert viewer.uploads[-1]["defer_camera_subdivision"]

    viewer.poll(0.04)
    assert viewer.entry.camera_refinement_pending
    assert viewer.entry.change_serial == viewer.entry.applied_serial

    viewer.mouse.buttons = 0
    viewer.poll(0.01)
    assert not viewer.entry.camera_refinement_pending
    assert viewer.entry.change_serial > viewer.entry.applied_serial
    assert viewer.entry.can_capture_camera_only({}, viewer.time)
    assert not viewer.entry.can_capture_camera_only({}, viewer.time + 1)

    viewer.update("drag")
    assert viewer.uploads[-1] == {"camera_path": "drag", "motion": {}, "defer_camera_subdivision": False}


@pytest.mark.parametrize("buttons", [0, 1])
def test_recent_motion_debounces_and_stationary_camera_refines_with_or_without_release(subdivided_viewer, buttons):
    viewer = subdivided_viewer
    viewer.mouse.buttons = buttons
    for edit in range(6):
        viewer.entry.changed(camera_only=True)
        viewer.update(f"position {edit}")
        viewer.poll(0.034)
        assert viewer.entry.camera_refinement_pending
        assert viewer.entry.change_serial == viewer.entry.applied_serial

    viewer.poll(0.115)
    assert viewer.entry.camera_refinement_pending
    viewer.poll(0.016)
    assert viewer.mouse.buttons == buttons
    assert not viewer.entry.camera_refinement_pending
    assert viewer.entry.can_capture_camera_only({}, viewer.time)
    viewer.update("position 5")
    assert not viewer.uploads[-1]["defer_camera_subdivision"]


def test_second_drag_supersedes_the_queued_refinement(subdivided_viewer):
    viewer = subdivided_viewer
    viewer.entry.changed(camera_only=True)
    viewer.update("first")
    viewer.poll(0.20)
    assert not viewer.entry.camera_refinement_pending

    viewer.mouse.buttons = 1
    viewer.entry.changed(camera_only=True)
    assert viewer.entry.can_capture_camera_only({}, viewer.time)
    viewer.update("second")
    assert viewer.uploads[-1] == {"camera_path": "second", "motion": {}, "defer_camera_subdivision": True}


def test_release_keeps_pending_scene_capture_and_stop(viewer):
    viewer.entry.changed()
    viewer.entry.camera_refinement_pending = True
    viewer.poll(0.20)
    assert viewer.entry.change_serial > viewer.entry.applied_serial
    assert not viewer.entry.camera_only_pending

    serial = viewer.entry.change_serial
    viewer.session.state = "cancelled"
    viewer.entry.camera_refinement_pending = True
    viewer.poll(0.20)
    assert not viewer.entry.camera_refinement_pending
    assert viewer.entry.change_serial == serial


@pytest.mark.parametrize("freeze", [False, True])
def test_camera_move_needs_no_refinement_with_dedicated_dicing_camera_or_freeze(viewer, freeze):
    viewer.entry.rop.evalParm = lambda name: not freeze
    dicing = {} if freeze else {"camera_path": "/obj/dicing"}
    geometry = [{"object_key": "obj", "geometry_key": "mesh", "subdivision": {"type": "linear"}}]
    viewer.update("start", geometry=geometry, dicing=dicing)
    viewer.entry.changed(camera_only=True)
    viewer.update("drag")
    assert not viewer.uploads[-1]["defer_camera_subdivision"]
    viewer.poll(0.20)
    assert viewer.entry.change_serial == viewer.entry.applied_serial
