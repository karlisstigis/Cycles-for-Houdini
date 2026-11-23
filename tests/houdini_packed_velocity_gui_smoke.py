# SPDX-License-Identifier: Apache-2.0
"""Native packed velocity edits while a heavy shared prototype stays retained."""

from houdini_packed_deformation_gui_smoke import Workflow as SharedWorkflow
from packed_velocity_fixture import Fixture


class Workflow(SharedWorkflow):
    fixture_type = Fixture
    camera_animation = "rint($FF)*.01+.12"
    changed_stages = (8,)
    retained_stages = (1, 2, 3, 4, 5, 6, 7, 9)
    scope = "One native viewer and Scene Viewer; 1,036,800-triangle prototype and 16 nested instances. Native keyboard velocity edits, invalid-data repair, duplicate-independent identities, concurrent geometry/navigation and five frame images. No shutter SOP cooks. Physical mouse/display and latency certification are separate."

    def motion_controls(self, f):
        return f.velocity.parm("test_velocity"), f.velocity.parm("test_invalid"), 2

    def render_source(self, f):
        return f.outer

    def animated_position(self):
        return 0

    def check_status(self, status):
        assert status["motion_cooks"] == 0, status
        assert len(status["source_diagnostics"]) == (1 if self.stage in (2, 3) else 0), status


workflow = Workflow()
