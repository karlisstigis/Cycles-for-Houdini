# SPDX-License-Identifier: Apache-2.0
"""Artist world workflow: shelf, native tabs, HDRI updates and heavy SOP IPR."""

import os
from pathlib import Path
import runpy
import time
import traceback

import hou
import numpy as np
import OpenImageIO as OIIO
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from cycles_houdini import registry
from cycles_houdini.materials import create_world_material

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "65536"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="world_fixture")[
    "HeavyResponse"
]


class WorldWorkflow(HeavyResponse):
    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        self.events = []
        self.asset = self.path.with_suffix(".hdr.exr")
        self.write_texture(1)

    def write_texture(self, scale):
        image = np.empty((128, 256, 3), np.float32)
        image[:, :128] = (0.125 * scale, 0.25 * scale, 0.5 * scale)
        image[:, 128:] = (0.5 * scale, 0.125 * scale, 0.0625 * scale)
        spec = OIIO.ImageSpec(256, 128, 3, OIIO.FLOAT)
        spec.attribute("oiio:ColorSpace", "data")
        output = OIIO.ImageOutput.create(str(self.asset))
        assert output.open(str(self.asset), spec) and output.write_image(image) and output.close()
        stat = self.asset.stat()
        os.utime(self.asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

    def picture(self, name):
        for label, window in (
            ("render", self.panel.window()),
            ("parameters", hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel())),
        ):
            path = self.path.with_suffix("." + name + "." + label + ".png")
            assert window.screen().grabWindow(int(window.winId())).save(str(path))
            self.events.append(dict(name=name, window=label, screenshot=str(path)))

    def show_parameters(self, node, name=None):
        self.settings_pane.setCurrentNode(node)
        self.settings_pane.setPin(True)
        if name:
            parm = node.parm(name)
            parm.containingFolderSetParmTuples()[0][0].set(parm.containingFolderIndices()[0])

    def changed(self, name):
        self.before = self.panel.entry.session.status()["generation"]
        self.stage = name
        self.report["phase"] = name
        self.next_at = time.monotonic() + 0.4

    def open_environment_panel(self):
        # Native collapsible state is owned by the parameter editor. Open the
        # actual header after it has painted instead of setting a channel.
        window = hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel())
        window.show()
        window.raise_()
        window.activateWindow()
        before_image = window.screen().grabWindow(int(window.winId()))
        ratio = before_image.devicePixelRatio()
        point = QtCore.QPoint(round(28 / ratio), round(357 / ratio))
        target = window.childAt(point)
        assert target is not None
        # The OpenGL drawable only paints Houdini's controls. The enclosing
        # RE_Window owns native input (also used by the viewport handle tests).
        while target is not None and target.objectName() != "RE_Window":
            target = target.parentWidget()
        assert target is not None, "Native parameter input viewport was not found"
        before = self.panel.entry.session.status()["generation"]
        local = target.mapFrom(window, point)
        self.events.append(
            dict(
                name="native-panel-click",
                pixel_ratio=ratio,
                logical_size=[window.width(), window.height()],
                image_size=[before_image.width(), before_image.height()],
                window_ratio=window.devicePixelRatioF(),
                point=[point.x(), point.y()],
                target=dict(
                    type=type(target).__name__,
                    object=target.objectName(),
                    rectangle=[target.x(), target.y(), target.width(), target.height()],
                    local=[local.x(), local.y()],
                ),
                generation=before,
            )
        )
        before_image.save(str(self.path.with_suffix(".environment-before.parameters.png")))
        self.save()
        global_point = window.mapToGlobal(point)
        QtGui.QCursor.setPos(global_point)
        # Houdini draws native parameters in a custom viewport. Deliver the
        # same local/global mouse-event pair used by the native handle tests.
        for kind, button, buttons in (
            (QtCore.QEvent.MouseMove, QtCore.Qt.NoButton, QtCore.Qt.NoButton),
            (QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton),
            (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.LeftButton, QtCore.Qt.NoButton),
        ):
            event = QtGui.QMouseEvent(
                kind, QtCore.QPointF(local), QtCore.QPointF(global_point), button, buttons, QtCore.Qt.NoModifier
            )
            QtWidgets.QApplication.sendEvent(target, event)
            QtTest.QTest.qWait(40)
        self.native_panel_before = (before_image.toImage(), before)
        # Native Houdini actions can complete only after this callback returns.
        # Observe the visible result on a subsequent host event-loop turn.
        self.stage = "world-panel-open"
        self.report["phase"] = self.stage
        self.next_at = time.monotonic() + 0.4

    def verify_environment_panel(self):
        window = hou.qt.floatingPanelWindow(self.settings_pane.floatingPanel())
        before_image, generation = self.native_panel_before
        after_image = window.screen().grabWindow(int(window.winId()))
        after_image.save(str(self.path.with_suffix(".environment-after.parameters.png")))
        assert self.panel.entry.session.status()["generation"] == generation, "Opening a native panel reset tracing"
        assert before_image.copy(0, 390, 900, 500) != after_image.toImage().copy(0, 390, 900, 500), (
            "Native Environment header did not expand"
        )
        self.events.append(
            dict(
                name="native-panel-open",
                generation=generation,
                visible_parameters=[parm.name() for parm in self.settings_pane.visibleParms()],
            )
        )

    def retained(self, entry):
        status = entry.session.status()
        assert status["geometry_conversions"] == self.conversions
        assert status["displacement_rebuilds"] == self.displacement_builds
        assert status["renderer_pid"] == self.renderer_pid
        assert len(registry.entries()) == 1 and entry.views == 1
        self.events.append(dict(name=self.stage, status=status, diagnostics=entry.session.diagnostics()))

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 200:
                raise TimeoutError(self.stage + ": " + self.panel.info.text())
            entry = registry.get(self.key)
            if entry is None:
                return
            self.report["events"] = self.events
            self.report["current"] = dict(stage=self.stage, status=entry.session.status(), error=entry.error)
            self.save()
            if self.stage == "initial":
                if not self.ready(entry):
                    return
                assert self.report["fixture"]["mesh_triangles"] >= 1000000
                self.conversions = entry.session.status()["geometry_conversions"]
                self.displacement_builds = entry.session.status()["displacement_rebuilds"]
                self.renderer_pid = entry.session.status()["renderer_pid"]
                self.settings_pane = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
                self.settings_pane.setIsCurrentTab()
                # Execute the registered artist-facing shelf action in the viewport.
                self.scene.runShelfTool("cycles_create_environment")
                self.stage = "shelf-create"
                self.report["phase"] = self.stage
            elif self.stage == "shelf-create":
                selected = hou.selectedNodes()
                self.report["shelf_selection"] = [dict(path=node.path(), type=node.type().name()) for node in selected]
                # runShelfTool queues the action through Houdini's UI. Observe
                # its completed node selection on a subsequent event-loop turn.
                if not selected or selected[0].type().name() != "envlight":
                    return
                self.environment = selected[0]
                assert self.environment.type().icon() == "OBJ_light_environment"
                assert self.environment.parm("cycles_environment_colorspace") and not self.environment.parm(
                    "cycles_light_radius"
                )
                self.show_parameters(self.environment, "cycles_environment_colorspace")
                self.environment.setParms(dict(env_map=str(self.asset), cycles_environment_colorspace="data"))
                self.changed("native-environment")
            elif self.stage == "native-environment":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("native-environment")
                self.before_image = self.panel.canvas.image.copy()
                self.before = entry.session.status()["generation"]
                self.environment.parm("ry").set(90)
                self.changed("rotated-environment")
            elif self.stage == "rotated-environment":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("rotated-environment")
                assert self.panel.canvas.image != self.before_image
                QtTest.QTest.mouseClick(self.panel.snapshot_button, QtCore.Qt.LeftButton)
                assert len(self.panel._snapshots) == 1
                self.before = entry.session.status()["generation"]
                self.write_texture(2)
                self.stage = "disk-refresh"
                self.report["phase"] = self.stage
            elif self.stage == "disk-refresh":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("disk-refresh")
                assert len(self.panel._snapshots) == 1, "An automatic world edit created a snapshot"
                self.show_parameters(self.rop, "world_source")
                self.world = create_world_material(name="artist_world", texture=str(self.asset))
                self.world.node("environment_texture").parm("colorspace").set("data")
                self.rop.setParms(dict(world_source="material", world_material=self.world.path()))
                self.changed("world-material")
            elif self.stage == "world-material":
                if not self.ready(entry):
                    return
                self.open_environment_panel()
            elif self.stage == "world-panel-open":
                if time.monotonic() < self.next_at:
                    return
                self.verify_environment_panel()
                self.rop.updateParmStates()
                assert self.rop.parmTuple("background")[0].isDisabled(), (
                    "An unused fallback color remained active in World Material mode"
                )
                self.retained(entry)
                self.picture("world-material")
                builds = entry.session.diagnostics()["background_builds"]
                self.show_parameters(self.world.node("background_shader"))
                self.world.node("background_shader").parm("strength").set(0.25)
                self.changed("world-strength")
                self.previous_builds = builds
            elif self.stage == "world-strength":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("world-strength")
                assert entry.session.diagnostics()["background_builds"] > self.previous_builds
                self.previous_builds = entry.session.diagnostics()["background_builds"]
                self.show_parameters(self.rop, "world_source")
                self.rop.parm("world_sampling").set("none")
                self.changed("sampling-only")
            elif self.stage == "sampling-only":
                if not self.ready(entry):
                    return
                self.retained(entry)
                assert entry.session.diagnostics()["background_builds"] == self.previous_builds
                self.rop.parm("world_source").set("color")
                self.changed("constant-world")
            elif self.stage == "constant-world":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("constant-world")
                self.rop.parm("world_source").set("scene")
                self.environment.parm("light_enable").set(0)
                self.changed("disabled-environment")
            elif self.stage == "disabled-environment":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.rop.setParms(dict(source_mode="sop", soppath=self.obj.renderNode().path()))
                self.changed("sop-source")
            elif self.stage == "sop-source":
                if not self.ready(entry):
                    return
                self.conversions = entry.session.status()["geometry_conversions"]
                self.displacement_builds = entry.session.status()["displacement_rebuilds"]
                self.environment.parm("light_enable").set(1)
                self.changed("sop-enable-environment")
            elif self.stage == "sop-enable-environment":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.environment.parm("light_intensity").set(2)
                self.changed("sop-environment-intensity")
            elif self.stage == "sop-environment-intensity":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.environment.destroy()
                self.changed("sop-delete-environment")
            elif self.stage == "sop-delete-environment":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.environment = hou.node("/obj").createNode("envlight", "sop_new_environment")
                self.environment.parm("light_contribprimary").set(1)
                self.changed("sop-add-environment")
            elif self.stage == "sop-add-environment":
                if not self.ready(entry):
                    return
                self.retained(entry)
                self.picture("sop-add-environment")
                self.panel.dispose()
                registry.close_all()
                self.timer.stop()
                closed = entry.session.status()
                assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0
                self.report.update(result="passed", events=self.events, closed=closed)
                self.save()
                hou.ui.removeEventLoopCallback(self.callback)
                hou.ui.postEventCallback(lambda: hou.exit(0, suppress_save_prompt=True))
        except Exception:
            self.report.update(result="failed", events=self.events, error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            try:
                self.panel.dispose()
                registry.close_all()
            except Exception:
                pass
            hou.ui.postEventCallback(lambda: hou.exit(1, suppress_save_prompt=True))
        finally:
            self.tick_busy = False


workflow = WorldWorkflow()
