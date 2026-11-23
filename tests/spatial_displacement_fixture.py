# SPDX-License-Identifier: Apache-2.0
"""Native SOP geometry for the independently authored spatial reference scenes."""

from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import math
import hou
import cycles_houdini as ch


class Fixture:
    def __init__(self, device):
        hou.setFps(24)
        hou.setFrame(1)
        self.parent_angle = 0
        self.materials = []
        for name in ("spatial_main", "spatial_shutter", "spatial_seam"):
            material = ch.create_material(name=name, shader="emission")
            surface = material.node("surface_shader")
            pass_fixture.parm(surface, "strength").set(1)
            coordinate = material.createNode("cycles::texture_coordinate::1", "coordinates")
            multiply = material.createNode("cycles::vector_math::1", "generated_offset")
            pass_fixture.parm(multiply, "math_type").set("multiply")
            multiply.parmTuple("vector2").set((0.24, 0.12, 0.08))
            multiply.setNamedInput("vector1", coordinate, "generated")
            color = material.createNode("cycles::vector_math::1", "generated_color")
            pass_fixture.parm(color, "math_type").set("multiply")
            color.parmTuple("vector2").set((0.7, 0.5, 0))
            color.setNamedInput("vector1", coordinate, "generated")
            add = material.createNode("cycles::vector_math::1", "color_bias")
            pass_fixture.parm(add, "math_type").set("add")
            add.parmTuple("vector2").set((0.1, 0.15, 0.3))
            add.setNamedInput("vector1", color, "vector")
            split = material.createNode("cycles::separate_xyz::1", "generated_x")
            split.setNamedInput("vector", coordinate, "generated")
            scalar = material.createNode("cycles::displacement::1", "scalar_offset")
            pass_fixture.set_parameters(scalar, dict(midlevel=0, scale=0.25, space="object"))
            scalar.setNamedInput("height", split, "x")
            transform = material.createNode("cycles::vector_transform::1", "transform")
            transform.parmTuple("vector").set((0.25, 0.1, 0.2))
            geometry = material.createNode("cycles::geometry::1", "geometry")
            vector = material.createNode("cycles::vector_displacement::1", "vector_offset")
            pass_fixture.set_parameters(vector, dict(midlevel=0, scale=0.4, space="object"))
            pass_fixture.parm(material, "cycles_displacement_method").set("true")
            self.materials.append(
                dict(
                    material=material,
                    surface=surface,
                    coordinate=coordinate,
                    multiply=multiply,
                    color=add,
                    scalar=scalar,
                    transform=transform,
                    vector=vector,
                    geometry=geometry,
                )
            )
        self.obj = hou.node("/obj").createNode("geo", "spatial_surface", run_init_scripts=False)
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.materials[0]["material"].path())
        self.obj.addSpareParmTuple(hou.FloatParmTemplate("object_rate", "Rotation Rate", 1, default_value=(18,)))
        self.grid = self.obj.createNode("grid", "base_grid")
        pass_fixture.set_parameters(self.grid, dict(type=0, orient=0, rows=2, cols=2))
        self.grid.parmTuple("size").set((1.4, 1))
        reverse = self.obj.createNode("reverse", "front_faces")
        reverse.setInput(0, self.grid)
        self.sop = self.obj.createNode("attribwrangle", "shape_motion")
        self.sop.setInput(0, reverse)
        self.sop.addSpareParmTuple(hou.FloatParmTemplate("shape_rate", "Shape Rate", 1, default_value=(48,)))
        pass_fixture.parm(self.sop, "snippet").set(
            'vector p=@P;float rate=chf("shape_rate"),h=sin(@Time*rate),d=cos(@Time*rate)*rate;@P=set(p.x*(1+.4*h)+.12*sin(@Time*24),p.y+.6*h*p.x,.25*h*p.x);v@v=set(p.x*.4*d+.12*24*cos(@Time*24),p.x*.6*d,p.x*.25*d);'
        )
        self.output = self.obj.createNode("attribwrangle", "OUT")
        self.output.setInput(0, self.sop)
        pass_fixture.parm(self.output, "class").set(1)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "spatial_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=5, shutter=0.5))
        self.rop = hou.node("/out").createNode("cycles_ipr", "spatial_displacement")
        # The default this test was written for (before the ROP took Blender's defaults).
        self.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
        set_rop_devices(self.rop, device)
        configure_render(
            self.rop,
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=160,
                resy=96,
                samples=1024,
                ipr_samples=1024,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                use_material_assignments=1,
                pass_position=1,
                pass_normal=1,
                pass_depth=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))

    def configure(self, spec):
        self.spec = dict(spec)
        pass_fixture.set_parameters(self.grid, dict(rows=2, cols=2))
        pass_fixture.parm(self.sop, "shape_rate").set(spec["shape_rate"])
        pass_fixture.parm(self.obj, "object_rate").set(spec["object_rate"])
        configure_render(
            self.rop,
            dict(
                shutteroffset=0,
                geometry_motion="velocity" if spec.get("velocity") else "deformation",
                subdivision=dict(type="none"),
            ),
        )
        for index, nodes in enumerate(self.materials):
            surface = nodes["surface"]
            surface.setInput(surface.inputIndex("color"), None)
            surface.parmTuple("color").set((0.2, 0.4, 0.8))
            pass_fixture.parm(nodes["coordinate"], "from_dupli").set(0)
            vector = nodes["vector"]
            vector.setInput(vector.inputIndex("vector"), None)
            pass_fixture.set_parameters(vector, dict(space="world" if spec["world"] else "object", scale=0.4))
            if spec["mode"] in (1, 2) or spec.get("surface_generated"):
                surface.setNamedInput("color", nodes["color"], "vector")
            if spec["mode"] == 2:
                displacement = nodes["scalar"]
            else:
                displacement = vector
                if spec["mode"] == 1 or (spec["mode"] == 3 and index > 0):
                    pass_fixture.parm(vector, "scale").set(1)
                    vector.setNamedInput("vector", nodes["multiply"], "vector")
                elif spec["mode"] == 3:
                    pass_fixture.parm(vector, "scale").set(1)
                    vector.parmTuple("vector").set((0.12, 0.06, 0))
                else:
                    pass_fixture.set_parameters(
                        nodes["transform"],
                        dict(
                            transform_type=("vector", "point", "normal")[spec["kind"]],
                            convert_from=("world", "object", "camera")[spec["from"]],
                            convert_to=("world", "object", "camera")[spec["to"]],
                        ),
                    )
                    transform = nodes["transform"]
                    transform.setInput(transform.inputIndex("vector"), None)
                    for parm in transform.parmTuple("vector"):
                        parm.deleteAllKeyframes()
                    transform.parmTuple("vector").set((spec["input_x"], 0.1, 0.2))
                    if spec.get("input_mode"):
                        transform.setNamedInput(
                            "vector", nodes["geometry"], "position" if spec["input_mode"] == 1 else "normal"
                        )
                    elif spec.get("input_animation"):
                        transform.parmTuple("vector")[0].setExpression(
                            str(spec["input_x"]) + "+$T*12", hou.exprLanguage.Hscript
                        )
                    vector.setNamedInput("vector", transform, "vector")
            nodes["material"].node("surface_output").setNamedInput("displacement", displacement, "displacement")
        pass_fixture.parm(self.output, "snippet").set(
            'if(@Time>0)s@shop_materialpath="' + self.materials[1]["material"].path() + '";'
            if spec["mode"] == 3
            else ""
        )
        pass_fixture.parm(self.obj, "sx").setExpression(
            ("-1*" if spec["mirror"] else "") + "(1.15+.15*sin($T*24*180/$PI))", hou.exprLanguage.Hscript
        )
        pass_fixture.parm(self.obj, "sy").setExpression(".85+.1*sin($T*36*180/$PI)", hou.exprLanguage.Hscript)
        pass_fixture.parm(self.obj, "sz").set(1.2)
        self.transforms()
        pass_fixture.parm(self.camera, "tx").deleteAllKeyframes()
        if spec["camera_motion"]:
            pass_fixture.parm(self.camera, "tx").setExpression(
                str(spec["camera_x"]) + "+.4*sin($T*24*180/$PI)", hou.exprLanguage.Hscript
            )
        else:
            pass_fixture.parm(self.camera, "tx").set(spec["camera_x"])
        pass_fixture.parm(self.camera, "rz").set(math.degrees(spec["camera_angle"]))

    def transforms(self, parent_angle=0):
        self.parent_angle = parent_angle
        if parent_angle:
            if not hasattr(self, "parent"):
                self.parent = hou.node("/obj").createNode("null", "spatial_parent")
            pass_fixture.parm(self.parent, "rz").set(math.degrees(parent_angle))
            self.obj.setInput(0, self.parent)
        else:
            self.obj.setInput(0, None)
        x = "(.1+.2*sin($T*24*180/$PI))"
        y = "(-.05+.1*sin($T*12*180/$PI))"
        pass_fixture.parm(self.obj, "tx").setExpression(
            str(math.cos(parent_angle)) + "*" + x + "+" + str(math.sin(parent_angle)) + "*" + y,
            hou.exprLanguage.Hscript,
        )
        pass_fixture.parm(self.obj, "ty").setExpression(
            str(-math.sin(parent_angle)) + "*" + x + "+" + str(math.cos(parent_angle)) + "*" + y,
            hou.exprLanguage.Hscript,
        )
        pass_fixture.parm(self.obj, "tz").set(0.15)
        pass_fixture.parm(self.obj, "rz").setExpression(
            "(.15-" + str(parent_angle) + '+$T*ch("object_rate"))*180/$PI', hou.exprLanguage.Hscript
        )
