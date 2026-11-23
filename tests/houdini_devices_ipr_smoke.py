# SPDX-License-Identifier: Apache-2.0
"""Heavy SOP artist workflow: native device checkboxes/backend, one render view."""

import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from PySide6 import QtWidgets
import cycles_houdini as ch
from cycles_houdini import device_selection, native_scene, registry

os.environ["CYCLES_HOUDINI_BENCH_SHARED_VIEWS"] = "0"
os.environ["CYCLES_HOUDINI_BENCH_SAMPLES"] = "16"
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="devices_fixture")[
    "HeavyResponse"
]


class DevicesWorkflow(HeavyResponse):
    def configure_scene(self):
        self.ground.parm("resolution").set(560)
        native_scene.configuration(self.rop)  # Verify the fixture's explicit physical-device selection.
        self.parameters = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.Parm)
        self.parameters.setCurrentNode(self.rop)
        self.parameters.setPin(True)
        parm = self.rop.parm("compute_backend")
        parm.containingFolderSetParmTuples()[0][0].set(parm.containingFolderIndices()[0])
        self.device_cases = []
        self.device_case = 0
        self.old_session = None

    def tick(self):
        if self.tick_busy:
            return
        self.tick_busy = True
        try:
            if time.monotonic() - self.began > 180:
                raise TimeoutError("Native device workflow stalled")
            entry = registry.get(self.key)
            if entry is None:
                return
            if entry.error or entry.session.status()["state"] == "failed":
                raise RuntimeError(entry.error or entry.session.status()["error"])
            frame = entry.session.frame_info()
            if not frame or not frame["complete"] or self.panel._session is not entry.session:
                return
            self.panel.poll()
            if self.panel.canvas.generation != frame["generation"] or self.panel._sequence < frame["sequence"]:
                return
            if self.old_session is not None and entry.session is self.old_session:
                return
            if self.old_session is not None:
                assert self.old_session.status()["state"] == "closed"
                assert self.old_session.status()["device_allocation_bytes"] == 0
            expected = native_scene.configuration(self.rop)[0]
            status = entry.session.status()
            assert tuple(status["render_device_ids"]) == expected, (status, expected)
            assert len(registry.entries()) == 1 and entry.views == 1 and self.viewer is None
            assert not hasattr(self.panel, "device") and not hasattr(self.panel, "settings_button")
            assert self.panel.camera.y() > self.panel.rop_path.y()
            assert self.panel.camera.x() == self.panel.rop_path.x()
            assert self.panel.checker.text() == "" and not self.panel.checker.icon().isNull()
            assert not any(
                button.text().lower() == "full frame" for button in self.panel.findChildren(QtWidgets.QPushButton)
            )
            self.device_cases.append(dict(name=self.report["phase"], status=status, expected=expected))
            self.report["device_cases"] = self.device_cases
            if self.device_case == 4:
                self.panel.grab().save(str(self.path.with_suffix(".viewer.png")))
                hou.qt.floatingPanelWindow(self.parameters.floatingPanel()).grab().save(
                    str(self.path.with_suffix(".devices.png"))
                )
                self.parameters.close()
                self.report["last_status"] = status
                self.finish()
                return
            self.old_session = entry.session
            self.device_case += 1
            if self.device_case == 1:
                self.report["phase"] = "check-both-gpus"
                self.rop.parm("device_mode").set("gpu")
                self.rop.parm("compute_backend").set("OPTIX")
                for parm in device_selection.gpu_parameters(self.rop):
                    self.rop.parm(parm.name()).set(1)
                assert len(native_scene.configuration(self.rop)[0]) == 2
            elif self.device_case == 2:
                self.report["phase"] = "switch-backend-cuda"
                self.rop.parm("compute_backend").set("CUDA")
            elif self.device_case == 3:
                self.report["phase"] = "switch-device-cpu"
                self.rop.parm("device_mode").set("cpu")
            elif self.device_case == 4:
                self.report["phase"] = "restore-both-optix-gpus"
                self.rop.setParms(dict(device_mode="gpu", compute_backend="OPTIX"))
            self.save()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            if self.panel:
                self.panel.dispose()
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)
        finally:
            self.tick_busy = False


workflow = DevicesWorkflow()
