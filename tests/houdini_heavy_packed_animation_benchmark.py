# SPDX-License-Identifier: Apache-2.0
"""Five frames with a changing million-triangle packed prototype and displacement."""

import os
from pathlib import Path
import runpy
import hou

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
HeavyAnimation = runpy.run_path(
    str(root / "tests/houdini_heavy_animation_benchmark.py"), run_name="packed_animation_base"
)["HeavyAnimation"]
PackedResponse = runpy.run_path(
    str(root / "tests/houdini_heavy_packed_response_benchmark.py"), run_name="packed_animation_scene"
)["PackedResponse"]


class PackedAnimation(HeavyAnimation):
    expected_geometry_conversions = 2

    def configure_scene(self):
        HeavyAnimation.configure_scene(self)
        original_objects = self.rop.evalParm("vobject")
        PackedResponse.configure_scene(self)
        self.rop.parm("vobject").set(original_objects + " heavy_packed")
        self.deform.parm("rx").setExpression("5+7*($F-1)", hou.exprLanguage.Hscript)
        self.report["packed_fixture"] = dict(
            triangles_per_prototype=2 * (self.prototype_rows - 1) ** 2,
            instances=self.instance_count,
            nesting_levels=2 if self.instance_count == 16 else 1,
            animated_parameter=self.deform.path() + "/rx",
        )


workflow = PackedAnimation()
