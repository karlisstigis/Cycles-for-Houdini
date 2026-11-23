# SPDX-License-Identifier: Apache-2.0
"""Heavy IPR edits followed by the same edits with renderer updates stopped."""

import os
from pathlib import Path
import runpy
import time
import traceback
import hou
from cycles_houdini import registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_response_benchmark.py"), run_name="host_control_fixture"
)["HeavyResponse"]


class HostCookingControl(HeavyResponse):
    def finish(self):
        if not hasattr(self, "host_control_index"):
            self.host_control_index = 0
            self.host_control_apply_count = len(self.report["applies"])
            self.report["host_only_control"] = []
            self.panel.auto.setChecked(False)
            assert registry.get(self.key).session.status()["state"] == "cancelled"
            self.stage = "host-control-start"
            return
        return super().finish()

    def tick(self):
        if not self.stage.startswith("host-control"):
            return super().tick()
        try:
            entry = registry.get(self.key)
            assert entry.session.status()["state"] == "cancelled"
            assert len(self.report["applies"]) == self.host_control_apply_count
            if self.stage == "host-control-start":
                if self.host_control_index == 6:
                    self.report["host_only_scope"] = (
                        "Same visible heavy SOP viewport, renderer stopped and Auto disabled. No scene submissions. Three alternating deformation edits and three topology edits after the IPR run; warm caches/order can differ. Timer gaps include host cooking, viewport redraw and panel polling."
                    )
                    return self.finish()
                name = "mesh-deform" if self.host_control_index < 3 else "mesh-topology"
                self.report["phase"] = "host-only/" + name + "-" + str(self.host_control_index % 3)
                self.host_control_beat = len(self.report["heartbeat_gaps"])
                started = time.perf_counter()
                self.edit(name, 1 + self.host_control_index % 3)
                self.host_control_case = dict(name=name, parameter_action_seconds=time.perf_counter() - started)
                self.next_at = time.monotonic() + 0.5
                self.stage = "host-control-wait"
            elif time.monotonic() >= self.next_at:
                gaps = self.report["heartbeat_gaps"][self.host_control_beat :]
                self.host_control_case["maximum_timer_gap_seconds"] = max((gap["seconds"] for gap in gaps), default=0.0)
                self.report["host_only_control"].append(self.host_control_case)
                self.host_control_index += 1
                self.stage = "host-control-start"
                self.save()
        except Exception:
            self.report.update(result="failed", error=traceback.format_exc())
            self.save()
            self.timer.stop()
            hou.ui.removeEventLoopCallback(self.callback)
            registry.close_all()
            hou.exit(1, suppress_save_prompt=True)


workflow = HostCookingControl()
