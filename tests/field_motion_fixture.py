# SPDX-License-Identifier: Apache-2.0
"""Analytic E[q*q] over the shutter on native mesh, sphere points and hair."""

from support.paths import artifacts_root
from pathlib import Path
import os
import hou
import numpy as np
from named_attribute_fixture import Fixture as Base

CASES = ("scalar", "integer", "vector", "float2", "color_layer", "uv_map", "default_color", "default_uv")


class Fixture:
    width, height = Base.width, Base.height

    def __init__(self, device):
        self.__dict__.update(Base(device).__dict__)
        self.dest = artifacts_root() / "examples/field-motion"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.animators = []
        self.invalids = []
        self.switches = []
        for index, (obj, stash, delete) in enumerate(zip(self.objects, self.stashes, self.deletes)):
            animate = obj.createNode("attribwrangle", "animated_fields")
            animate.setInput(0, stash)
            animate.parm("snippet").set(
                "float q=.5+(@Frame-floor(@Frame+.5))*2; f@mask=q; i@counter=int(floor(q*4+.5)); v@paint=set(q,q,q); u@map2=set(q,q); v@uv2=set(q,0,0); v@Cd=set(q,q,q); v@uv=set(q,0,0);"
            )
            self.animators.append(animate)
            delete.setInput(0, animate)
            invalid = obj.createNode("attribwrangle", "incompatible_shutter_field")
            invalid.setInput(0, delete)
            self.invalids.append(invalid)
            switch = obj.createNode("switch", "OUT_FIELD_MOTION")
            for n, node in enumerate((animate, delete, invalid)):
                switch.setInput(n, node)
            switch.setDisplayFlag(True)
            switch.setRenderFlag(True)
            self.switches.append(switch)
        controller = self.animators[0]
        group = controller.parmTemplateGroup()
        group.append(
            hou.IntParmTemplate(
                "test_broken", "Shutter Field: Valid / Missing / Invalid", 1, default_value=(0,), min=0, max=2
            )
        )
        group.append(
            hou.IntParmTemplate(
                "test_target", "Affected Component: Mesh / Points / Hair", 1, default_value=(0,), min=0, max=2
            )
        )
        controller.setParmTemplateGroup(group)
        self.control = controller.parm("test_broken")
        self.target = controller.parm("test_target")
        for index, switch in enumerate(self.switches):
            switch.parm("input").setExpression(
                'if($FF<floor($FF+.5) && ch("%s")==%d,ch("%s"),0)' % (self.target.path(), index, self.control.path()),
                hou.exprLanguage.Hscript,
            )
        self.camera.parm("shutter").set(0.5)
        self.rop.setParms(
            dict(
                samples=1024,
                ipr_samples=1024,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
            )
        )
        self.configure("scalar")

    def configure(self, case):
        mapped = (
            case if case not in ("default_color", "default_uv") else "vector" if case == "default_color" else "uv_map"
        )
        Base.configure(self, mapped)
        self.case = case
        self.control.set(0)
        self.target.set(0)
        self.rop.parm("shutteroffset").set(0)
        if case == "default_color":
            self.field = "Cd"
            self.source.parm("attribute").set("Cd")
        elif case == "default_uv":
            self.field = "uv"
            self.source.parm("attribute").set("uv")
        output = (
            "fac"
            if case in ("scalar", "integer")
            else "UV"
            if case in ("uv_map", "default_uv")
            else "vector"
            if case in ("vector", "float2", "default_color")
            else "color"
        )
        node = self.source
        if case not in ("scalar", "integer"):
            split = self.material.createNode("cycles::separate_xyz::1", "first_component")
            split.setNamedInput("vector", node, output)
            node = split
            output = "x"
        if case == "integer":
            scale = self.material.createNode("cycles::math::1", "integer_to_q")
            scale.setParms(dict(math_type="multiply", value2=0.25))
            scale.setNamedInput("value1", node, output)
            node = scale
            output = "value"
        square = self.material.createNode("cycles::math::1", "square")
        square.parm("math_type").set("multiply")
        for input in ("value1", "value2"):
            square.setNamedInput(input, node, output)
        self.surface.setNamedInput("color", square, "value")
        for delete, invalid in zip(self.deletes, self.invalids):
            delete.parm("ptdel").set(self.field)
            code = (
                ("v@" + self.field + "=set(0,0,0);")
                if case in ("scalar", "integer", "float2")
                else ("f@" + self.field + "=0;")
                if case in ("vector", "color_layer", "uv_map")
                else ("v@" + self.field + '=set(atof("nan"),0,0);')
            )
            invalid.parm("snippet").set(code)
        self.parm = self.control
        self.material.layoutChildren()

    def planes(self, frame):
        return Base.planes(self, frame)

    def edit(self, value):
        self.control.set(int(value))

    def check(self, frame, status, broken=False, baseline=None):
        p = self.planes(frame)
        rgb = p["combined"]
        position = p["position"]
        assert not status["error"] and status["state"] != "failed", status
        assert status["geometry_resources"] == status["object_resources"] == 3, status
        assert all(np.isfinite(values).all() for values in p.values())
        issues = status["source_diagnostics"]
        assert bool(issues) == bool(broken), (self.case, broken, issues)
        if broken:
            assert all(issue["code"].startswith("geometry.shutter_") for issue in issues), issues
            assert all(hou.node(issue["source"]) is not None for issue in issues), issues
            if self.case not in ("default_color", "default_uv"):
                assert all(issue["attribute"] == self.field for issue in issues), issues
        offset = self.rop.evalParm("shutteroffset")
        centre = 0.5 + 0.5 * offset
        for index, x in enumerate((-1.6, 0, 1.6)):
            mask = (rgb[:, :, 3] > 0.999) & (np.abs(position[:, :, 0] - x) < 0.65)
            assert mask.sum() > 5, (index, mask.sum())
            expected = centre * centre if broken and index == self.target.evalAsInt() else centre * centre + 1 / 12
            measured = float(rgb[mask, 0].mean())
            assert abs(measured - expected) < 0.006, (self.case, index, measured, expected)
        if baseline is not None:
            difference = float(np.abs(p["combined"] - baseline["combined"]).mean())
            assert difference > 0.001 if broken else difference < 1e-6, (self.case, broken, difference)
        return p
