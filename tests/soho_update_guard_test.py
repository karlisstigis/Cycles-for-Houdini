# SPDX-License-Identifier: Apache-2.0
"""SOHO iterator semantics and conservative no-op/camera-only update guards."""

from support.package_loader import load_module
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from support.paths import PACKAGE

package = ModuleType("soho_guard_test_host")
package.__path__ = [str(PACKAGE / "rop")]
package.Session = object
package._native = SimpleNamespace()
package.synchronize_color_configuration = lambda: None
package.geometry_request = package.matrix_from_houdini = lambda value: value
dirty = []
deleted = []
soho = ModuleType("soho")
soho.objectList = lambda name: (value for value in (deleted if name == "objlist:deletedinstance" else dirty))


class FullSceneCapture(Exception):
    pass


package.native_scene = SimpleNamespace(
    scene_lighting_configuration=Mock(side_effect=FullSceneCapture),
    configuration=lambda rop: ("CPU", 96, 72),
    session_identity=lambda config: config,
    light_capture=Mock(return_value=(dict(lights=[dict(strength=4.0)]), "lights only")),
)
package.final_render = SimpleNamespace()
passes = ModuleType(package.__name__ + ".passes")
passes.configuration = passes.custom_configuration = lambda *args: None
saved = {name: sys.modules.get(name) for name in ("hou", "soho", "sohog")}
sys.modules.update(
    {
        package.__name__: package,
        passes.__name__: passes,
        "hou": ModuleType("hou"),
        "soho": soho,
        "sohog": ModuleType("sohog"),
    }
)
try:
    registry = load_module(package, "registry")
    module = load_module(package, "soho_adapter")
    update = dict(camera=dict(transform=[1.0, 0.0, 0.0]), motion=dict(enabled=False), crop=(0, 0, 96, 72))
    rop = SimpleNamespace(sessionId=lambda: 1)
    entry = registry.Entry(
        "rop:1",
        ("CPU", 96, 72),
        SimpleNamespace(status=lambda: dict(state="rendering")),
        channel="open",
        rop=rop,
        observer=SimpleNamespace(deleted_sources=()),
        last_motion=update["motion"],
    )
    registry.get = lambda key: entry if key == entry.key else None
    entry.last_soho["camera_update"] = update

    def reuse(camera_update=update, config=entry.config, now=0.0, mode="update"):
        return module._reuse_native_update(entry, config, now, camera_update, mode)[0]

    assert reuse(), "An empty SOHO generator was treated as a changed scene"
    assert not reuse(now=1.0), "Frame changes must evaluate the scene"
    assert not reuse(config=("CPU", 192, 72)), "Resolution changes must rebuild configuration"
    assert not reuse(mode="generate"), "An explicit start must not be skipped"
    assert not reuse(camera_update=dict(update, crop=(0, 0, 48, 72)))
    assert not reuse(camera_update=dict(update, camera=dict(transform=[1.1, 0.0, 0.0])))
    entry.change_serial += 1
    assert not reuse(), "Host shader/object notifications must not be ignored"
    entry.change_serial -= 1
    dirty.append(object())
    assert not reuse()
    dirty.clear()
    deleted.append(object())
    assert not reuse()
    deleted.clear()
    entry.error = "rejected"
    assert not reuse()
    entry.error = ""
    entry.session.status = lambda: dict(state="cancelled")
    assert not reuse(), "Stopped renders must be restartable"
    entry.session.status = lambda: dict(state="rendering")
    entry.session.status = lambda: dict(state="rendering", color_update_pending=True)
    assert not reuse(), "A pending working-space change must reach the renderer"
    entry.session.status = lambda: dict(state="rendering")

    # A camera-only update must consume the empty generators, avoid scene
    # traversal, and return independent root ownership for Entry.apply mutation.
    entry.changed(camera_only=True)
    camera_only = module._update(rop, None, 0.0, 96, 72, "update", update)
    assert camera_only == update and camera_only is not update
    camera_only["defer_camera_subdivision"] = True
    assert "defer_camera_subdivision" not in update
    package.native_scene.light_capture.assert_not_called()
    package.native_scene.scene_lighting_configuration.assert_not_called()

    def expect_full(camera_update=update, now=0.0):
        full_capture = package.native_scene.scene_lighting_configuration
        full_capture.reset_mock()
        try:
            module._update(rop, None, now, 96, 72, "update", camera_update)
        except FullSceneCapture:
            full_capture.assert_called_once_with(rop, now, camera_update["motion"], (), interactive=True)
        else:
            raise AssertionError("SOHO incorrectly skipped the full scene capture")

    expect_full(dict(update, motion=dict(enabled=True)))
    expect_full(now=1.0)
    dirty.append(object())
    expect_full()
    dirty.clear()
    deleted.append(object())
    expect_full()
    deleted.clear()

    # A light-only notification may bypass the full read, but a time, shutter,
    # dirty-object or deletion change still requires an authoritative capture.
    entry.applied_serial = entry.change_serial
    entry.changed(light_value=True)
    package.native_scene.scene_lighting_configuration.reset_mock()
    light_only = module._update(rop, None, 0.0, 96, 72, "update", update)
    assert light_only == dict(update, lights=[dict(strength=4.0)], _narrow="lights")
    package.native_scene.scene_lighting_configuration.assert_not_called()
    package.native_scene.light_capture.assert_called_once_with(rop, 0.0, update["motion"], entry.light_basis)
    expect_full(dict(update, motion=dict(enabled=True)))
    expect_full(now=1.0)
    dirty.append(object())
    expect_full()
    dirty.clear()
    deleted.append(object())
    expect_full()
    deleted.clear()
    package.native_scene.light_capture.assert_called_once()
finally:
    for name, previous in saved.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
print("SOHO update guard: reuse, restart, camera/light-only captures and full-scene fallbacks passed")
