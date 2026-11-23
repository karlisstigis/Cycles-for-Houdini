# SPDX-License-Identifier: Apache-2.0
"""Animated shader values/images on a static mesh, with independent motion reference."""

from support.paths import artifacts_root
import os
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
from sampled_displacement_fixture import Fixture as SpatialFixture

CASES = ("parameter", "image", "scene_time", "deformation")


def edit_source(node):
    broken = bool(node.evalParm("test_broken"))
    path = Path(node.userData("temporal_file"))
    backup = path.with_suffix(".disabled")
    if node.userData("temporal_case") == "image":
        if broken and path.exists():
            path.replace(backup)
        elif not broken and backup.exists():
            backup.replace(path)
    else:
        hou.node(node.userData("temporal_rop")).parm("allowmotionblur").set(not broken)


class Fixture(SpatialFixture):
    def __init__(self, device):
        self.initializing = True
        super().__init__(device)
        self.initializing = False
        self.dest = artifacts_root() / "examples/temporal-displacement"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.obj.parm("tx").set(-0.5)
        self.obj.node("grid1").parmTuple("size").set((1.5, 1))
        self.sop.parm("snippet").set("v@N=set(1,0,0);")
        self.image = self.material.createNode("cycles::image_texture::1", "animated_height")
        self.image.setParms(dict(colorspace="data", interpolation="closest"))
        self.image.setNamedInput("vector", self.geometry, "position")
        self.time = self.material.createNode("cycles::scene_time::1", "shutter_time")
        self.math = self.material.createNode("cycles::math::1", "height_from_time")
        self.math.setParms(dict(math_type="multiply_add", value2=72, value3=0.5))
        self.math.setNamedInput("value1", self.time, "seconds")
        self.file = self.dest / "height-0.exr"
        for i in range(-6, 11):
            output = oiio.ImageOutput.create(str(self.dest / f"height-{i}.exr"))
            assert output.open(str(self.dest / f"height-{i}.exr"), oiio.ImageSpec(2, 2, 3, oiio.FLOAT))
            assert output.write_image(np.full((2, 2, 3), -0.25 + i * 0.375, np.float32))
            assert output.close()
        self.filename = str(self.dest).replace("\\", "/") + "/height-`rint($T*192+2)`.exr"
        self.source.setUserData("temporal_file", str(self.file))
        self.source.setUserData("temporal_rop", self.rop.path())
        pt = self.source.parmTemplateGroup()
        parm = pt.find("test_broken")
        parm.setLabel("Test Recovery / Motion Toggle")
        parm.setScriptCallback(
            "import temporal_displacement_fixture; temporal_displacement_fixture.edit_source(kwargs['node'])"
        )
        pt.replace("test_broken", parm)
        self.source.setParmTemplateGroup(pt)
        self.parm = self.source.parm("test_broken")
        self.configure(CASES[0])

    def configure(self, case):
        if self.initializing:
            return super().configure("normal")
        self.edit(False)
        self.case = case
        self.source.setUserData("temporal_case", case)
        self.sop.parm("snippet").set("v@N=set(1,0,0);" + ("@P.x+=@Time*48;" if case == "deformation" else ""))
        self.scalar.setInput(self.scalar.inputIndex("height"), None)
        self.scalar.parm("height").deleteAllKeyframes()
        self.scalar.parm("height").set(0.5)
        if case in ("parameter", "deformation"):
            self.scalar.parm("height").setExpression(
                ".5+$T*24" if case == "deformation" else ".5+$T*72", hou.exprLanguage.Hscript
            )
        elif case == "image":
            self.image.parm("filename").set(self.filename)
            self.scalar.setNamedInput("height", self.image, "color")
        elif case == "scene_time":
            self.scalar.setNamedInput("height", self.math, "value")
        else:
            raise AssertionError(case)
        self.material.node("surface_output").setNamedInput("displacement", self.scalar, "displacement")
        self.material.parm("cycles_displacement_method").set("true")
        self.rop.parm("allowmotionblur").set(1)
        self.parm.set(0)

    def edit(self, broken):
        if self.initializing:
            return super().edit(broken)
        self.parm.set(int(broken))
        self.parm.pressButton()

    def check(self, frame, status, broken, baseline=None):
        assert not status["error"], status
        assert status["geometry_resources"] == status["object_resources"] == 1, status
        issues = status["source_diagnostics"]
        assert bool(issues) == bool(broken and self.case == "image"), (self.case, broken, issues)
        planes = {}
        for name, p in frame["planes"].items():
            values = np.frombuffer(p["pixels"], np.float32).reshape(90, 160, p["channels"])
            assert np.isfinite(values).all(), name
            planes[name] = values.copy()
        assert planes["combined"][:, :, :3].max() > 0.1
        if not broken and baseline is not None:
            assert np.allclose(planes["combined"], baseline["combined"], atol=1e-5), (
                "Temporal shader repair changed the blurred image"
            )
        return planes
