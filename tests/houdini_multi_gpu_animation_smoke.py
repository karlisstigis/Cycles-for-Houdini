# SPDX-License-Identifier: Apache-2.0
"""Five heavy animated SOP frames, changing image displacement, both checked GPUs."""

import os
from pathlib import Path
import runpy
import hou
from cycles_houdini import device_selection, native_scene

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyAnimation = runpy.run_path(
    str(root / "tests/houdini_heavy_animation_benchmark.py"), run_name="multi_gpu_animation_fixture"
)["HeavyAnimation"]


class MultiGpuAnimation(HeavyAnimation):
    def configure_scene(self):
        super().configure_scene()
        native_scene.configuration(self.rop)
        self.rop.setParms(dict(device_mode="gpu", compute_backend="OPTIX"))
        for parm in device_selection.gpu_parameters(self.rop):
            self.rop.parm(parm.name()).set(1)
        self.selected = native_scene.configuration(self.rop)[0]
        assert len(self.selected) == 2
        # HeavyResponse applies its explicit selection after this hook. Keep
        # the two-device selection through that initialization step as well.
        self.device = self.selected
        self.report["selected_device_ids"] = self.selected

    def finish_frame(self, entry):
        assert tuple(entry.session.status()["render_device_ids"]) == self.selected, (
            entry.session.status()["render_device_ids"],
            self.selected,
        )
        super().finish_frame(entry)


workflow = MultiGpuAnimation()
