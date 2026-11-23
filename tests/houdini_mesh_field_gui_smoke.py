# SPDX-License-Identifier: Apache-2.0
"""Live dense field edits and NaN repairs while camera/object updates are queued."""

from mesh_field_fixture import Fixture
from houdini_mesh_payload_gui_smoke import Workflow


class FieldWorkflow(Workflow):
    def __init__(self):
        super().__init__(fixture_type=Fixture)
        f = self.f
        self.steps = [
            ("Initial fields", lambda: None, False, False, False),
            ("UV edit", lambda: f.edit("uv", "v@uv.x+=.1;", 0.1), False, False, False),
            ("Color edit", lambda: f.edit("Cd", "v@Cd.y+=.1;", 0.1), False, False, False),
            ("Named vector edit", lambda: f.edit("paint", "v@paint.z+=.1;", 0.1), False, False, False),
            ("Named scalar edit", lambda: f.edit("mask", "f@mask+=.1;", 0.1), False, False, False),
            ("Broken UV", lambda: f.edit("uv", 'v@uv.x=atof("nan");', broken=True), True, False, False),
            ("Repair UV", lambda: f.edit("uv", ""), False, False, False),
            ("Broken color", lambda: f.edit("Cd", 'v@Cd.y=atof("nan");', broken=True), True, False, False),
            ("Repair color", lambda: f.edit("Cd", ""), False, False, False),
            ("Broken named vector", lambda: f.edit("paint", 'v@paint.z=atof("nan");', broken=True), True, False, False),
            ("Repair named vector", lambda: f.edit("paint", ""), False, False, False),
            ("Broken named scalar", lambda: f.edit("mask", 'f@mask=atof("nan");', broken=True), True, False, False),
            ("Repair named scalar", lambda: f.edit("mask", ""), False, False, False),
            ("UV edit during navigation", self.surface_navigation, False, False, False),
            ("Color edit during navigation", self.geometry_navigation, False, False, False),
            ("Camera only", lambda: f.camera.parm("tx").set(-0.05), False, True, False),
            ("Object only", lambda: f.obj.parm("tx").set(-0.05), False, True, False),
        ]
        self.report["scope"] = __doc__

    def surface_navigation(self):
        self.f.edit("uv", "v@uv.x+=.1;", 0.1)
        self.navigate()

    def geometry_navigation(self):
        self.f.edit("Cd", "v@Cd.y+=.1;", 0.1)
        self.navigate()


if __name__ == "__main__":
    workflow = FieldWorkflow()
