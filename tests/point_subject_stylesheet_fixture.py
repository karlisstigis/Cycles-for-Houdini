# SPDX-License-Identifier: Apache-2.0
"""Artist-authored point styles on particles and complete polygon/spline curves."""

import json
from pathlib import Path
import hou
import numpy as np
from geometry_key_binding_fixture import Fixture as KeyFixture, CURVE_CODE, COLORS, TINT


def sheet(power=None, *, material=None, target=None, surface=None, displacement=None):
    fields = {}
    if power is not None:
        fields["materialParameters"] = {"power": power}
    if material is not None:
        fields["material"] = {"name": material}
    if surface is not None:
        fields["surfaceParameters"] = {"power": surface}
    if displacement is not None:
        fields["displaceParameters"] = {"power": displacement}
    return json.dumps({"styles": [{"target": {"type": "self"} if target is None else target, "overrides": fields}]})


POINT_CURVE_CODE = CURVE_CODE.replace(
    "g.addAttrib(hou.attribType.Point,'width',.32)",
    "g.addAttrib(hou.attribType.Point,'width',.32)\ngain=g.addAttrib(hou.attribType.Point,'gain',0.)",
).replace(
    "if v.get('point_style'):p.setAttribValue(ps,v['point_style'])",
    """p.setAttribValue(gain,float(v.get('gains',[2.,3.])[i]))
        if v.get('point_style'):
            style=at(v['point_style'][i],k)
            if v.get('shutter_style_fault') and abs(now)>1e-7 and i==0 and k==0:style=v['bad_shutter_style']
            p.setAttribValue(ps,style)""",
)

PARTICLE_CODE = """import hou,json
n=hou.pwd();g=n.geometry();v=json.loads(n.evalParm('payload'))
pa=g.addAttrib(hou.attribType.Prim,'shop_materialpath','')
ps=g.addAttrib(hou.attribType.Prim,'material_stylesheet','')
po=g.addAttrib(hou.attribType.Point,'material_override','')
pt=g.addAttrib(hou.attribType.Point,'material_stylesheet','')
gain=g.addAttrib(hou.attribType.Point,'gain',0.)
for i,c in enumerate(g.prims()):
 c.setAttribValue(pa,v['material']);c.setAttribValue(ps,v['primitive_styles'][i])
for i,p in enumerate(g.points()):
 p.setAttribValue(po,repr({'tint':(.1,.2,.3),'power':2}))
 p.setAttribValue(pt,v['point_styles'][i]);p.setAttribValue(gain,float(i+1))
"""


class Fixture(KeyFixture):
    def __init__(self, device="CPU", *, heavy=False):
        super().__init__(device, heavy=heavy)
        self.dest = self.dest.parent / "point-subject-stylesheets"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.curves.parm("python").set(POINT_CURVE_CODE)
        self.particles.parm("python").set(PARTICLE_CODE)
        old = self.assigned
        self.assigned = self.obj.createNode("python", "unused_mesh_point_styles")
        self.assigned.setInput(0, old)
        self.assigned.addSpareParmTuple(hou.StringParmTemplate("point_style", "Unused mesh point style", 1))
        self.assigned.parm("python").set(
            "n=hou.pwd();g=n.geometry();a=g.addAttrib(hou.attribType.Point,'material_stylesheet','')\nfor p in g.points():p.setAttribValue(a,n.evalParm('point_style'))"
        )
        if self.obj.parm("shop_materialstylesheet") is None:
            self.obj.addSpareParmTuple(hou.StringParmTemplate("shop_materialstylesheet", "Stylesheet", 1))
        self.style = self.obj.parm("shop_materialstylesheet")
        self.imported = self.dest / "point-subject-import.json"
        self.missing_path = self.materials[0].parent().path() + "/point_subject_repaired"
        self.reset()

    def configure(self, **values):
        self.config.update(values)
        # A Python literal owns the JSON bytes; ordinary string expansion can
        # otherwise consume an escaping level inside nested CVEX source text.
        self.curves.parm("payload").setExpression(repr(json.dumps(self.config)), hou.exprLanguage.Python)

    def configure_particles(self, **values):
        self.particle_config.update(values)
        self.particles.parm("payload").setExpression(repr(json.dumps(self.particle_config)), hou.exprLanguage.Python)

    def reset(self):
        hou.setFrame(1)
        self.route(False)
        self.rop.parm("allowmotionblur").set(0)
        self.style.set("")
        self.assigned.parm("point_style").set("")
        self.particle_second.parm("particlegroup").set("2")
        self.configure(
            primitive=[m.path() for m in self.materials[:2]],
            point_paths=[m.path() for m in self.materials[2:4]],
            point_overrides=["{'power':2}"] * 2,
            vertex_paths=["", ""],
            vertex_overrides=["", ""],
            point_style=["", ""],
            vertex_style="",
            primitive_styles=["", ""],
            gains=[2.0, 3.0],
            spline=False,
            shared=False,
            animate=False,
            shutter_fault=False,
            shutter_style_fault=False,
        )
        self.particle_config = dict(
            material=self.materials[0].path(), primitive_styles=["", ""], point_styles=["", "", ""]
        )
        self.configure_particles()
        self.expected_curves = np.asarray(COLORS[2:4]) * 2
        self.expected_particles = np.asarray([TINT] * 3) * 2
        self.codes = set()

    def local_points(self, power=5):
        self.reset()
        self.configure(point_style=[sheet(power)] * 2)
        self.configure_particles(point_styles=[sheet(power), sheet(power + 1), sheet(power + 2)])
        self.expected_curves = np.asarray(COLORS[2:4]) * power
        self.expected_particles = np.asarray(TINT) * np.asarray([power, power + 1, power + 2])[:, None]

    def parent_priority(self):
        self.local_points(5)
        self.configure(primitive_styles=[sheet(3)] * 2)
        self.configure_particles(primitive_styles=[sheet(3)] * 2)
        self.expected_curves = np.asarray(COLORS[2:4]) * 3
        self.expected_particles = np.asarray([TINT] * 3) * 3

    def root_priority(self):
        self.parent_priority()
        self.configure(point_style=[sheet(1)] * 2)
        self.configure_particles(point_styles=[sheet(1)] * 3)
        self.style.set(sheet(1, target={"type": "self"}))
        self.expected_curves = np.asarray(COLORS[2:4])
        self.expected_particles = np.asarray([TINT] * 3)

    def root_points(self):
        self.local_points(5)
        self.style.set(sheet(4, target={"type": "point"}))
        self.expected_curves = np.asarray(COLORS[2:4]) * 4
        self.expected_particles = np.asarray([TINT] * 3) * 4

    def root_subtarget(self):
        self.parent_priority()
        self.style.set(sheet(7, target={"type": "primitive", "subTarget": {"type": "point"}}))
        self.expected_curves = np.asarray(COLORS[2:4]) * 7
        self.expected_particles = np.asarray([TINT] * 3) * 7

    def varying(self, broken=True):
        self.local_points(5)
        if broken:
            self.configure(point_style=[[sheet(5), sheet(1), sheet(5), sheet(5)], sheet(5)])
            self.expected_curves[0] = np.asarray(COLORS[2]) * 2
            self.codes = {"material.unsupported_binding"}

    def masked_variation(self):
        self.varying()
        self.configure(primitive_styles=[sheet(3)] * 2)
        self.expected_curves = np.asarray(COLORS[2:4]) * 3
        self.codes = set()

    def shader_categories(self):
        self.reset()
        value = sheet(2, surface=3, displacement=9)
        self.configure(point_style=[value] * 2)
        self.configure_particles(point_styles=[value] * 3)
        self.expected_curves = np.asarray(COLORS[2:4]) * 3
        self.expected_particles = np.asarray([TINT] * 3) * 3

    def switched_material(self):
        self.reset()
        self.configure(point_style=[sheet(material=m.path()) for m in self.materials[4:6]])
        self.configure_particles(point_styles=[sheet(material=self.materials[4].path())] * 3)
        self.expected_curves = np.asarray(COLORS[4:6])
        self.expected_particles = np.asarray([COLORS[4]] * 3)

    def missing(self):
        self.local_points()
        self.configure(point_style=[sheet(material=self.missing_path), sheet(5)])
        self.expected_curves[0] = (1, 0, 1)
        self.codes = {"material.missing"}

    def cvex(self, broken=False):
        self.reset()
        program = {
            "script": {
                "code": "cvex key(float gain=0;export float result=0){result=2*gain;}",
                "bindings": {"gain": "absent" if broken else "gain"},
                "return": "result",
            }
        }
        value = sheet(program)
        self.configure(point_style=[value] * 2)
        self.configure_particles(point_styles=[value] * 3)
        if broken:
            self.codes = {"material.stylesheet_ignored"}
        else:
            self.expected_curves = np.asarray(COLORS[2:4]) * np.asarray([4, 6])[:, None]
            self.expected_particles = np.asarray(TINT) * np.asarray([2, 4, 6])[:, None]

    def timed(self, frame=1):
        self.reset()
        self.obj.parm("ty").set(0)
        if self.obj.parm("style_clock") is None:
            self.obj.addSpareParmTuple(hou.FloatParmTemplate("style_clock", "Style clock", 1))
        self.obj.parm("style_clock").setExpression("$F", hou.exprLanguage.Hscript)
        value = sheet(
            {"script": {"code": 'cvex clocked(export float result=0){result=chf("style_clock");}', "return": "result"}}
        )
        self.configure(point_style=[value] * 2)
        self.configure_particles(point_styles=[value] * 3)
        hou.setFrame(frame)
        self.expected_curves = np.asarray(COLORS[2:4]) * frame
        self.expected_particles = np.asarray([TINT] * 3) * frame

    def imported_points(self, power=4, broken=False):
        self.reset()
        self.imported.write_text("{bad" if broken else sheet(power, target={"type": "point"}), encoding="utf-8")
        self.style.set(json.dumps({"importFiles": [self.imported.as_posix()]}))
        if broken:
            self.codes = {"material.stylesheet_ignored"}
        else:
            self.expected_curves = np.asarray(COLORS[2:4]) * power
            self.expected_particles = np.asarray([TINT] * 3) * power

    def mesh_ignored(self, broken=True):
        self.local_points()
        self.assigned.parm("point_style").set("{bad" if broken else "")

    def shared_context(self, conflict=False):
        self.local_points()
        self.configure(shared=True, point_paths=["", ""], primitive_styles=[sheet(3), sheet(4)])
        self.expected_curves = np.asarray(COLORS[:2]) * np.asarray([3, 4])[:, None]
        self.particle_second.parm("particlegroup").set("1 2" if conflict else "2")
        self.configure_particles(primitive_styles=[sheet(3), sheet(4)])
        self.expected_particles = np.asarray(TINT) * np.asarray([3, 3, 4])[:, None]
        if conflict:
            self.codes = {"material.conflicting_particle_binding"}

    def spline_points(self):
        self.local_points()
        self.configure(spline=True)

    def shutter(self, broken=False):
        self.local_points()
        self.rop.setParms(dict(allowmotionblur=1, geometry_motion="deformation"))
        self.configure(animate=True, shutter_style_fault=broken, bad_shutter_style=sheet(1))
        if broken:
            self.codes = {"material.unsupported_binding"}

    def steps(self):
        return [
            ("Underlying native point override layers", self.reset),
            ("Local particle and uniform whole-curve point sheets", self.local_points),
            ("Native primitive parents retain stylesheet priority", self.parent_priority),
            ("Root self and equal-valued point intent retain native priority", self.root_priority),
            ("Direct typed-point root rules fill native child context", self.root_points),
            ("Native primitive to point subtargets remain active", self.root_subtarget),
            ("Varying curve point style retains uniform native shading", self.varying),
            ("Cached point-style warning survives camera navigation", lambda: self.camera.parm("tx").set(0.04)),
            ("Repair varying curve point style without restart", lambda: self.varying(False)),
            ("Higher-priority primitive style masks varying local styles", self.masked_variation),
            ("Point material surface and displace categories remain independent", self.shader_categories),
            ("Point stylesheet material switches retain exact identities", self.switched_material),
            ("Missing assigned point-style material remains visible", self.missing),
            ("Create missing point-style material during IPR", self.repair_material),
            ("Delete point-style material during IPR", self.delete_material),
            ("Recreate point-style material clears diagnostics", self.repair_material),
            ("Bound point CVEX uses actual source point fields", self.cvex),
            ("Bad point CVEX input retains native material and geometry", lambda: self.cvex(True)),
            ("Repair bound point CVEX without restarting", self.cvex),
            ("Time-aware point style at first frame", self.timed),
            ("Time-aware point style advances without geometry changes", lambda: self.timed(3)),
            ("Imported direct point rules", self.imported_points),
            ("Break imported point rules locally", lambda: self.imported_points(broken=True)),
            ("Repair imported point rules during IPR", lambda: self.imported_points(6)),
            ("Invalid mesh-only point sheet is unused and quiet", self.mesh_ignored),
            ("Remove unused broken mesh point sheet", lambda: self.mesh_ignored(False)),
            ("Shared curve source point keeps distinct native parents", self.shared_context),
            ("Overlapping particle point diagnoses only conflicting owners", lambda: self.shared_context(True)),
            ("Repair particle owner membership", self.shared_context),
            ("Native spline keys retain uniform point styles", self.spline_points),
            ("Point styles retain independent curve deformation", self.shutter),
            ("Off-frame varying point style recovers locally", lambda: self.shutter(True)),
            ("Repair off-frame point style during IPR", self.shutter),
            ("Restore static point styles", self.local_points),
            ("Final camera navigation retains accepted point styles", lambda: self.camera.parm("tx").set(0.08)),
            ("Final object navigation retains accepted point styles", lambda: self.obj.parm("tx").set(0.08)),
        ]
