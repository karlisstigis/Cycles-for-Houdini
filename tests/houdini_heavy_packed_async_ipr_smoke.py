# SPDX-License-Identifier: Apache-2.0
"""Hold acquired heavy packed prototypes while editing/deleting/restarting."""

import os
from pathlib import Path
import runpy
import hou
from cycles_houdini import _native

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
AsyncCaptureWorkflow = runpy.run_path(
    str(root / "tests/houdini_async_capture_ipr_smoke.py"), run_name="packed_lifecycle_fixture"
)["AsyncCaptureWorkflow"]
PackedResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="packed_scene_fixture"
)["PackedResponse"]


class PackedLifecycle(AsyncCaptureWorkflow):
    def configure_scene(self):
        PackedResponse.configure_scene(self)
        AsyncCaptureWorkflow.configure_scene(self)
        self.rop.parm("vobject").set("fx_courtyard heavy_packed async_transient")
        self.hero = self.deform
        self.hero.addSpareParmTuple(hou.FloatParmTemplate("twist", "Prototype deformation", 1))
        self.hero.parm("rx").setExpression('ch("twist")*10', hou.exprLanguage.Hscript)

    def hold(self, scan=False):
        super().hold(scan)
        if scan:
            held = _native.prewarm_apply
            counts = {}

            def packed_scan(job):
                count = counts[id(job)] = counts.get(id(job), 0) + 1
                # Roots and packed container have already been scanned; the
                # million-triangle leaf has been acquired on the host now.
                return held(job) if count == 3 else self.original_prewarm(job)

            _native.prewarm_apply = packed_scan


workflow = PackedLifecycle()
