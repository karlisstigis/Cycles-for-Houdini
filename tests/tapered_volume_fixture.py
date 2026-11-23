# SPDX-License-Identifier: Apache-2.0
"""Native typed frusta, independent continuous references and local fault repairs."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import numpy as np
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from cycles_houdini.device_selection import set_devices
from tapered_volume_source import build, descriptors
import tapered_volume_reference as reference

ROOT = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
SENTINEL = (0.12, 0.7, 0.22)


class Fixture:
    def __init__(self, device, *, heavy=False, samples=2048):
        self.dest = artifacts_root() / "stage3/volumes/tapered"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.obj = hou.node("/obj").createNode("geo", "tapered_volume", run_init_scripts=False)
        self.control = build(self.obj)
        self.absorb = ch.create_material(name="tapered_absorption", shader="absorption_volume")
        self.absorb.node("volume_shader").parmTuple("color").set((0, 0, 0))
        self.integer = ch.create_material(name="tapered_integer_absorption", shader="absorption_volume")
        self.integer.node("volume_shader").parmTuple("color").set((0, 0, 0))
        self.integer.node("density_scale").parm("value2").set(0.42)
        self.emission = ch.create_material(name="tapered_vector_emission", shader="principled_volume")
        self.emission.node("volume_shader").destroy()
        self.read = self.emission.createNode("cycles::attribute::1", "native_tint")
        self.read.parm("attribute").set("tint")
        emit = self.emission.createNode("cycles::emission::1", "field_emission")
        emit.parm("strength").set(1)
        emit.setNamedInput("color", self.read, "color")
        self.emission.node("volume_output").setNamedInput("volume", emit, "emission")
        self.control.setParms(
            dict(
                float_material=self.absorb.path(),
                integer_material=self.integer.path(),
                emission_material=self.emission.path(),
            )
        )
        self.sentinel = ch.create_material(name="tapered_unaffected_mesh", shader="emission")
        self.sentinel.node("surface_shader").parmTuple("color").set(SENTINEL)
        self.sentinel.node("surface_shader").parm("strength").set(1)
        box = self.obj.createNode("box", "unaffected_mesh")
        box.parmTuple("size").set((0.3, 0.5, 0.2))
        box.parmTuple("t").set((2.4, 0, 1.7))
        bind = self.obj.createNode("attribwrangle", "mesh_binding")
        bind.setInput(0, box)
        bind.setParms({"class": 1, "snippet": 's@shop_materialpath="' + self.sentinel.path() + '";'})
        self.output = self.obj.createNode("merge", "OUT")
        self.output.setInput(0, self.control)
        self.output.setInput(1, bind)
        self.output.setDisplayFlag(True)
        self.output.setRenderFlag(True)
        self.obj.parm("shop_materialpath").set(self.absorb.path())
        self.camera = hou.node("/obj").createNode("cam", "tapered_camera")
        self.camera.setParms(dict(tz=5, projection=1, orthowidth=6))
        self.rop = hou.node("/out").createNode("cycles_ipr", "tapered_volume")
        # The default this test was written for (before the ROP took Blender's defaults).
        self.rop.setParms(dict(pixel_filter="box", filter_width=1.0))
        requested = json.loads(device) if isinstance(device, str) and device.startswith("[") else device
        self.target_ids = list(set_devices(self.rop, requested))
        self.rop.setParms(
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=96,
                resy=64,
                samples=samples,
                ipr_samples=samples,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
                use_material_assignments=1,
                volume_step_rate=0.25,
            )
        )
        self.rop.parmTuple("background").set((1, 1, 1))
        self.heavy = None
        if heavy:
            self.heavy = hou.node("/obj").createNode("geo", "retained_tapered_instances", run_init_scripts=False)
            self.prototype = self.heavy.createNode("torus", "million_triangle_prototype")
            self.prototype.setParms(dict(type=0, rows=512, cols=1024))
            grid = self.heavy.createNode("grid", "twelve_targets")
            grid.setParms(dict(type=0, rows=3, cols=4))
            grid.parmTuple("size").set((8, 6))
            grid.parmTuple("t").set((0, 0, 20))
            self.copies = self.heavy.createNode("copytopoints", "shared_prototype")
            self.copies.setInput(0, self.prototype)
            self.copies.setInput(1, grid)
            self.copies.parm("pack").set(1)
            self.copies.setDisplayFlag(True)
            self.copies.setRenderFlag(True)
            self.heavy.parm("shop_materialpath").set(self.sentinel.path())
            self.rop.parm("vobject").set(self.obj.path() + " " + self.heavy.path())
        self.entry = None
        self.navigation = False

    def materials(self):
        return tuple(
            (node.path(), tuple((p.name(), p.rawValue()) for p in node.parms()))
            for builder in (self.absorb, self.integer, self.emission, self.sentinel)
            for node in (builder, *builder.allSubChildren())
        )

    def set(self, **values):
        self.navigation = False
        if "border" in values:
            for name in ("source_float", "source_integer", "source_vector_components"):
                self.obj.node(name).parm("volborder").set(values["border"])
        self.control.setParms(values)

    def move(self, node, value):
        self.navigation = True
        node.parm("tx").set(value)

    def steps(self):
        return [
            ("Positive anisotropic native frustum", lambda: None),
            ("Compressed positive taper keeps its narrow-end sampling", lambda: self.set(taperx=0.5, tapery=1.8)),
            ("Legal collapsed cone apex remains visible", lambda: self.set(taperx=0, tapery=0)),
            ("Signed hourglass clips its two finite sections", lambda: self.set(taperx=-0.5, tapery=1.8)),
            ("Reflected and rotated oriented lattice", lambda: self.set(mirror=1, angle=37)),
            ("Restore positive lattice", lambda: self.set(taperx=2, tapery=1.5, mirror=0, angle=0)),
            ("Affine fields retain their original fast path", lambda: self.set(taperx=1, tapery=1)),
            ("Nonlinear fields restored after affine edit", lambda: self.set(taperx=2, tapery=1.5)),
            ("Voxel values update in the same worker", lambda: self.set(level=0.6)),
            ("Voxel value repair", lambda: self.set(level=0.35)),
            ("Stock constant-zero border retains its edge interpolation", lambda: self.set(border=0)),
            ("Streak sampling still clips finite frustum support", lambda: self.set(border=2)),
            ("Unusable auxiliary field retains independent density", lambda: self.set(mode=5)),
            ("Cached auxiliary warning survives camera navigation", lambda: self.move(self.camera, 0.04)),
            ("Camera return does not recook fields", lambda: self.move(self.camera, 0)),
            ("Auxiliary resolution repair", lambda: self.set(mode=0)),
            ("Density over budget is omitted without invented mass", lambda: self.set(mode=4)),
            ("Object navigation retains unavailable-field warning", lambda: self.move(self.obj, 0.06)),
            ("Object return retains volume conversion cache", lambda: self.move(self.obj, 0)),
            ("Density resolution repair restores the volume", lambda: self.set(mode=0)),
            ("Singular density map remains local", lambda: self.set(mode=3)),
            ("Density transform repair", lambda: self.set(mode=0)),
            ("Nonfinite source voxel remains local", lambda: self.set(mode=6)),
            ("Voxel repair clears its warning", lambda: self.set(mode=0)),
            ("Nonfinite taper remains local", lambda: self.set(mode=2)),
            ("Taper repair clears its warning", lambda: self.set(mode=0)),
            ("Vector storage in the density slot remains local", lambda: self.set(mode=7)),
            ("Scalar density type repair", lambda: self.set(mode=0)),
            ("Native integer density uses SDK interpolation", lambda: self.set(mode=1)),
            ("Float density restored", lambda: self.set(mode=0)),
            ("Native Vec3 voxel interpolation drives emission", lambda: self.set(shading=1)),
            ("Broken contributing vector retains valid density", lambda: self.set(mode=8)),
            ("Vector map repair restores emission", lambda: self.set(mode=0)),
            ("Missing contributing vector uses diagnostic volume", lambda: self.set(mode=9)),
            ("Missing field repair restores emission", lambda: self.set(mode=0)),
            ("First usable later density preserves its own material", lambda: self.set(mode=10, shading=0)),
            ("Density assignment repair", lambda: self.set(mode=0)),
            ("Valid empty density does not invent scattering", lambda: self.set(level=0)),
            ("Empty density repair restores extinction", lambda: self.set(level=0.35)),
        ]

    def configuration(self):
        return {
            key: self.control.evalParm(key)
            for key in ("mode", "taperx", "tapery", "level", "angle", "mirror", "shading")
        }

    def expected_codes(self):
        c = self.configuration()
        mode = c["mode"]
        codes = set()
        if c["taperx"] != 1 or c["tapery"] != 1:
            codes.add("volume.resampled")
        if mode in (2, 3, 6, 7, 8):
            codes.add("volume.field_ignored")
        if mode in (4, 5, 10):
            codes.add("volume.resample_budget")
        if mode in (2, 3, 4, 6, 7):
            codes.add("volume.density_unavailable")
        if c["shading"] and mode in (8, 9):
            codes.add("volume.shader_field_unavailable")
        if mode == 10:
            codes.add("volume.conflicting_materials")
        return codes

    def source_reference(self):
        c = self.configuration()
        mode = c["mode"]
        items = descriptors(self.control, self.obj)
        # Source failures intentionally do not serialize their NaN payloads.
        valid = [
            item
            for item in items
            if np.isfinite(item["matrix"]).all()
            and np.isfinite(item["taper"]).all()
            and abs(np.linalg.det(np.asarray(item["matrix"]).reshape(4, 4)[:3, :3])) > 1e-15
            and reference.target_plan(item["resolution"], item["taper"])[2]
        ]
        preferred = "density" if mode in (1, 10) else "integer_auxiliary"
        field = next((item for item in valid if item["name"] == preferred), valid[0])
        assert reference.assert_centres(field) < 3e-6
        return field

    @staticmethod
    def pixels(frame):
        p = frame["planes"]["combined"]
        return (
            np.frombuffer(p["pixels"], np.float32)
            .reshape(frame["height"], frame["width"], p["channels"])[:, :, :3]
            .copy()
        )

    def assert_pixels(self, frame):
        c = self.configuration()
        mode = c["mode"]
        pixels = self.pixels(frame)
        assert np.isfinite(pixels).all()
        shift = self.obj.evalParm("tx") - self.camera.evalParm("tx")
        col = int(((2.4 + shift) / 6 + 0.5) * 96)
        np.testing.assert_allclose(
            pixels[31:33, col - 1 : col + 1], np.broadcast_to(SENTINEL, (2, 2, 3)), atol=3e-4, rtol=0
        )
        field = self.source_reference()
        if c["shading"] and mode in (8, 9):
            measured = pixels[30:34, 46:50].mean((0, 1))
            assert measured[0] > measured[1] + 0.03 and measured[2] > measured[1] + 0.03, measured
            return dict(diagnostic_rgb=measured.tolist(), independent_mesh=SENTINEL)
        expected = reference.image(
            field,
            camera_x=self.camera.evalParm("tx"),
            level=c["level"],
            integer=mode in (1, 10),
            emission=bool(c["shading"]) and mode not in (1, 10),
            absent=mode in (2, 3, 4, 6, 7),
        )
        # Compare finite support over a broad image region; the unaffected mesh
        # is deliberately outside it. Boundary filtering is a finite-grid
        # approximation, so include both a whole-region and interior witness.
        diff = np.abs(pixels[8:56, 8:78] - expected[8:56, 8:78])
        centre = float(np.abs(pixels[29:35, 45:51] - expected[29:35, 45:51]).mean())
        mean = float(diff.mean())
        assert mean < 0.012 and centre < 0.015, (c, mean, centre, pixels[32, 48].tolist(), expected[32, 48].tolist())
        return dict(
            mean_continuous_error=mean,
            interior_continuous_error=centre,
            observed=pixels[32, 48].tolist(),
            continuous=expected[32, 48].tolist(),
            source=field,
        )

    def assert_status(self, status, *, heavy=False):
        assert status["state"] != "failed" and not status["error"] and not status["motion_warnings"], status
        assert status["render_device_ids"] == self.target_ids, status
        issues = status["source_diagnostics"]
        assert {d["code"] for d in issues} == self.expected_codes(), issues
        for issue in issues:
            assert issue["source"] and issue["message"] and issue["action"], issue
            assert issue.get("severity", "warning") == ("info" if issue["code"] == "volume.resampled" else "warning"), (
                issue
            )
            if issue["code"] == "volume.resample_budget":
                assert "67108864" in issue["message"] and "no coarser" in issue["message"], issue
        available = self.configuration()["mode"] not in (2, 3, 4, 6, 7)
        assert status["geometry_resources"] == 1 + int(available) + int(heavy), status
        assert status["object_resources"] == 1 + int(available) + (12 if heavy else 0), status
        assert status["points"] == status["curves"] == 0, status

    def session(self):
        self.entry = registry.for_rop(self.rop, native_scene.configuration(self.rop))
        return self.entry

    def update(self):
        return self.entry.session.wait(self.entry.apply(native_scene.capture(self.rop), hou.time()))
