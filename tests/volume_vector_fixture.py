# SPDX-License-Identifier: Apache-2.0
"""Native scalar triplets, explicit vector references and local recovery."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from dataclasses import dataclass
import os
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch


@dataclass(frozen=True)
class Case:
    mode: int = 0
    root: str = "tint"
    color: tuple | None = (0.2, 0.4, 0.8)
    fields: int = 1
    codes: frozenset = frozenset()


SHADER = frozenset({"volume.shader_field_unavailable"})
FIELD = frozenset({"volume.field_ignored"})
BASE = Case()
CASES = (
    BASE,
    Case(1, color=None, fields=0, codes=SHADER),
    BASE,
    Case(2, color=None, fields=0, codes=SHADER),
    BASE,
    Case(3, color=(0.6, 0.6, 0.6), fields=0),
    BASE,
    Case(4, fields=0),
    BASE,
    Case(5, color=None, fields=0, codes=SHADER),
    Case(5, root="tint.z", fields=0),
    Case(5, color=None, fields=0, codes=SHADER),
    BASE,
    Case(6, color=None, fields=0, codes=FIELD | SHADER),
    BASE,
    Case(7),
    Case(8, fields=0),
    BASE,
    Case(9, codes=FIELD),
    BASE,
    Case(1, root="tint.x", color=(0.2, 0.2, 0.2), fields=0),
    Case(1, color=None, fields=0, codes=SHADER),
    BASE,
    Case(root="unprovided", color=None, fields=0, codes=SHADER),
    BASE,
    Case(root="tint.x", color=(0.2, 0.2, 0.2), fields=0),
    BASE,
)


class Fixture:
    def __init__(self, device, *, scale=1, samples=8192, background=1):
        self.dest = artifacts_root() / "stage2/volumes/vector-components"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.obj = hou.node("/obj").createNode("geo", "vector_fields", run_init_scripts=False)
        self.density = self.volume("density", "density", value=0.35)
        self.vector = self.volume("vector", "tint", value=0.2)
        # Rank selects three named primitives; components selects voxel storage
        # within each primitive. The ordinary vector convention uses scalars.
        self.vector.setParms(dict(rank=1, components=1, initialval1=0.2, initialval2=0.4, initialval3=0.8))
        base = self.obj.createNode("merge", "physical_fields")
        base.setInput(0, self.density)
        base.setInput(1, self.vector)
        self.control = self.obj.createNode("attribwrangle", "faults")
        self.control.setInput(0, base)
        self.control.addSpareParmTuple(hou.IntParmTemplate("mode", "Field test", 1, min=0, max=9))
        self.control.setParms({"class": 1, "snippet": 'if(chi("mode")==1 && s@name=="tint.z")s@name="unused_z";'})
        shifted = self.obj.createNode("xform", "misaligned_component")
        shifted.setInput(0, self.control)
        shifted.setParms(dict(group="@name=tint.z", tz=0.25))
        explicit_scalar = self.volume("explicit_scalar", "tint", value=0.6)
        scalar = self.merge("with_explicit_scalar", self.control, explicit_scalar)
        joined = self.obj.createNode("volumevectorjoin", "native_vector_reference")
        joined.setInput(0, self.control)
        joined.setParms(
            dict(
                setcomponents=1,
                components=3,
                keep=1,
                xgroup="@name=tint.x",
                ygroup="@name=tint.y",
                zgroup="@name=tint.z",
                wgroup="",
            )
        )
        joined_only = self.obj.createNode("volumevectorjoin", "one_vector")
        joined_only.setInput(0, self.control)
        joined_only.setParms(
            dict(
                setcomponents=1,
                components=3,
                keep=0,
                xgroup="@name=tint.x",
                ygroup="@name=tint.y",
                zgroup="@name=tint.z",
                wgroup="",
            )
        )
        only_vector = self.blast("only_vector", joined_only, "@name=tint", keep=True)
        vector_z = self.obj.createNode("attribwrangle", "vector_in_scalar_slot")
        vector_z.setInput(0, only_vector)
        vector_z.setParms({"class": 1, "snippet": 's@name="tint.z";'})
        wrong_type = self.merge("wrong_component_type", self.blast("without_z", self.control, "@name=tint.z"), vector_z)
        bad_file = self.dest / "nan-field.bgeo.sc"
        ch._native.write_dense_volume_fixture(str(bad_file), 4)
        file = self.obj.createNode("file", "invalid_voxel_source")
        file.parm("file").set(str(bad_file))
        nan = self.obj.createNode("attribwrangle", "nan_component")
        nan.setInput(0, self.blast("only_nan", file, "@name=temperature", keep=True))
        nan.setParms({"class": 1, "snippet": 's@name="tint.y";'})
        invalid = self.merge("nonfinite_component", self.blast("without_y", self.control, "@name=tint.y"), nan)
        sparse = self.obj.createNode("convertvdb", "sparse_scalar_components")
        sparse.setInput(0, self.control)
        sparse.setParms(dict(conversion="vdb", vdbclass="fog"))
        vdb_joined = self.obj.createNode("vdbvectormerge", "independent_sparse_vector_reference")
        vdb_joined.setInput(0, sparse)
        vdb_joined.setParms(
            dict(
                xgroup="@name=tint.x",
                ygroup="@name=tint.y",
                zgroup="@name=tint.z",
                usexname=1,
                remove_sources=0,
                copyinactive=1,
                vectype=0,
            )
        )
        duplicate = self.merge("duplicate_component", self.control, self.volume("second_z", "tint.z", value=0.15))
        self.switch = self.obj.createNode("switch", "selected_fields")
        for index, node in enumerate(
            (self.control, self.control, shifted, scalar, joined, wrong_type, invalid, sparse, vdb_joined, duplicate)
        ):
            self.switch.setInput(index, node)
        self.switch.parm("input").setExpression('ch("../faults/mode")', hou.exprLanguage.Hscript)
        self.material = ch.create_material(name="vector_emission", shader="principled_volume")
        self.material.node("volume_shader").destroy()
        self.read = self.material.createNode("cycles::attribute::1", "field")
        self.read.parm("attribute").set("tint")
        self.info = self.material.createNode("cycles::volume_info::1", "standard_fields")
        self.emit = self.material.createNode("cycles::emission::1", "emission")
        self.emit.parm("strength").set(1)
        self.emit.setNamedInput("color", self.read, "color")
        self.material.node("volume_output").setNamedInput("volume", self.emit, "emission")
        self.obj.parm("shop_materialpath").set(self.material.path())
        sentinel = ch.create_material(name="vector_sentinel", shader="emission")
        sentinel.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
        sentinel.node("surface_shader").parm("strength").set(1)
        box = self.obj.createNode("box", "unaffected_mesh")
        box.parmTuple("size").set((0.3, 0.5, 0.2))
        box.parm("tx").set(1.5)
        binding = self.obj.createNode("attribwrangle", "mesh_material")
        binding.setInput(0, box)
        binding.setParms({"class": 1, "snippet": 's@shop_materialpath="' + sentinel.path() + '";'})
        self.output = self.merge("OUT", self.switch, binding)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "vector_camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=4))
        self.rop = hou.node("/out").createNode("cycles_ipr", "vector_components")
        set_rop_devices(self.rop, device)
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=80 * scale,
                resy=48 * scale,
                samples=samples,
                ipr_samples=samples,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                use_material_assignments=1,
            )
        )
        self.rop.parmTuple("background").set((background, background, background))

    def volume(self, node_name, field_name, *, value):
        node = self.obj.createNode("volume", node_name)
        node.setParms(dict(name=field_name, initialval1=value, uniformsamples=0, divsx=19, divsy=13, divsz=11))
        node.parmTuple("size").set((2, 1.5, 1))
        return node

    def merge(self, name, *inputs):
        node = self.obj.createNode("merge", name)
        for index, input in enumerate(inputs):
            node.setInput(index, input)
        return node

    def blast(self, name, input, group, *, keep=False):
        node = self.obj.createNode("blast", name)
        node.setInput(0, input)
        node.setParms(dict(group=group, negate=int(keep)))
        return node


def check(frame, status, case, *, geometry_extra=0, object_extra=0, background=1):
    plane = frame["planes"]["combined"]
    image = np.frombuffer(plane["pixels"], np.float32).reshape(frame["height"], frame["width"], plane["channels"])[
        :, :, :3
    ]
    scale = frame["width"] // 80
    measured = image[21 * scale : 27 * scale, 37 * scale : 43 * scale].mean((0, 1))
    assert np.isfinite(image).all() and status["state"] != "failed" and not status["motion_warnings"], status
    assert status["geometry_resources"] == 2 + geometry_extra and status["object_resources"] == 2 + object_extra, status
    assert {issue["code"] for issue in status["source_diagnostics"]} == case.codes, status
    assert status["volume_vector_fields"] == case.fields and bool(status["volume_vector_source_bytes"]) == bool(
        case.fields
    ), status
    if case.color is None:
        assert measured[0] > measured[1] + 0.03 and measured[2] > measured[1] + 0.03, measured
    else:
        expected = np.array(case.color) + background
        np.testing.assert_allclose(measured, expected, atol=0.006, rtol=0)
    np.testing.assert_allclose(image[24 * scale, 70 * scale], (0.1, 0.7, 0.2), atol=1e-4)
    return image.copy(), measured.tolist()
