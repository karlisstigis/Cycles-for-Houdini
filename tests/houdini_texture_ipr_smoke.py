# SPDX-License-Identifier: Apache-2.0
"""Actual native IPR file edits, missing-file recovery and shared panel ownership."""

from support.paths import artifacts_root

from cycles_houdini.device_selection import selected_devices as rop_devices
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import time
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
from cycles_houdini import registry
from ipr_update_checks import recovered_update


class TextureLifecycle:
    def __init__(self):
        root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
        hou.hipFile.load(str(artifacts_root() / "stage3/textures/native-textures.hiplc"), suppress_save_prompt=True)
        self.rop = hou.node("/out/native_texture_cycles")
        self.texture = hou.node("/mat/native_texture_surface/file_image")
        set_rop_devices(self.rop, os.environ["CYCLES_HOUDINI_GUI_DEVICE"])
        self.rop.parm("samples").set(65536)
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent / "interactive.exr"
        self.write(self.path, (0.2, 0.4, 0.8))
        self.texture.parm("filename").set(str(self.path))
        self.key = f"rop:{self.rop.sessionId()}"
        self.report = dict(device=rop_devices(self.rop, hou.time())[0], host_pid=os.getpid(), events=[])
        self.started = time.monotonic()
        self.last_tick = 0
        self.stage = 0
        self.viewer = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.IPRViewer)
        self.viewer.setRopNode(self.rop)
        self.viewer.setPreview(True)
        self.viewer.setAutoUpdate(True)
        self.viewer.setDelay(0.02)
        self._tick = self.tick
        hou.ui.addEventLoopCallback(self._tick)
        self.viewer.startRender()
        self.save()

    def write(self, path, color):
        output = OIIO.ImageOutput.create(str(path))
        assert output.open(str(path), OIIO.ImageSpec(8, 8, 3, OIIO.FLOAT)), output.geterror()
        assert output.write_image(np.broadcast_to(np.asarray(color, dtype=np.float32), (8, 8, 3)).copy()), (
            output.geterror()
        )
        assert output.close(), output.geterror()

    def save(self):
        Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def record(self, name, entry):
        self.report["events"].append(
            dict(
                name=name,
                seconds=time.monotonic() - self.started,
                status=entry.session.status(),
                soho=entry.last_soho.copy(),
            )
        )
        self.save()

    def advance(self, stage, entry):
        self.generation = entry.session.status()["generation"]
        self.stage = stage

    def image(self, entry, color, newer=True):
        if not entry or entry.error:
            return False
        status = entry.session.status()
        frame = entry.session.frame_info()
        if (
            (newer and status["generation"] <= self.generation)
            or not frame
            or frame["generation"] != status["generation"]
        ):
            return False
        try:
            pixel = self.viewer.pixel("C", 32, 32)[:3]
        except hou.OperationFailed:
            return False
        return all(abs(a - b) < 0.001 for a, b in zip(pixel, color))

    def tick(self):
        if time.monotonic() - self.last_tick < 0.1:
            return
        self.last_tick = time.monotonic()
        try:
            entry = registry.get(self.key)
            self.report["current"] = dict(
                stage=self.stage, error=entry.error if entry else "", status=entry.session.status() if entry else None
            )
            self.report["native_panes"] = [
                dict(name=p.name(), rop=p.ropNode().path() if p.ropNode() else None, active=p.isActive())
                for p in hou.ui.paneTabs()
                if p.type() == hou.paneTabType.IPRViewer
            ]
            self.save()
            if time.monotonic() - self.started > 90:
                raise TimeoutError("Native texture IPR stalled")
            if self.stage == 0:
                if not entry or not entry.helper_pid or not self.image(entry, (0.2, 0.4, 0.8), False):
                    return
                self.extractions = entry.session.status()["extractions"]
                self.record("Specified texture radiance in native IPR", entry)
                self.advance(1, entry)
                self.write(self.path, (0.8, 0.2, 0.1))
            elif self.stage == 1:
                if not self.image(entry, (0.8, 0.2, 0.1)):
                    return
                assert entry.session.status()["extractions"] == self.extractions
                self.record("A disk-only file edit refreshes native IPR automatically", entry)
                self.advance(2, entry)
                self.texture.parm("filename").set(str(self.path.parent / "missing.exr"))
            elif self.stage == 2:
                if not recovered_update(entry, self.generation, "Missing texture file"):
                    return
                self.record("Missing image renders its diagnostic fallback and reports its path", entry)
                self.stage = 3
                self.write(self.path.parent / "missing.exr", (0.1, 0.6, 0.2))
            elif self.stage == 3:
                if not self.image(entry, (0.1, 0.6, 0.2)):
                    return
                self.record("Creating the missing file recovers IPR without a parameter edit", entry)
                # Texture recovery should not repeatedly recapture geometry.
                self.report["recovery_extractions"] = entry.session.status()["extractions"] - self.extractions
                assert self.report["recovery_extractions"] <= 1
                self.extractions = entry.session.status()["extractions"]
                self.panel_tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel)
                self.panel_tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
                self.panel = self.panel_tab.activeInterfaceRootWidget()
                before = entry.session.status()
                self.panel.set_rop(self.rop)
                assert self.panel.entry is entry and len(registry.entries()) == 1
                after = entry.session.status()
                for key in ("generation", "geometry_resources", "material_resources", "owned_geometry_bytes"):
                    assert before[key] == after[key], key
                self.record("Native viewer and panel share the texture session", entry)
                self.advance(4, entry)
                self.texture.setName("renamed_file_image")
            elif self.stage == 4:
                if not self.image(entry, (0.1, 0.6, 0.2)):
                    return
                assert entry.session.status()["extractions"] == self.extractions
                self.record("Renaming a texture node preserves its rendering identity", entry)
                self.panel.poll()
                path = self.path.parent / "texture-panel.png"
                assert self.panel.grab().save(str(path))
                self.report["panel_preview"] = str(path)
                self.advance(5, entry)
                self.viewer.killRender()
                self.viewer.close()
            elif self.stage == 5:
                if entry.channel or entry.helper_pid:
                    return
                assert entry.views == 1 and registry.get(self.key) is entry
                self.record("The panel retains image ownership after native viewer closure", entry)
                self.stage = 6
                self.write(self.path.parent / "missing.exr", (0.7, 0.3, 0.5))
            elif self.stage == 6:
                frame = entry.session.frame()
                if not frame or frame["generation"] <= self.generation:
                    return
                from array import array

                values = array("f")
                values.frombytes(frame["planes"]["combined"]["pixels"])
                offset = (32 * 64 + 32) * 4
                if max(abs(a - b) for a, b in zip(values[offset : offset + 3], (0.7, 0.3, 0.5))) > 0.001:
                    return
                assert entry.session.status()["extractions"] == self.extractions
                self.record("A disk edit updates the dedicated panel automatically", entry)
                self.finish(0)
        except Exception:
            self.report["error"] = traceback.format_exc()
            self.finish(1)

    def finish(self, code):
        hou.ui.removeEventLoopCallback(self._tick)
        try:
            self.viewer.killRender()
            self.viewer.close()
        except (hou.ObjectWasDeleted, hou.OperationFailed):
            pass
        if hasattr(self, "panel"):
            self.panel.dispose()
            try:
                self.panel_tab.close()
            except (hou.ObjectWasDeleted, hou.OperationFailed):
                pass
        entry = registry.get(self.key)
        registry.close_all()
        if entry:
            self.report["closed"] = entry.session.status()
            assert (
                self.report["closed"]["owned_geometry_bytes"] == self.report["closed"]["device_allocation_bytes"] == 0
            )
        self.report["result"] = "passed" if code == 0 else "failed"
        self.save()
        self.code = code
        self.exit_at = time.monotonic() + 0.5
        self._shutdown = self.shutdown
        hou.ui.addEventLoopCallback(self._shutdown)

    def shutdown(self):
        if time.monotonic() >= self.exit_at:
            hou.ui.removeEventLoopCallback(self._shutdown)
            hou.ui.postEventCallback(lambda: self.exit(self.code))

    def exit(self, code):
        try:
            hou.exit(code, suppress_save_prompt=True)
        except SystemExit:
            pass


test = TextureLifecycle()
