# SPDX-License-Identifier: Apache-2.0
"""Viewer time from the artist's action, cached ROP lookup and Copy diagnostics (temp scene).

- The status time counts from Render / an edit: while nothing samples it says
  "Loading", a finished render keeps its total, and that total matches the
  measured edit-to-complete time; the renderer's pure render time stays in the
  tooltip.
- An idle viewer does not look its ROP up on every poll; renaming, deleting
  the ROP and clearing the scene still reach the viewer at once.
- Copy diagnostics puts a plain-text report on the clipboard without a cook
  or a new render.
"""

import json
import os
from pathlib import Path
import re
import time
import traceback

import hou
from PySide6 import QtCore, QtTest, QtWidgets
import cycles_houdini as ch
from cycles_houdini import device_selection, menu_actions, registry


def shown_seconds(text):
    match = re.search(r"Time: (?:(\d+):)?(\d+):(\d+\.\d+)", text or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


class Smoke:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.report = dict(result="running", scope=__doc__, checks=[], updates=[])
        self.root = hou.node("/obj").createNode("subnet", "update_time")
        self.obj = self.root.createNode("geo", "subject", run_init_scripts=False)
        sphere = self.obj.createNode("sphere")
        sphere.setParms(dict(type=2, rows=120, cols=120))
        camera = self.root.createNode("cam", "camera")
        camera.parm("tz").set(5)
        material = ch.create_material(name="update_time_diffuse", shader="diffuse_bsdf")
        self.shader = material.node("surface_shader")
        self.obj.parm("shop_materialpath").set(material.path())
        self.rop = menu_actions.create_rop()
        self.rop.setParms(
            dict(
                objectroot=self.root.path(),
                vobject="*",
                lights="",
                camera=camera.path(),
                resx=320,
                resy=180,
                ipr_resolution_percent=0,
                ipr_samples=24,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                render_threads=2,
                allowmotionblur=0,
                world_source="color",
                use_texture_cache=0,
            )
        )
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(1000, 700))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.key = f"rop:{self.rop.sessionId()}"
        self.panel = None
        self.lookups = 0
        original = hou.nodeBySessionId

        def counted(identity, original=original):
            self.lookups += 1
            return original(identity)

        hou.nodeBySessionId = counted
        self.steps = [
            ("render", self.press_render),
            ("material edit", self.edit_material),
            ("transform edit", self.edit_transform),
        ]
        self.index = 0
        self.case = None
        self.phase = "mount"
        self.started = time.monotonic()
        self.busy = self.finished = False
        hou.ui.addEventLoopCallback(self.tick)

    def check(self, text):
        self.report["checks"].append(text)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def press_render(self):
        QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)

    def edit_material(self):
        self.shader.parmTuple("color").set((0.7, 0.25, 0.1))

    def edit_transform(self):
        self.obj.parm("tx").set(0.1)

    def status(self):
        entry = registry.get(self.key)
        return entry.session.status() if entry else None

    def headline(self):
        return self.panel.info.text().split("\n", 1)[0]

    def begin(self):
        label, action = self.steps[self.index]
        status = self.status()
        self.case = dict(label=label, before=status["generation"] if status else 0, input_at=time.monotonic(), seen=[])
        action()

    def follow(self, status):
        case = self.case
        now = time.monotonic()
        head = self.headline()
        word = head.split(" · ")[0]
        if not case["seen"] or case["seen"][-1][1] != word:
            case["seen"].append((round(now - case["input_at"], 3), word, shown_seconds(head)))
        done = (
            status is not None
            and status["generation"] > case["before"]
            and status["state"] == "complete"
            and not status.get("pending_update")
            and not self.panel._pending
        )
        if done and "complete_at" not in case:
            case["complete_at"] = now
        if "complete_at" not in case or now - case["complete_at"] < 0.3:
            assert now - case["input_at"] < 60, case
            return False
        measured = case["complete_at"] - case["input_at"]
        shown = shown_seconds(head)
        tip = self.panel.info.toolTip()
        record = dict(
            label=case["label"],
            measured_seconds=round(measured, 3),
            shown_seconds=shown,
            render_seconds=status["elapsed_seconds"],
            seen=case["seen"][:10],
            tooltip_timing=[
                line for line in tip.splitlines() if line.startswith(("Total", "Loading", "First image", "Render"))
            ],
        )
        self.report["updates"].append(record)
        assert head.startswith("Complete"), head
        # Shown total: from the action to the end of sampling. The probe sees
        # completion one poll later and the status refreshes every 0.1 s.
        assert shown is not None and measured - 0.3 <= shown <= measured + 0.05, record
        assert shown >= status["elapsed_seconds"] - 0.01, record
        assert f"Render {status['elapsed_seconds']:.2f} s" in tip, tip
        assert any(line.startswith("Loading ") for line in record["tooltip_timing"]), tip
        # The previous update's "Complete" does not linger once this one started.
        assert all(t >= measured - 0.15 for t, word, _ in case["seen"][1:] if word == "Complete"), case["seen"]
        time.sleep(0.25)
        QtWidgets.QApplication.processEvents()
        self.panel.poll()
        assert shown_seconds(self.headline()) == shown, (self.headline(), shown)
        return True

    def idle_lookups(self):
        self.lookups = 0
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            QtWidgets.QApplication.processEvents()
            time.sleep(0.005)
        return self.lookups

    def copy_diagnostics(self):
        status = self.status()
        before = (status["generation"], status["cooking_seconds"], status["extractions"])
        QtWidgets.QApplication.clipboard().setText("")
        self.panel.copy_diagnostics_action.trigger()
        text = QtWidgets.QApplication.clipboard().text()
        self.report["diagnostics"] = text
        for needle in (
            "Cycles for Houdini diagnostics",
            "Build: ",
            "Display: ",
            "Viewers: ",
            "IPR sessions: 1",
            "Renderer processes: ",
            "Registry: polling yes",
            "Updates (last ",
        ):
            assert needle in text, (needle, text)
        assert "material edit" not in text and "  render · complete · gen 1" in text, text
        assert re.search(r"pid \d+ · alive yes · age \d+:\d\d:\d\d", text), text
        assert self.panel.info.text().startswith("Diagnostics copied"), self.panel.info.text()
        QtWidgets.QApplication.processEvents()
        status = self.status()
        assert (status["generation"], status["cooking_seconds"], status["extractions"]) == before, (before, status)
        assert not status.get("pending_update")
        self.check("Copy diagnostics puts the report on the clipboard and confirms it; no cook, no new render")

    def rop_events(self):
        panel = self.panel
        self.rop.setName("renamed_render")
        assert panel.rop_path.currentData() == "/out/renamed_render", panel.rop_path.currentData()
        self.check("Renaming the ROP updates the viewer's ROP menu at once")
        self.rop.destroy()
        assert panel._rop is None
        QtWidgets.QApplication.processEvents()
        panel.poll()
        assert not panel.info.text().startswith("Render error"), panel.info.text()
        assert not panel.render_button.isEnabled()
        self.check("Deleting the ROP clears the viewer's ROP at once; no ObjectWasDeleted reaches the status")
        replacement = menu_actions.create_rop()
        panel.set_rop(replacement)
        assert panel._rop is not None
        hou.hipFile.clear(suppress_save_prompt=True)
        QtWidgets.QApplication.processEvents()
        assert panel._rop is None
        panel.poll()
        assert not panel.info.text().startswith("Render error"), panel.info.text()
        self.check("Clearing the scene clears the viewer's ROP; polling continues without errors")

    def tick(self):
        if self.finished or self.busy:
            return
        self.busy = True
        try:
            assert time.monotonic() - self.started < 400, self.phase
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                self.phase = "run"
                self.begin()
                return
            status = self.status()
            if status is not None:
                assert status["state"] != "failed", status
            if self.phase == "run" and self.follow(status):
                self.index += 1
                self.save()
                if self.index < len(self.steps):
                    self.begin()
                    return
                self.check("Status time counts from Render and from each edit and matches the measured wait")
                self.check("A finished update keeps its total; the pure render time stays in the tooltip")
                loading = [u for u in self.report["updates"] if any(word == "Loading" for _, word, _ in u["seen"])]
                self.report["updates_showing_loading"] = len(loading)
                assert loading, self.report["updates"]
                self.check("While nothing samples yet the status says Loading")
                lookups = self.idle_lookups()
                self.report["idle_node_lookups_per_2s"] = lookups
                assert lookups <= 6, lookups
                self.check("An idle open viewer verifies its ROP at most about once per second")
                self.copy_diagnostics()
                self.rop_events()
                self.finish()
        except BaseException:
            self.report["error"] = traceback.format_exc()
            self.finish(failed=True)
        finally:
            self.busy = False

    def finish(self, failed=False):
        self.finished = True
        self.report["result"] = "failed" if failed else "passed"
        self.save()
        try:
            hou.ui.removeEventLoopCallback(self.tick)
            if self.panel is not None:
                self.panel.dispose()
            registry.close_all()
        except Exception:
            pass
        hou.ui.postEventCallback(lambda: hou.exit(1 if failed else 0, suppress_save_prompt=True))


smoke = Smoke()
