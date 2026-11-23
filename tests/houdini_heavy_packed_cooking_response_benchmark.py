# SPDX-License-Identifier: Apache-2.0
"""Real packed shape/topology edits, followed by matching stopped-renderer controls.

HOM edits, actual Qt paint and event-loop gaps are scoped observations. They do
not certify physical input latency or production p95 budgets.
"""

import os
from pathlib import Path
import runpy
import threading
import time
import traceback
import hou
from cycles_houdini import _native, registry

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
PackedResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="packed_cooking_control_fixture"
)["PackedResponse"]


class PackedCookingControl(PackedResponse):
    def configure_scene(self):
        super().configure_scene()
        # A real nonlinear deformation of the retained million-triangle source;
        # the source is cooked by native SOP/VEX, never a Python mesh generator.
        self.wave = self.packed_obj.createNode("attribwrangle", "WAVE_PROTOTYPE")
        self.wave.setInput(0, self.prototype)
        group = self.wave.parmTemplateGroup()
        group.append(hou.FloatParmTemplate("amplitude", "Amplitude", 1, default_value=(0.06,)))
        self.wave.setParmTemplateGroup(group)
        self.wave.parm("snippet").set('@P.y += chf("amplitude")*sin(4*@P.x)*cos(6*@P.z);')
        self.deform.setInput(0, self.wave)
        self.host_calls = []
        self.original_native = {}
        for name in ("acquire_apply", "expand_apply", "prepare_apply", "finish_apply", "native_stats"):
            original = getattr(_native, name)
            self.original_native[name] = original

            def measured(*args, _name=name, _original=original, **kwargs):
                started = time.monotonic()
                try:
                    return _original(*args, **kwargs)
                finally:
                    if threading.current_thread() is threading.main_thread():
                        self.host_calls.append(
                            dict(name=_name, phase=self.report.get("phase"), seconds=time.monotonic() - started)
                        )

            setattr(_native, name, measured)

    def edit(self, name, repetition):
        if name == "mesh-deform":
            self.wave.parm("amplitude").set(0.085 + 0.025 * repetition)
        else:
            super().edit(name, repetition)
        if name in ("mesh-deform", "mesh-topology"):
            self.scene.setPwd(self.packed_obj)
            self.packed_obj.renderNode().setCurrent(True, clear_all_selected=True)

    def finish(self):
        if not hasattr(self, "host_control_index"):
            self.host_control_index = 0
            self.host_control_apply_count = len(self.report["applies"])
            self.report["host_only_control"] = []
            self.panel.auto.setChecked(False)
            assert registry.get(self.key).session.status()["state"] == "cancelled"
            self.stage = "host-control-start"
            return
        self.report["host_call_observations"] = self.host_calls
        self.report["shape_edit"] = dict(
            source=self.wave.path(),
            parameter="amplitude",
            expression=self.wave.evalParm("snippet"),
            kind="nonlinear native VEX displacement of SOP points",
        )
        for name, original in self.original_native.items():
            setattr(_native, name, original)
        return super().finish()

    def tick(self):
        if not self.stage.startswith("host-control"):
            return super().tick()
        try:
            assert time.monotonic() - self.began < 200
            entry = registry.get(self.key)
            assert entry.session.status()["state"] == "cancelled"
            assert len(self.report["applies"]) == self.host_control_apply_count
            if self.stage == "host-control-start":
                if self.host_control_index == 6:
                    self.report["host_only_scope"] = (
                        "Same visible packed SOP viewport and shape/topology parameter values, renderer stopped and Auto disabled. Three edits each; no scene submissions. Controls follow the IPR run with warm caches. Event-loop gaps include cooking and viewport redraw."
                    )
                    return self.finish()
                name = "mesh-deform" if self.host_control_index < 3 else "mesh-topology"
                repetition = self.host_control_index % 3
                self.report["phase"] = "host-only/" + name + "-" + str(repetition)
                self.host_control_beat = len(self.report["heartbeat_gaps"])
                started = time.monotonic()
                self.edit(name, repetition)
                self.host_control_case = dict(
                    name=name, repetition=repetition, parameter_action_seconds=time.monotonic() - started
                )
                self.next_at = time.monotonic() + 0.8
                self.stage = "host-control-wait"
            elif time.monotonic() >= self.next_at:
                gaps = self.report["heartbeat_gaps"][self.host_control_beat :]
                self.host_control_case.update(
                    maximum_timer_gap_seconds=max((g["seconds"] for g in gaps), default=0), timer_samples=len(gaps)
                )
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


if __name__ == "__main__":
    workflow = PackedCookingControl()
