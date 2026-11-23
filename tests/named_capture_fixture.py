# SPDX-License-Identifier: Apache-2.0
"""Named-field schemas and same-build forced-read control for native capture."""

import math
import hou
from mesh_field_fixture import Fixture as BaseFixture


class Fixture(BaseFixture):
    def __init__(self, device="CPU", heavy=False):
        super().__init__(device, heavy=heavy)
        # Host IDs identify cook output, not just equal values. Keep demanded
        # fields upstream of UV/Cd so those independent edits genuinely retain
        # their IDs. A downstream active author still gets a fresh ID when an
        # upstream field changes, and the capture must then read it again.
        previous = self.obj.node("prototype_fields")
        for name in ("mask", "paint", "Cd", "uv"):
            self.edits[name].setInput(0, previous)
            previous = self.edits[name]
        self.output = previous
        self.control = self.obj.createNode("python", "named_capture_control")
        self.control.setInput(0, self.output)
        self.control.addSpareParmTuple(hou.ToggleParmTemplate("force_ids", "Force Named Data IDs", default_value=False))
        self.control.addSpareParmTuple(hou.IntParmTemplate("field_mode", "Field Mode", 1, default_value=(0,)))
        # Copying the input keeps host ownership. This SOP changes only named
        # fields and explicitly bumps their IDs; topology and P stay untouched.
        # Forced reads are a measurement control in the same compiled runtime.
        self.control.parm("python").set("""import hou
node = hou.pwd()
node.setManagesAttribDataIds(True)
geo = node.geometry()
mode = node.evalParm('field_mode')
if mode:
    name = 'paint' if mode in (1, 7, 8) else 'mask'
    old = geo.findPointAttrib(name)
    if old is not None:
        old.destroy()
    owner, value = hou.attribType.Point, None
    if mode == 1: value = (.1, .05)
    elif mode == 2: value = 1
    elif mode == 3: owner, value = hou.attribType.Vertex, .03
    elif mode == 4: owner, value = hou.attribType.Prim, .03
    elif mode == 5: owner, value = hou.attribType.Global, .03
    elif mode == 6: owner, value = hou.attribType.Vertex, .03
    elif mode == 8: value = 'not_numeric'
    if value is not None:
        geo.addAttrib(owner, name, value).incrementDataId()
if node.evalParm('force_ids'):
    for name in ('paint', 'mask'):
        for finder in (geo.findVertexAttrib, geo.findPointAttrib, geo.findPrimAttrib, geo.findGlobalAttrib):
            attrib = finder(name)
            if attrib is not None:
                attrib.incrementDataId()
""")
        # HOM and point-targeted VEX setters promote same-name numeric fields.
        # A detail binding writer retains the existing vertex owner; this exact
        # case is measured in artifacts/named-owner-probe-20261020T194051484.
        # Bypass it elsewhere so it cannot reauthor unrelated named IDs.
        self.shadow = self.obj.createNode("attribwrangle", "shadowed_detail_field")
        self.shadow.setInput(0, self.control)
        self.shadow.parm("class").set(0)
        self.shadow.parm("snippet").set('f@mask=atof("nan");')
        self.shadow.bypass(True)
        self.output = self.shadow
        self.copies.setInput(0, self.output)

    def set_mode(self, value):
        self.control.parm("field_mode").set(value)
        self.shadow.bypass(value != 6)

    def reset(self):
        for node in self.edits.values():
            node.parm("snippet").set("")
        self.set_mode(0)
        self.control.parm("force_ids").set(0)
        self.obj.parm("tx").set(0)
        self.camera.parm("tx").set(0)

    def ids(self):
        geo = self.output.geometry()
        assert not self.control.errors() and not self.shadow.errors(), (self.control.errors(), self.shadow.errors())
        if self.control.parm("field_mode").eval() == 6:
            detail, vertex = geo.findGlobalAttrib("mask"), geo.findVertexAttrib("mask")
            assert detail is not None and vertex is not None, "The fixture must retain both mask owners"
            assert math.isnan(geo.attribValue(detail))
            assert abs(geo.prims()[0].vertices()[0].attribValue(vertex) - 0.03) < 1e-8
        result = {}
        for owner, finder in (
            ("point", geo.findPointAttrib),
            ("vertex", geo.findVertexAttrib),
            ("primitive", geo.findPrimAttrib),
            ("detail", geo.findGlobalAttrib),
        ):
            for name in ("P", "uv", "Cd", "paint", "mask"):
                attrib = finder(name)
                if attrib is not None:
                    result[owner + ":" + name] = dict(
                        id=list(attrib.dataId().vexAttribDataId()), size=attrib.size(), type=str(attrib.dataType())
                    )
        return result
