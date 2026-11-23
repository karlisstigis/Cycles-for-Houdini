# SPDX-License-Identifier: Apache-2.0
"""Native packed geometry, with numeric fields on copies rather than prototypes."""

from support.paths import artifacts_root
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, device_selection, native_scene


class Fixture:
    width, height = 160, 100

    def __init__(self, device="CPU"):
        self.dest = artifacts_root() / "stage3/materials/packed-instance-attributes"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.scene = hou.node("/obj").createNode("subnet", "packed_attribute_fixture")
        self.obj = self.scene.createNode("geo", "copies", run_init_scripts=False)
        self.grid = self.obj.createNode("grid", "surface_prototype")
        self.grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.grid.parmTuple("size").set((0.8, 0.7))
        self.point = self.obj.createNode("add", "point_prototype")
        self.point.parm("points").set(1)
        self.hair = self.obj.createNode("line", "hair_prototype")
        self.hair.parmTuple("origin").set((-0.3, 0, 0))
        self.hair.parmTuple("dir").set((1, 0, 0))
        self.hair.parm("dist").set(0.6)
        self.mixed = self.obj.createNode("merge", "mixed_prototype")
        for i, node in enumerate((self.grid, self.point, self.hair)):
            self.mixed.setInput(i, node)
        self.shape = self.obj.createNode("switch", "prototype_kind")
        for i, node in enumerate((self.grid, self.point, self.hair, self.mixed)):
            self.shape.setInput(i, node)
        self.prototype_fields = self.obj.createNode("attribwrangle", "prototype_attributes")
        self.prototype_fields.setInput(0, self.shape)
        self.targets = self.obj.createNode("grid", "four_placements")
        self.targets.setParms(dict(type=0, orient=0, rows=2, cols=2))
        self.targets.parmTuple("size").set((3, 2))
        self.orientation = self.obj.createNode("attribwrangle", "placement_orientation")
        self.orientation.setInput(0, self.targets)
        self.orientation.parm("snippet").set("v@N={0,0,1}; v@up={0,1,0};")
        self.copies = self.obj.createNode("copytopoints", "shared_copies")
        self.copies.setInput(0, self.prototype_fields)
        self.copies.setInput(1, self.orientation)
        self.copies.parm("pack").set(1)
        self.fields = self.obj.createNode("attribwrangle", "instance_attributes")
        self.fields.setInput(0, self.copies)
        self.nested = self.obj.createNode("pack", "nested_copies")
        self.nested.setInput(0, self.fields)
        self.nested.parm("pivot").set("origin")
        self.outer = self.obj.createNode("attribwrangle", "ancestor_attribute")
        self.outer.setInput(0, self.nested)
        self.outer.setParms({"class": 1, "snippet": "f@rand=.9;"})
        self.output = self.obj.createNode("switch", "OUT")
        self.output.setInput(0, self.fields)
        self.output.setInput(1, self.outer)
        self.output.setRenderFlag(True)
        self.output.setDisplayFlag(True)
        self.material = ch.create_material(name="packed_instance_fields", shader="emission")
        self.emission = self.material.node("surface_shader")
        self.emission.parm("strength").set(1)
        self.rand = self.material.createNode("cycles::attribute::1", "random_float")
        self.rand.parm("attribute").set("rand")
        self.counter = self.material.createNode("cycles::attribute::1", "integer_field")
        self.counter.parm("attribute").set("counter")
        self.scale = self.material.createNode("cycles::math::1", "integer_scale")
        self.scale.setParms(dict(math_type="multiply", value2=0.1))
        self.scale.setNamedInput("value1", self.counter, "fac")
        self.combine = self.material.createNode("cycles::combine_xyz::1", "field_colors")
        self.combine.setNamedInput("x", self.rand, "fac")
        self.combine.setNamedInput("y", self.scale, "value")
        self.combine.parm("z").set(0.3)
        self.emission.setNamedInput("color", self.combine, "vector")
        self.obj.parm("shop_materialpath").set(self.material.path())
        self.displacement = self.material.createNode("cycles::displacement::1", "instance_height")
        self.displacement.setParms(dict(scale=1, midlevel=0, space="object"))
        self.displacement.setNamedInput("height", self.scale, "value")
        self.camera = self.scene.createNode("cam", "camera")
        self.camera.setParms(dict(tz=6, projection=1, orthowidth=6.4))
        self.rop = menu_actions.create_rop()
        device_selection.set_devices(self.rop, [device])
        self.rop.setParms(
            dict(
                objectroot=self.scene.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=4,
                ipr_samples=4,
                use_denoise=0,
                ipr_use_denoise=0,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                film_exposure=1,
                allowmotionblur=0,
                use_texture_cache=0,
                render_threads=2,
                world_source="color",
                point_radius=0.2,
                curve_width=0.15,
            )
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.rop.parmTuple("lightstrength").set((0, 0, 0))
        self.mode = "good"
        self.set_fields()

    def set_fields(self, mode="good", owner="primitive", offset=0):
        self.mode = mode
        self.offset = offset
        self.fields.parm("class").set(
            self.fields.parm("class").menuItems().index("primitive" if owner == "primitive" else "point")
        )
        source = "@primnum" if owner == "primitive" else "@ptnum"
        head = "int k=" + source + "; i@counter=k+1;"
        if mode == "bad_type":
            code = head + 's@rand="unused string as a color source";'
        elif mode == "missing":
            code = head
        elif mode in ("vector", "bad_vector"):
            code = head + "v@rand=set(.2+.2*k+" + str(offset) + ",.1*(k+1),.3);"
            if mode == "bad_vector":
                code += 'if(k==1) v@rand.y=atof("nan");'
        elif mode == "float2":
            code = head + "u@rand=set(.2+.2*k+" + str(offset) + ",.1*(k+1));"
        else:
            if mode == "bad_height":
                head = "int k=" + source + '; f@counter=k+1; if(k==1)f@counter=atof("nan");'
            code = head + "f@rand=.2+.2*k+" + str(offset) + ";"
            if mode == "bad_one":
                code += 'if(k==1) f@rand=atof("nan");'
            if mode == "bad_counter":
                code += "if(k==1) i@counter=0;"
            if mode == "animation":
                code = head + 'f@rand=.1*@Frame+.1*k; if(@Frame==3 && k==1)f@rand=atof("nan");'
        self.fields.parm("snippet").set(code)

    def update(self):
        result = native_scene.capture(self.rop)
        result["passes"] = ["combined", "position", "depth"]
        return result

    def colors(self, frame, expected=None, warning=False):
        p = frame["planes"]["combined"]
        image = np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])
        assert np.isfinite(image).all()
        colors = np.unique(np.round(image[image[:, :, 3] > 0.999, :3], 4), axis=0)
        if expected is None:
            expected = [(0.2 + 0.2 * k + self.offset, 0.1 * (k + 1), 0.3) for k in range(4)]
            if self.mode in ("bad_one", "bad_height", "bad_vector"):
                expected[1] = (1, 0, 1)
            elif self.mode in ("bad_type", "missing"):
                expected = [(1, 0, 1)]
            elif self.mode == "float2":
                expected = [(0.2 + 0.2 * k + self.offset, 0.1 * (k + 1), 0) for k in range(4)]
        for color in expected:
            distances = np.max(np.abs(colors - np.asarray(color)), axis=1)
            assert distances.min() < 3e-4, (color, colors)
        if not warning:
            assert not np.any(np.all(np.isclose(colors, (1, 0, 1), atol=0.001), axis=1)), colors
        return colors.tolist()

    def check_displacement(self, frame, disabled_one=False):
        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])

        beauty = plane("combined")
        position = plane("position")
        normal = float(self.grid.geometry().prims()[0].normal()[2])
        assert abs(normal) == 1, normal
        for k in range(4):
            color = (
                (1, 0, 1)
                if k == 1 and self.mode in ("bad_one", "bad_height")
                else (0.2 + 0.2 * k + self.offset, 0.1 * (k + 1), 0.3)
            )
            mask = (beauty[:, :, 3] > 0.999) & np.all(np.isclose(beauty[:, :, :3], color, atol=3e-4, rtol=0), axis=2)
            expected = 0 if disabled_one and k == 1 else normal * 0.1 * (k + 1)
            assert mask.sum() > 4, (k, color)
            np.testing.assert_allclose(position[:, :, 2][mask], expected, atol=2e-4, rtol=0)

    def add_heavy(self):
        geo = self.scene.createNode("geo", "retained_heavy_prototype", run_init_scripts=False)
        self.heavy = geo.createNode("torus")
        self.heavy.setParms(dict(type=0, rows=512, cols=1024))
        targets = geo.createNode("grid")
        targets.setParms(dict(type=0, rows=3, cols=4))
        targets.parmTuple("t").set((0, 0, 20))
        copies = geo.createNode("copytopoints")
        copies.setInput(0, self.heavy)
        copies.setInput(1, targets)
        copies.parm("pack").set(1)
        copies.setRenderFlag(True)
        copies.setDisplayFlag(True)
        mat = ch.create_material(name="heavy_constant", shader="emission")
        # The default this test was written for (new nodes now take Blender's).
        mat.node("surface_shader").setParms(dict(strength=10.0))
        mat.node("surface_shader").parmTuple("color").set((0.3, 0.4, 0.5))
        geo.parm("shop_materialpath").set(mat.path())
        return geo
