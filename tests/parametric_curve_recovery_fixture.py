# SPDX-License-Identifier: Apache-2.0
"""Native rational curves, an independent curve and mesh, and editable faults."""

from cycles_houdini.device_selection import set_devices as set_rop_devices
import hou
import cycles_houdini as ch
from parametric_curve_fixture import Fixture as BaseFixture


class Fixture(BaseFixture):
    def __init__(self, device, *, scale=1):
        super().__init__()
        self.reader_material = ch.create_material(name="parametric_reader", shader="emission")
        self.reader_material.node("surface_shader").parm("strength").set(1)
        self.reader = self.reader_material.createNode("cycles::attribute::1", "field")
        self.reader.parm("attribute").set("Cd")
        self.reader_material.node("surface_shader").setNamedInput("color", self.reader, "color")
        self.obj.parm("shop_materialpath").set(self.reader_material.path())
        self.control = self.obj.createNode("attribwrangle", "faults")
        self.control.setInput(0, self.attributes["rational_nurbs"])
        self.control.addSpareParmTuple(hou.IntParmTemplate("mode", "Fault test", 1, min=0, max=10))
        self.control.parm("snippet").set("""int mode=chi("mode");
if(mode==1 && @ptnum==1)@P.x=atof("nan");
if(mode==2 && @ptnum==1)@width=atof("nan");
if(mode==3 && @ptnum==1)@Cd.x=atof("nan");
if(mode==4 && @ptnum==1)f@signal=atof("nan");
if(mode==5 && @ptnum==0)@uv.x=atof("nan");
if(mode==6 && @ptnum==1)@v.x=atof("nan");
if((mode==7 || mode==9) && @ptnum==1)@Pw=atof("nan");
if(mode==8 && @ptnum==1)@Pw*=.15;""")
        sentinel = ch.create_material(name="parametric_sentinel", shader="emission")
        sentinel.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
        sentinel.node("surface_shader").parm("strength").set(1)
        secondary = self.obj.createNode("curve", "retained_curve", exact_type_name=True)
        secondary.setParms(dict(type="bezier", order=2, coords="-1.55,-.5,0 -1.55,.5,0"))
        retained = self.obj.createNode("attribwrangle", "retained_curve_attributes")
        retained.setInput(0, secondary)
        retained.parm("snippet").set('f@width=.1; if(chi("../faults/mode")==9)@P.x=atof("nan");')
        secondary_material = self.obj.createNode("material", "retained_curve_material")
        secondary_material.setInput(0, retained)
        secondary_material.parm("shop_materialpath1").set(sentinel.path())
        box = self.obj.createNode("box", "retained_mesh")
        box.parmTuple("size").set((0.3, 0.5, 0.2))
        box.parm("tx").set(1.55)
        box_material = self.obj.createNode("material", "retained_mesh_material")
        box_material.setInput(0, box)
        box_material.parm("shop_materialpath1").set(sentinel.path())
        primary = self.obj.createNode("switch", "primary_source")
        primary.setInput(0, self.control)
        primary.setInput(1, self.invalid_sources["bad_basis"])
        primary.parm("input").setExpression('ch("../faults/mode") == 10', hou.exprLanguage.Hscript)
        self.output = self.obj.createNode("merge", "OUT")
        for i, node in enumerate((primary, secondary_material, box_material)):
            self.output.setInput(i, node)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "recovery_camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "parametric_recovery")
        set_rop_devices(self.rop, device)
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=80 * scale,
                resy=48 * scale,
                samples=128,
                ipr_samples=128,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                geometry_motion="velocity",
                curve_width=0.08,
                curve_segments=16,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))


def pixels(frame):
    import numpy as np

    p = frame["planes"]["combined"]
    return (
        np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])[:, :, :3].copy()
    )
