# SPDX-License-Identifier: Apache-2.0
"""Whole-curve key layers and exact particle-owner binding references."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import json
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import device_selection
from light_shader_fixture import Fixture as LightFixture


COLORS = ((0.8, 0.1, 0.05), (0.05, 0.7, 0.1), (0.1, 0.3, 0.5), (0.6, 0.2, 0.1), (0.15, 0.6, 0.3), (0.5, 0.2, 0.7))
CONTROL = (0.12, 0.24, 0.36)
TINT = (0.1, 0.2, 0.3)

CURVE_CODE = """import hou,json
n=hou.pwd();g=n.geometry();v=json.loads(n.evalParm('payload'));now=n.evalParm('clock')
pa=g.addAttrib(hou.attribType.Prim,'shop_materialpath','')
def active(value):return any(active(x) for x in value) if isinstance(value,list) else bool(value)
assert not(active(v['point_paths']) and active(v['vertex_paths']))
assert not(active(v['point_overrides']) and active(v['vertex_overrides']))
pp=g.addAttrib(hou.attribType.Point,'shop_materialpath','') if active(v['point_paths']) else None
vp=g.addAttrib(hou.attribType.Vertex,'shop_materialpath','') if active(v['vertex_paths']) else None
po=g.addAttrib(hou.attribType.Point,'material_override','') if active(v['point_overrides']) else None
vo=g.addAttrib(hou.attribType.Vertex,'material_override','') if active(v['vertex_overrides']) else None
g.addAttrib(hou.attribType.Point,'width',.32)
if v.get('point_style'): ps=g.addAttrib(hou.attribType.Point,'material_stylesheet','')
if v.get('vertex_style'): vs=g.addAttrib(hou.attribType.Vertex,'material_stylesheet','')
if v.get('primitive_styles'): pr=g.addAttrib(hou.attribType.Prim,'material_stylesheet','')
shared=None
def at(value,k):return value[k] if isinstance(value,list) else value
for i,x in enumerate((-1.,1.)):
    c=g.createNURBSCurve(4,False,4) if v.get('spline') else g.createPolygon(is_closed=False)
    c.setAttribValue(pa,v['primitive'][i])
    if v.get('primitive_styles'):c.setAttribValue(pr,v['primitive_styles'][i])
    for k,y in enumerate((-.7,-.2,.2,.7)):
        if v.get('spline'):vertex=c.vertices()[k];p=vertex.point()
        elif v.get('shared') and k==3 and shared is not None:p=shared;vertex=c.addVertex(p)
        else:p=g.createPoint();vertex=c.addVertex(p)
        px=0. if v.get('shared') and k==3 else x
        p.setPosition((px+(now*1.44 if v.get('animate') else 0.),y,0.))
        path=at(v['point_paths'][i],k)
        if v.get('shutter_fault') and abs(now)>1e-7 and i==0 and k==0:path=v['primitive'][0]
        if pp is not None:p.setAttribValue(pp,path)
        if po is not None:p.setAttribValue(po,at(v['point_overrides'][i],k))
        if vp is not None:vertex.setAttribValue(vp,at(v['vertex_paths'][i],k))
        if vo is not None:vertex.setAttribValue(vo,at(v['vertex_overrides'][i],k))
        if v.get('point_style'):p.setAttribValue(ps,v['point_style'])
        if v.get('vertex_style'):vertex.setAttribValue(vs,v['vertex_style'])
        if v.get('shared') and k==3:shared=p
"""

PARTICLE_CODE = """import hou,json
n=hou.pwd();g=n.geometry();v=json.loads(n.evalParm('payload'))
pa=g.addAttrib(hou.attribType.Prim,'shop_materialpath','')
ps=g.addAttrib(hou.attribType.Prim,'material_stylesheet','')
po=g.addAttrib(hou.attribType.Point,'material_override','')
for i,c in enumerate(g.prims()):
    c.setAttribValue(pa,v['material'])
    material=v.get('missing') if i==1 and v.get('missing') else None
    fields={'material':{'name':material}} if material else {'materialParameters':{'power':2+i}}
    c.setAttribValue(ps,json.dumps({'styles':[{'target':{'type':'self'},'overrides':fields}]}))
for p in g.points():p.setAttribValue(po,repr({'tint':(.1,.2,.3),**({'unknown_field':1} if v.get('bad_override') else {})}))
"""


class Fixture(LightFixture):
    def __init__(self, device="CPU", *, heavy=False):
        targets = json.loads(device) if isinstance(device, str) and device.lstrip().startswith("[") else [device]
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(v, str) or not v for v in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError("Expected a device ID or nonempty JSON list of unique device IDs")
        super().__init__(targets[0])
        device_selection.set_devices(self.rop, targets)
        self.device, self.target_ids = device, sorted(targets)
        self.dest = artifacts_root() / "stage3/materials/geometry-key-bindings"
        self.dest.mkdir(parents=True, exist_ok=True)
        hou.setFps(24)
        hou.setFrame(1)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                use_material_assignments=1,
                resx=96,
                resy=96,
                samples=16,
                ipr_samples=16,
                pass_position=1,
                motion_samples=3,
                point_radius=0.25,
            ),
        )
        pass_fixture.parm(self.light, "light_intensity").set(0)
        self.materials = [self.material("key_binding_" + str(i), color) for i, color in enumerate(COLORS)]
        self.mesh_material = self.material("key_binding_control", CONTROL)
        displacement = self.mesh_material.createNode("cycles::vector_displacement::1", "independent_height")
        # The default this test was written for (new nodes now take Blender's).
        displacement.setParms(dict(scale=1.0, space="object"))
        displacement.parmTuple("vector").set((0.0, 0.0, 0.15))
        self.mesh_material.node("surface_output").setNamedInput("displacement", displacement, "displacement")
        pass_fixture.parm(self.mesh_material, "cycles_displacement_method").set("true")
        self.sop.parmTuple("size").set((4.0, 0.6))
        self.sop.parmTuple("t").set((0.0, -1.5, 0.0))
        self.assigned = self.obj.createNode("material", "independent_mesh_control")
        self.assigned.setInput(0, self.sop)
        pass_fixture.parm(self.assigned, "shop_materialpath1").set(self.mesh_material.path())
        self.curves = self.obj.createNode("python", "authored_curve_keys")
        self.curves.addSpareParmTuple(hou.StringParmTemplate("payload", "Owned key assignments", 1))
        self.curves.addSpareParmTuple(hou.FloatParmTemplate("clock", "Clock", 1))
        pass_fixture.parm(self.curves, "clock").setExpression("$T", hou.exprLanguage.Hscript)
        pass_fixture.parm(self.curves, "python").set(CURVE_CODE)
        points = self.obj.createNode("add", "particle_points")
        pass_fixture.set_parameters(
            points,
            dict(points=3, usept0=1, pt0x=-1, pt0y=1.25, usept1=1, pt1x=0, pt1y=1.25, usept2=1, pt2x=1, pt2y=1.25),
        )
        self.particle_first = self.obj.createNode("add", "first_particle_owner")
        self.particle_first.setInput(0, points)
        pass_fixture.set_parameters(self.particle_first, dict(addparticlesystem=1, particlegroup="0 1"))
        self.particle_second = self.obj.createNode("add", "second_particle_owner")
        self.particle_second.setInput(0, self.particle_first)
        pass_fixture.set_parameters(self.particle_second, dict(addparticlesystem=1, particlegroup="2"))
        self.particles = self.obj.createNode("python", "particle_owner_styles")
        self.particles.setInput(0, self.particle_second)
        self.particles.addSpareParmTuple(hou.StringParmTemplate("payload", "Owned particle layers", 1))
        pass_fixture.parm(self.particles, "python").set(PARTICLE_CODE)
        self.output = self.obj.createNode("merge", "supported_mixed_components")
        for i, source in enumerate((self.assigned, self.curves, self.particles)):
            self.output.setInput(i, source)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.isolated = self.scene.createNode("geo", "isolated_vertex_override_curves", run_init_scripts=False)
        pass_fixture.parm(self.isolated, "tx").setExpression(
            'ch("' + self.obj.path() + '/tx")', hou.exprLanguage.Hscript
        )
        isolated_source = self.isolated.createNode("object_merge", "source_keys")
        pass_fixture.set_parameters(isolated_source, dict(objpath1=self.curves.path(), xformtype=0))
        isolated_source.setDisplayFlag(True)
        isolated_source.setRenderFlag(True)
        self.heavy = None
        self.isolated_mode = False
        self.expected_curves = np.asarray(COLORS[2:4])
        self.expected_particles = np.asarray((np.asarray(TINT) * 2, np.asarray(TINT) * 2, np.asarray(TINT) * 3))
        self.codes = set()
        self.config = dict(
            primitive=[m.path() for m in self.materials[:2]],
            point_paths=[m.path() for m in self.materials[2:4]],
            point_overrides=["", ""],
            vertex_paths=["", ""],
            vertex_overrides=["", ""],
        )
        self.configure()
        self.particle_config = dict(material=self.materials[0].path())
        self.configure_particles()
        self.missing_path = self.materials[0].parent().path() + "/key_binding_repaired"
        self.repaired = None
        if heavy:
            self.add_heavy_packed()
        self.route(False)

    @staticmethod
    def material(name, color):
        node = ch.create_material(name=name, shader="emission")
        shader = node.node("surface_shader")
        shader.parmTuple("color").set(tuple(color))
        pass_fixture.parm(shader, "strength").set(1.0)
        ch.promote_parameter(node, shader, "color", name="tint")
        ch.promote_parameter(node, shader, "strength", name="power")
        return node

    def configure(self, **values):
        self.config.update(values)
        pass_fixture.parm(self.curves, "payload").set(json.dumps(self.config))

    def configure_particles(self, **values):
        self.particle_config.update(values)
        pass_fixture.parm(self.particles, "payload").set(json.dumps(self.particle_config))

    def route(self, isolated):
        self.isolated_mode = isolated
        sources = [self.assigned, self.particles] if isolated else [self.assigned, self.curves, self.particles]
        for index, source in enumerate(sources):
            if self.output.input(index) != source:
                self.output.setInput(index, source)
        for index in range(len(self.output.inputs()) - 1, len(sources) - 1, -1):
            self.output.setInput(index, None)
        objects = [self.obj.path()] + ([self.isolated.path()] if isolated else [])
        if self.heavy is not None:
            objects.append(self.heavy.path())
        pattern = " ".join(objects)
        if pass_fixture.evaluate(self.rop, "vobject") != pattern:
            pass_fixture.parm(self.rop, "vobject").set(pattern)

    def uniform(self):
        self.route(False)
        pass_fixture.parm(self.rop, "allowmotionblur").set(0)
        self.configure(
            primitive=[m.path() for m in self.materials[:2]],
            point_paths=[m.path() for m in self.materials[2:4]],
            point_overrides=["", ""],
            vertex_paths=["", ""],
            vertex_overrides=["", ""],
            spline=False,
            shared=False,
            animate=False,
            shutter_fault=False,
            point_style="",
            vertex_style="",
            primitive_styles=[],
        )
        self.expected_curves = np.asarray(COLORS[2:4])
        self.codes = set()

    def point_power(self):
        self.uniform()
        self.configure(point_overrides=["{'power':2}"] * 2)
        self.expected_curves = np.asarray(COLORS[2:4]) * 2

    def vertex_power(self):
        self.uniform()
        self.route(True)
        self.configure(vertex_overrides=["{'power':3}"] * 2)
        self.expected_curves = np.asarray(COLORS[2:4]) * 3

    def varying_vertex(self, broken=True):
        self.point_power()
        self.configure(
            primitive=[m.path() for m in self.materials[2:4]],
            point_paths=["", ""],
            vertex_paths=[[self.materials[4].path(), "", "", ""], ""] if broken else ["", ""],
        )
        self.codes = {"material.unsupported_binding"} if broken else set()

    def varying_point(self, broken=True):
        self.uniform()
        self.configure(
            point_paths=[[self.materials[0].path()] + [self.materials[2].path()] * 3, self.materials[3].path()]
            if broken
            else [m.path() for m in self.materials[2:4]]
        )
        self.expected_curves[0] = COLORS[0] if broken else COLORS[2]
        self.codes = {"material.unsupported_binding"} if broken else set()

    def masked_lower(self):
        self.uniform()
        self.configure(
            point_paths=["", ""],
            point_overrides=[["{'power':1}", "{'power':2}", "{'power':3}", "{'power':4}"], "{'power':2}"],
            vertex_paths=[self.materials[4].path(), self.materials[5].path()],
        )
        self.expected_curves = np.asarray(COLORS[4:6])
        self.codes = set()

    def shared_keys(self):
        self.uniform()
        self.configure(shared=True, point_paths=["", ""], point_overrides=["{'power':2}"] * 2)
        self.expected_curves = np.asarray(COLORS[:2]) * 2

    def style_priority(self):
        self.point_power()
        sheet = json.dumps(
            {"styles": [{"target": {"type": "self"}, "overrides": {"materialParameters": {"power": 5}}}]}
        )
        self.configure(primitive_styles=[sheet, sheet])
        self.expected_curves = np.asarray(COLORS[2:4]) * 5

    def style_masks_lower(self):
        self.varying_point()
        sheets = [
            json.dumps({"styles": [{"target": {"type": "self"}, "overrides": {"material": {"name": m.path()}}}]})
            for m in self.materials[4:6]
        ]
        self.configure(primitive_styles=sheets)
        self.expected_curves = np.asarray(COLORS[4:6])
        self.codes = set()

    def spline(self):
        self.vertex_power()
        self.configure(spline=True)

    def missing(self):
        self.uniform()
        self.configure(point_paths=[self.missing_path, self.materials[3].path()])
        self.expected_curves[0] = (1, 0, 1)
        self.codes = {"material.missing"}

    def repair_material(self):
        assert hou.node(self.missing_path) is None
        self.repaired = self.material(self.missing_path.rsplit("/", 1)[1], (0.25, 0.55, 0.75))
        assert self.repaired.path() == self.missing_path
        self.expected_curves[0] = (0.25, 0.55, 0.75)
        self.codes = set()

    def delete_material(self):
        self.repaired.destroy()
        self.repaired = None
        self.expected_curves[0] = (1, 0, 1)
        self.codes = {"material.missing"}

    def curve_styles(self, enabled):
        self.uniform()
        self.configure(
            point_style=json.dumps(
                {"styles": [{"target": {"type": "self"}, "overrides": {"materialParameters": {"power": 5}}}]}
            )
            if enabled
            else ""
        )
        if enabled:
            self.expected_curves = np.asarray(COLORS[2:4]) * 5
        self.codes = set()

    def overlap(self, enabled):
        self.uniform()
        pass_fixture.parm(self.particle_second, "particlegroup").set("1 2" if enabled else "2")
        self.codes = {"material.conflicting_particle_binding"} if enabled else set()

    def particle_fault(self, enabled):
        self.uniform()
        self.configure_particles(bad_override=enabled)
        self.codes = {"material.override_ignored"} if enabled else set()

    def deformation(self, broken=False):
        self.uniform()
        pass_fixture.set_parameters(self.rop, dict(allowmotionblur=1, geometry_motion="deformation"))
        self.configure(animate=True, shutter_fault=broken)
        self.codes = {"material.unsupported_binding"} if broken else set()

    def authored(self):
        return {
            parm.path(): parm.rawValue()
            for node in (*self.materials, self.mesh_material)
            for child in (node, *node.allSubChildren())
            for parm in child.parms()
        }

    def sample(self, pixels, x, y):
        x += pass_fixture.parm(self.obj, "tx").eval() - pass_fixture.parm(self.camera, "tx").eval()
        col, row = int((x / 4 + 0.5) * 96), int((y / 4 + 0.5) * 96)
        return pixels[row - 1 : row + 2, col : col + 1, :3].mean((0, 1))

    def assert_pixels(self, frame):
        image = self.pixels(frame)
        assert image.shape == (96, 96, 4) and np.isfinite(image).all()
        curves = [self.sample(image, x, 0) for x in (-1, 1)]
        particles = [self.sample(image, x, 1.25) for x in (-1, 0, 1)]
        np.testing.assert_allclose(curves, self.expected_curves, atol=8e-5, rtol=0)
        np.testing.assert_allclose(particles, self.expected_particles, atol=8e-5, rtol=0)
        np.testing.assert_allclose(self.sample(image, 0, -1.5), CONTROL, atol=8e-5, rtol=0)
        positions = self.pixels(frame, "position")
        x = pass_fixture.parm(self.obj, "tx").eval() - pass_fixture.parm(self.camera, "tx").eval()
        col, row = int((x / 4 + 0.5) * 96), 12
        np.testing.assert_allclose(positions[row - 1 : row + 2, col : col + 1, 2], 0.15, atol=3e-5, rtol=0)
        return dict(curves=np.asarray(curves).tolist(), particles=np.asarray(particles).tolist(), independent_z=0.15)

    def steps(self):
        return [
            ("Uniform point material paths on complete curves", lambda: None),
            ("Uniform point overrides preserve distinct curve parents", self.point_power),
            ("Uniform vertex overrides take priority", self.vertex_power),
            ("Varying vertex paths retain uniform point shading", self.varying_vertex),
            (
                "Cached varying-key warning survives camera navigation",
                lambda: pass_fixture.parm(self.camera, "tx").set(0.04),
            ),
            ("Vertex-key repair clears warning", lambda: self.varying_vertex(False)),
            ("Varying point paths retain complete primitive shading", self.varying_point),
            ("Uniform higher vertex paths mask irrelevant lower variation", self.masked_lower),
            ("Primitive stylesheet keeps priority over native key overrides", self.style_priority),
            ("Whole-curve stylesheet masks varying lower material paths", self.style_masks_lower),
            ("Shared source point keeps each complete curve parent", self.shared_keys),
            ("Native spline control keys retain uniform overrides", self.spline),
            ("Missing uniformly assigned material stays local", self.missing),
            ("Creating assigned material automatically repairs shading", self.repair_material),
            ("Deleting assigned material retains curves", self.delete_material),
            ("Recreating assigned material clears warning", self.repair_material),
            ("Uniform whole-curve point stylesheet uses native subjects", lambda: self.curve_styles(True)),
            ("Removing unsupported key stylesheet clears warning", lambda: self.curve_styles(False)),
            ("Object navigation retains mixed component bindings", lambda: pass_fixture.parm(self.obj, "tx").set(0.04)),
            ("Overlapping particle owners retain first complete binding", lambda: self.overlap(True)),
            ("Particle-owner repair restores unambiguous combinations", lambda: self.overlap(False)),
            ("Broken point override keeps supported primitive stylesheet", lambda: self.particle_fault(True)),
            ("Point override repair clears diagnostic", lambda: self.particle_fault(False)),
            ("No-style curve deformation retains key bindings", self.deformation),
            ("Off-frame varying key layer preserves independent curve motion", lambda: self.deformation(True)),
            ("Shutter key repair clears warning and keeps motion", self.deformation),
            ("Return to static uniform bindings", self.uniform),
        ]
