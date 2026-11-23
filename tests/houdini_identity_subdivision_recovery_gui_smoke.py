# SPDX-License-Identifier: Apache-2.0
"""Edit optional identities and subdivision metadata through native parameters in one live IPR."""

from support.paths import artifacts_root

from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
exec((root / "tests/identity_subdivision_fixture.py").read_text())


class Workflow:
    def __init__(self):
        hou.setFps(24)
        hou.setFrame(1)
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.fixtures = {kind: identity_fixture(kind) for kind in ("mesh", "points", "curves", "packed", "subdivision")}
        material = ch.create_material(name="recovery_surface", shader="emission")
        material.node("surface_shader").parmTuple("color").set((0.2, 0.7, 0.1))
        pass_fixture.parm(material.node("surface_shader"), "strength").set(1)
        for obj, node in self.fixtures.values():
            pass_fixture.parm(obj, "shop_materialpath").set(material.path())
            obj.layoutChildren()
        self.camera = hou.node("/obj").createNode("cam", "recovery_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=6, projection=1, orthowidth=6, shutter=1.0))
        self.rop = hou.node("/out").createNode("cycles_ipr", "identity_subdivision_recovery")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        configure_render(
            self.rop,
            dict(
                source_mode="objects",
                camera=self.camera.path(),
                resx=192,
                resy=96,
                samples=64,
                ipr_samples=64,
                geometry_motion="deformation",
                pass_position=1,
                point_radius=0.3,
                allowmotionblur=1,
                subdivision=dict(space="object", dicing_rate=0.05, max_level=6),
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 740))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.settings = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm, size=(650, 450))
        self.settings.setPin(True)
        self.cases = [(kind, mode) for kind in ("mesh", "points", "curves", "packed") for mode in (1, 2)] + [
            ("subdivision", 6),
            ("subdivision", 7),
        ]
        self.case = 0
        self.stage = "mount"
        self.panel = None
        self.started = time.monotonic()
        self.generation = 0
        self.capture_checks = -1
        self.next_at = 0
        self.report = dict(result="running", events=[])
        self.configure()
        example = artifacts_root() / "examples/identity-subdivision-recovery/identity-subdivision-recovery.hiplc"
        example.parent.mkdir(parents=True, exist_ok=True)
        hou.hipFile.save(str(example))
        hou.ui.addEventLoopCallback(self.tick)

    def configure(self):
        kind, value = self.cases[self.case]
        obj, self.node = self.fixtures[kind]
        configure_render(
            self.rop,
            dict(
                vobject=obj.path(),
                allowmotionblur=int(kind not in ("packed", "subdivision")),
                subdivision=dict(type="catmull_clark" if kind == "subdivision" else "none"),
            ),
        )
        self.parm = "mode"
        self.good = 0
        self.settings.setCurrentNode(self.node)
        # A source edit produces a fresh accepted frame even between cases
        # with identical ROP settings; no render restart is involved.
        pass_fixture.parm(self.node, self.parm).set(self.good)

    def edit(self, value, retry=False):
        self.input_attempts = (self.input_attempts + 1) if retry else 1
        self.pending = value
        self.settings.setIsCurrentTab()
        window = hou.qt.floatingPanelWindow(self.settings.floatingPanel())
        window.show()
        window.raise_()
        window.activateWindow()
        self.next_at = time.monotonic() + 0.25
        self.stage = "focus"

    def ready(self, stage):
        entry = self.panel.entry
        if not entry:
            return False
        status = entry.session.status()
        if entry.error or status["state"] == "failed":
            raise RuntimeError(entry.error or status["error"])
        info = entry.session.frame_info()
        if status.get("capture_pending"):
            return False
        self.report["last_wait"] = dict(
            stage=stage,
            status=status,
            info=info,
            presented=self.panel._live_generation,
            mode=pass_fixture.evaluate(self.node, "mode"),
        )
        if (
            not info
            or info["generation"] != status["generation"]
            or not info["complete"]
            or self.panel._live_generation != info["generation"]
        ):
            return False
        if (status["component_checks"] + status["cache_hits"] + status["geometry_conversions"]) <= self.capture_checks:
            return False
        f = entry.session.frame()
        p = f["planes"]["position"]
        positions = np.frombuffer(p["pixels"], np.float32).copy()
        rgba = np.frombuffer(f["planes"]["combined"]["pixels"], np.float32).reshape(-1, 4)
        assert np.count_nonzero(rgba[:, 1] > 0.1) > 100
        kind, value = self.cases[self.case]
        broken = stage == "broken"
        assert bool(status["source_diagnostics"]) == (broken and kind == "subdivision"), status
        assert status["object_resources"] == (4 if kind == "packed" else 1) and status["geometry_resources"] == 1, (
            status
        )
        if stage == "healthy":
            self.baseline = positions
            self.conversions = status["geometry_conversions"]
        if not np.array_equal(positions, self.baseline):
            np.savez(self.path.with_suffix(".npz"), baseline=self.baseline, actual=positions)
            self.panel.grab().save(str(self.path.with_suffix(".failure.png")))
            raise AssertionError(
                (
                    self.cases[self.case],
                    info["generation"],
                    status["generation"],
                    float(np.max(np.abs(positions - self.baseline))),
                    int(np.count_nonzero(positions != self.baseline)),
                    status,
                )
            )
        self.capture_checks = status["component_checks"] + status["cache_hits"] + status["geometry_conversions"]
        assert len(registry.entries()) == 1 and entry.views == 1
        assert not self.panel.render_button.isEnabled() and self.panel.stop_button.isEnabled()
        self.report["events"].append(
            dict(
                case=self.cases[self.case],
                stage=stage,
                status=status,
                input_attempts=getattr(self, "input_attempts", 0),
            )
        )
        self.generation = info["generation"]
        return True

    def tick(self):
        try:
            if time.monotonic() - self.started > 195:
                raise TimeoutError(
                    str(self.cases[self.case])
                    + ": "
                    + self.stage
                    + ": "
                    + (self.panel.info.text() if self.panel else "No panel")
                )
            if time.monotonic() < self.next_at:
                return
            if self.stage == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.stage = "healthy"
            elif self.stage == "healthy":
                if not self.ready("healthy"):
                    return
                self.edit(self.cases[self.case][1])
            elif self.stage == "focus":
                self.settings.moveFocusTo(self.parm)
                self.next_at = time.monotonic() + 0.2
                self.stage = "type"
            elif self.stage == "type":
                self.stage = "sending_keys"
                focus = QtWidgets.QApplication.focusWidget() or hou.qt.floatingPanelWindow(
                    self.settings.floatingPanel()
                )
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_A, QtCore.Qt.ControlModifier)
                QtTest.QTest.keyClicks(focus, str(self.pending))
                QtTest.QTest.keyClick(focus, QtCore.Qt.Key_Return)
                self.stage = "repaired" if self.pending == self.good else "broken"
                self.next_at = time.monotonic() + 0.3
            elif self.stage == "broken":
                if abs(pass_fixture.evaluate(self.node, self.parm) - self.pending) > 1e-6:
                    assert self.input_attempts < 3, (
                        self.cases[self.case],
                        self.parm,
                        self.pending,
                        pass_fixture.evaluate(self.node, self.parm),
                    )
                    self.edit(self.pending, retry=True)
                    return
                if not self.ready("broken"):
                    return
                self.panel.grab().save(str(self.path.with_name(self.path.stem + "-" + str(self.case) + ".png")))
                self.edit(self.good)
            elif self.stage == "repaired":
                if abs(pass_fixture.evaluate(self.node, self.parm) - self.good) > 1e-6:
                    assert self.input_attempts < 3, (
                        self.cases[self.case],
                        self.parm,
                        self.good,
                        pass_fixture.evaluate(self.node, self.parm),
                    )
                    self.edit(self.good, retry=True)
                    return
                if not self.ready("repaired"):
                    return
                self.case += 1
                if self.case == len(self.cases):
                    QtTest.QTest.mouseClick(self.panel.stop_button, QtCore.Qt.LeftButton)
                    self.finish(None)
                    return
                same_kind = self.cases[self.case][0] == self.cases[self.case - 1][0]
                if not same_kind:
                    self.configure()
                if same_kind:
                    self.edit(self.cases[self.case][1])
                else:
                    self.stage = "healthy"
            self.path.write_text(json.dumps(self.report, indent=2))
        except Exception:
            self.finish(traceback.format_exc())

    def finish(self, error):
        hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        self.report.update(result="failed" if error else "passed", error=error)
        self.path.write_text(json.dumps(self.report, indent=2))
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
