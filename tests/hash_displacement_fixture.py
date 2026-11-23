# SPDX-License-Identifier: Apache-2.0
"""Native bit-pattern controls for independent retained/fresh height checks."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

import os
import struct
import time
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import menu_actions, native_scene, device_selection

CASES = ("scalar", "vector", "float2", "uv", "color", "packed", "parameter", "parameter_vector")
BITS = (0, 0x80000000, 1, 0x80000001)

SOURCE = """import hou,numpy as np
node=hou.pwd();g=node.geometry();face=g.createPolygon()
for xyz in ((-1.,-1.,0.),(1.,-1.,0.),(1.,1.,0.),(-1.,1.,0.)):
 p=g.createPoint();p.setPosition(xyz);face.addVertex(p)
g.addAttrib(hou.attribType.Point,'N',(0.,0.,1.))
bits=int(node.evalParm('bits'))&0xffffffff
if node.evalParm('sampled') and hou.time()>=0:bits=0
for name,size in (('uv',3),('Cd',3),('seed',1),('seed2',2),('seed3',3),('unused',1)):
 if node.evalParm('packed') and name.startswith('seed'):continue
 g.addAttrib(hou.attribType.Point,name,.25 if size==1 else tuple(.25 for i in range(size)))
 values=np.full((4,size),.25,np.float32);values.view(np.uint32)[:,0]=(int(node.evalParm('unused_bits'))&0xffffffff) if name=='unused' else bits
 g.setPointFloatAttribValuesFromString(name,values.tobytes())
"""
PACKED = """import hou,numpy as np
node=hou.pwd();g=node.geometry();g.addAttrib(hou.attribType.Prim,'seed',.25)
values=np.full(g.intrinsicValue('primitivecount'),int(node.evalParm('bits'))&0xffffffff,np.uint32)
g.setPrimFloatAttribValuesFromString('seed',values.tobytes())
"""


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


class Fixture:
    width = height = 64

    def __init__(self, device="CPU"):
        self.dest = artifacts_root() / "stage3/materials/hash-displacement"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.root = hou.node("/obj").createNode("subnet", "hash_displacement")
        self.obj = self.root.createNode("geo", "hash_height", run_init_scripts=False)
        self.source = self.obj.createNode("python", "authored_float_bits")
        for name in ("bits", "unused_bits", "packed", "sampled"):
            self.source.addSpareParmTuple(hou.IntParmTemplate(name, name, 1, default_value=(0,)))
        pass_fixture.parm(self.source, "python").set(SOURCE)
        self.pack = self.obj.createNode("pack", "prototype")
        self.pack.setInput(0, self.source)
        pass_fixture.parm(self.pack, "pivot").set("origin")
        self.packed = self.obj.createNode("python", "instance_bits")
        self.packed.setInput(0, self.pack)
        self.packed.addSpareParmTuple(hou.IntParmTemplate("bits", "Bits", 1, default_value=(0,)))
        pass_fixture.parm(self.packed, "bits").setExpression(
            'ch("../authored_float_bits/bits")', hou.exprLanguage.Hscript
        )
        pass_fixture.parm(self.packed, "python").set(PACKED)
        self.output = self.obj.createNode("switch", "OUT")
        self.output.setInput(0, self.source)
        self.output.setInput(1, self.packed)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.material = ch.create_material(name="hash_height", shader="emission")
        self.material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.6))
        pass_fixture.parm(self.material.node("surface_shader"), "strength").set(1)
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.material.path())
        self.attribute = self.material.createNode("cycles::attribute::1", "height_field")
        self.coordinates = self.material.createNode("cycles::texture_coordinate::1", "standard_uv")
        self.split = self.material.createNode("cycles::separate_xyz::1", "first_component")
        self.noise = self.material.createNode("cycles::white_noise_texture::1", "bit_sensitive_height")
        pass_fixture.parm(self.noise, "dimensions").set("1D")
        self.combine = self.material.createNode("cycles::combine_xyz::1", "vertical_offset")
        self.combine.setNamedInput("z", self.noise, "value")
        self.displacement = self.material.createNode("cycles::vector_displacement::1", "object_displacement")
        pass_fixture.set_parameters(self.displacement, dict(space="object", midlevel=0, scale=0.5))
        self.displacement.setNamedInput("vector", self.combine, "vector")
        self.material.node("surface_output").setNamedInput("displacement", self.displacement, "displacement")
        pass_fixture.parm(self.material, "cycles_displacement_method").set("true")
        self.camera = self.root.createNode("cam", "camera")
        pass_fixture.set_parameters(self.camera, dict(tz=5, projection=1, orthowidth=3, shutter=0.5))
        self.rop = menu_actions.create_rop()
        # The default this test was written for (before the ROP took Blender's defaults).
        self.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
        pass_fixture.set_parameters(
            self.rop,
            dict(
                objectroot=self.root.path(),
                vobject="*",
                lights="",
                camera=self.camera.path(),
                resx=self.width,
                resy=self.height,
                samples=4,
                ipr_samples=4,
                allowmotionblur=0,
                use_denoise=0,
                ipr_use_denoise=0,
                use_texture_cache=0,
                render_threads=2,
                world_source="color",
                film_exposure=1,
                pass_position=1,
                pass_normal=1,
            ),
        )
        device_selection.set_devices(self.rop, device if isinstance(device, list) else [device])
        self.rop.parmTuple("background").set((0, 0, 0))
        self.case = "scalar"
        self.bits = 0
        self.sampled = False
        self.configure("scalar")

    def configure(self, case, sampled=False):
        self.case = case
        self.sampled = sampled
        self.bits = 0
        pass_fixture.set_parameters(
            self.source, dict(bits=0, unused_bits=0, packed=int(case == "packed"), sampled=int(sampled))
        )
        pass_fixture.parm(self.output, "input").set(int(case == "packed"))
        pass_fixture.set_parameters(
            self.rop, dict(allowmotionblur=int(sampled), geometry_motion="deformation", motion_samples=3)
        )
        if case == "parameter":
            self.noise.setInput(self.noise.inputIndex("w"), None)
        elif case == "parameter_vector":
            self.split.setInput(self.split.inputIndex("vector"), None)
            self.noise.setNamedInput("w", self.split, "x")
        elif case == "uv":
            self.split.setNamedInput("vector", self.coordinates, "UV")
            self.noise.setNamedInput("w", self.split, "x")
        else:
            pass_fixture.parm(self.attribute, "attribute").set(
                dict(scalar="seed", vector="seed3", float2="seed2", color="Cd", packed="seed")[case]
            )
            if case in ("scalar", "packed"):
                self.noise.setNamedInput("w", self.attribute, "fac")
            else:
                self.split.setNamedInput("vector", self.attribute, "vector" if case != "color" else "color")
                self.noise.setNamedInput("w", self.split, "x")
        self.edit(0)

    def edit(self, bits):
        self.bits = bits
        pass_fixture.parm(self.source, "bits").set(bits if bits < 0x80000000 else bits - 0x100000000)
        if self.case.startswith("parameter"):
            value = struct.unpack("<f", struct.pack("<I", bits))[0]
            if self.case == "parameter":
                pass_fixture.parm(self.noise, "w").deleteAllKeyframes()
                pass_fixture.parm(self.noise, "w").setExpression(repr(value), hou.exprLanguage.Python)
                actual = pass_fixture.evaluate(self.noise, "w")
            else:
                for i, parm in enumerate(self.split.parmTuple("vector")):
                    parm.deleteAllKeyframes()
                    parm.setExpression(repr(value) if i == 0 else "0.0", hou.exprLanguage.Python)
                actual = self.split.evalParmTuple("vector")[0]
            assert np.float32(actual).view(np.uint32) == bits

    def verify_source(self):
        if self.case.startswith("parameter"):
            return
        geo = self.output.geometry()
        if self.case == "packed":
            raw = geo.primFloatAttribValuesAsString("seed")
            size = 1
        else:
            name, size = dict(
                scalar=("seed", 1), vector=("seed3", 3), float2=("seed2", 2), color=("Cd", 3), uv=("uv", 3)
            )[self.case]
            raw = geo.pointFloatAttribValuesAsString(name)
        bits = np.frombuffer(raw, np.uint32).reshape(-1, size)[:, 0]
        np.testing.assert_array_equal(
            bits, np.full(len(bits), 0 if self.sampled and hou.time() >= 0 else self.bits, np.uint32)
        )

    def update(self):
        self.verify_source()
        value = native_scene.capture(self.rop)
        value["passes"] = ["combined", "position", "normal", "depth"]
        return value

    def render(self, session):
        result = session.wait(session.apply(self.update(), reuse_if_unchanged=True), timeout=120)
        deadline = time.monotonic() + 15
        while True:
            status = session.status()
            if status["state"] == "failed" or (
                status["state"] == "complete" and status["generation"] == result["generation"]
            ):
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.005)
        assert status["state"] == "complete" and not status["error"], status
        return result, status

    def mask(self, frame):
        return (
            (plane(frame, "combined")[:, :, 3] > 0.999)
            & (plane(frame, "depth")[:, :, 0] > 0)
            & (plane(frame, "depth")[:, :, 0] < 100)
        )

    def check(self, frame, interior=False):
        mask = self.mask(frame)
        if interior:
            position = plane(frame, "position")
            mask &= (np.abs(position[:, :, 0]) < 0.5) & (np.abs(position[:, :, 1]) < 0.5)
        assert mask.sum() > (25 if interior else 100)
        np.testing.assert_allclose(
            plane(frame, "combined")[mask, :3], np.broadcast_to((0.2, 0.4, 0.6), (mask.sum(), 3)), rtol=0, atol=3e-5
        )
        return float(np.median(plane(frame, "position")[mask, 2]))
