# SPDX-License-Identifier: Apache-2.0
"""A small native curve-node reference scene (inline native ramps)."""

import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch

ROOT = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])


class Fixture:
    def __init__(self, device, width=256, height=32):
        self.width, self.height = width, height
        self.builder = ch.create_material(name="shader_curves", shader="emission")
        self.emission = self.builder.node("surface_shader")
        pass_fixture.parm(self.emission, "strength").set(1)
        self.nodes = {
            kind: self.builder.createNode("cycles::" + kind + "::1")
            for kind in ("float_curve", "rgb_curves", "vector_curves")
        }
        self.obj = hou.node("/obj").createNode("geo", "curve_reference", run_init_scripts=False)
        self.grid = self.obj.createNode("grid")
        pass_fixture.set_parameters(self.grid, dict(type=0, orient=0, rows=2, cols=2))
        self.grid.parmTuple("size").set((8, 8))
        self.grid.setDisplayFlag(True)
        self.grid.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "curve_camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "shader_curves")
        set_rop_devices(self.rop, device)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                soppath=self.grid.path(),
                camera=self.camera.path(),
                materialpath=self.builder.path(),
                vobject=self.obj.path(),
                resx=width,
                resy=height,
                samples=1,
                ipr_samples=1,
                pass_position=1,
                pass_normal=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.rop.parmTuple("lightstrength").set((0, 0, 0))
        self.input = self.builder.createNode("cycles::value::1", "input_value")
        pass_fixture.parm(self.input, "value").set(0.425)
        self.connect(self.nodes["float_curve"])

    def connect(self, node):
        self.active = node
        self.emission.setNamedInput("color", node, "value")

    def constant(self, value):
        self.active.setNamedInput("value", self.input, "value")
        pass_fixture.parm(self.input, "value").set(value)

    def bands(self):
        geometry = self.builder.createNode("cycles::geometry::1")
        separate = self.builder.createNode("cycles::separate_xyz::1")
        separate.setNamedInput("vector", geometry, "position")
        previous = separate
        socket = "x"
        for operation, constant in (
            ("divide", 4.0),
            ("add", 0.5),
            ("multiply", 16.0),
            ("floor", 0.0),
            ("divide", 15.0),
            ("multiply", 1.5),
            ("subtract", 0.25),
        ):
            node = self.builder.createNode("cycles::math::1")
            pass_fixture.parm(node, "math_type").set(operation)
            pass_fixture.parm(node, "value2").set(constant)
            node.setNamedInput("value1", previous, socket)
            previous = node
            socket = "value"
        for curve in self.nodes.values():
            curve.setNamedInput("value", previous, "value")

    def image(self, frame, pass_name="combined"):
        plane = frame["planes"][pass_name]
        return np.frombuffer(plane["pixels"], np.float32).reshape(self.height, self.width, plane["channels"])
