# SPDX-License-Identifier: Apache-2.0
"""File/registered source break-repair in one real viewer with heavy instances."""

import json
import hou
from houdini_material_stylesheet_gui_smoke import Workflow as Base, sheet


class Workflow(Base):
    def __init__(self):
        super().__init__()
        f = self.f
        self.dest = f.dest.parent / "stylesheet-sources-live"
        self.dest.mkdir(exist_ok=True)
        self.file = self.dest / "light_surface.json"
        self.nested = self.dest / "nested.json"
        self.file.write_text(self.value(2))
        self.nested.write_text(self.value(3))
        self.source = f.obj.createNode("python", "import_attribute")
        self.source.setInput(0, f.sop)
        self.source.addSpareParmTuple(hou.StringParmTemplate("source_sheet", "Source Sheet", 1))
        self.source.parm("python").set(
            "g=hou.pwd().geometry();a=g.addAttrib(hou.attribType.Global,'material_stylesheet','');g.setGlobalAttribValue(a,hou.pwd().evalParm('source_sheet'))"
        )
        self.source.setDisplayFlag(True)
        self.source.setRenderFlag(True)
        self.selection = f.rop.parm("apply_stylesheets")
        self.control = f.obj.parm("shop_materialstylesheet")
        self.steps = [
            ("Underlying assignment", lambda: None, (0.2, 0.4, 0.6), False),
            ("Select scene stylesheet", lambda: self.scene(2), (0.4, 0.8, 1.2), False),
            ("Edit selected registered stylesheet", lambda: self.register(3), (0.6, 1.2, 1.8), False),
            (
                "Delete selected registered stylesheet",
                lambda: hou.styles.removeStyle("live_source"),
                (0.2, 0.4, 0.6),
                False,
            ),
            ("Recreate selected stylesheet", lambda: self.register(4), (0.8, 1.6, 2.4), False),
            ("Switch scene selection off", lambda: self.selection.set(""), (0.2, 0.4, 0.6), False),
            ("Scene bundle target", self.bundle_style, (1, 2, 3), False),
            ("Remove bundle membership", lambda: self.bundle.removeNode(f.obj), (0.2, 0.4, 0.6), False),
            ("Restore bundle membership", lambda: self.bundle.addNode(f.obj), (1, 2, 3), False),
            ("Remove bundle rule", lambda: self.selection.set(""), (0.2, 0.4, 0.6), False),
            ("Import object file", lambda: self.control.set(self.imported(self.file)), (0.4, 0.8, 1.2), False),
            ("Edit imported file", lambda: self.file.write_text(self.value(3)), (0.6, 1.2, 1.8), False),
            ("Break imported file", lambda: self.file.write_text("{broken"), (0.2, 0.4, 0.6), True),
            ("Repair imported file", lambda: self.file.write_text(self.value(4)), (0.8, 1.6, 2.4), False),
            ("Delete imported file", lambda: self.file.unlink(), (0.2, 0.4, 0.6), True),
            ("Recreate imported file", lambda: self.file.write_text(self.value(2)), (0.4, 0.8, 1.2), False),
            ("Nested import", lambda: self.file.write_text(self.imported(self.nested)), (0.6, 1.2, 1.8), False),
            ("Edit nested file", lambda: self.nested.write_text(self.value(5)), (1, 2, 3), False),
            ("Break nested file", lambda: self.nested.write_text("{broken"), (0.2, 0.4, 0.6), True),
            ("Repair nested file", lambda: self.nested.write_text(self.value(2)), (0.4, 0.8, 1.2), False),
            ("Registered import", self.registered_import, (0.6, 1.2, 1.8), False),
            ("Edit registered import", lambda: self.register(4), (0.8, 1.6, 2.4), False),
            ("Delete registered import", lambda: hou.styles.removeStyle("live_source"), (0.2, 0.4, 0.6), True),
            ("Recreate registered import", lambda: self.register(2), (0.4, 0.8, 1.2), False),
            ("Detail attribute import", self.detail_import, (0.6, 1.2, 1.8), False),
            ("Edit detail imported file", lambda: self.file.write_text(self.value(4)), (0.8, 1.6, 2.4), False),
            ("Break detail imported file", lambda: self.file.write_text("{broken"), (0.2, 0.4, 0.6), True),
            ("Repair detail imported file", lambda: self.file.write_text(self.value(2)), (0.4, 0.8, 1.2), False),
            ("Camera navigation", lambda: f.camera.parm("tx").set(0.3), (0.4, 0.8, 1.2), False),
            ("Packed object transform", lambda: f.heavy.parm("tx").set(0.5), (0.4, 0.8, 1.2), False),
            ("Remove all stylesheet sources", lambda: self.source.parm("source_sheet").set(""), (0.2, 0.4, 0.6), False),
        ]
        self.report["scope"] = (
            "Registered selection and object/detail nested imports update automatically; heavy shared prototype stays at one cook"
        )

    @staticmethod
    def value(power):
        return sheet({"materialParameters": {"power": power}})

    @staticmethod
    def imported(path):
        return json.dumps({"importFiles": [str(path).replace("\\", "/")]})

    def register(self, power):
        hou.styles.addStyle(
            "live_source",
            "Live renderer source test",
            sheet({"materialParameters": {"power": power}}, dict(objectName=self.f.obj.path())),
        )

    def scene(self, power):
        self.register(power)
        self.selection.set("live_*")

    def registered_import(self):
        self.register(3)
        self.file.write_text(self.imported("live_source"))

    def bundle_style(self):
        self.bundle = hou.addNodeBundle("live_stylesheet_bundle")
        self.bundle.addNode(self.f.obj)
        hou.styles.addStyle(
            "bundle_rule",
            "Live bundle targeting",
            sheet({"materialParameters": {"power": 5}}, dict(objectBundle=self.bundle.name())),
        )
        self.selection.set("bundle_rule")

    def detail_import(self):
        self.control.set("")
        self.file.write_text(self.value(3))
        self.source.parm("source_sheet").set(self.imported(self.file))


if __name__ == "__main__":
    workflow = Workflow()
