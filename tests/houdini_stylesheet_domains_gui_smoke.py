# SPDX-License-Identifier: Apache-2.0
"""One live viewer: independent shading/height edits, repairs and heavy sharing."""

import ast, json, time
import hou, numpy as np
import cycles_houdini as ch
from light_shader_fixture import write_image
from houdini_material_stylesheet_gui_smoke import Workflow as Base, sheet


class Workflow(Base):
    def __init__(self):
        super().__init__()
        f = self.f
        shader = self.mat.node("surface_shader")
        shader.parm("strength").deleteAllKeyframes()
        shader.parm("strength").set(1)
        value = self.mat.createNode("cycles::value::1", "shared_gain")
        value.parm("value").set(1)
        ch.promote_parameter(self.mat, value, "value", name="gain")
        shader.setNamedInput("strength", value, "value")
        scale = self.mat.createNode("cycles::math::1", "height_scale")
        scale.setParms(dict(math_type="multiply", value2=0.1))
        scale.setNamedInput("value1", value, "value")
        disp = self.mat.createNode("cycles::displacement::1", "scoped_height")
        disp.setParms(dict(scale=1, midlevel=0, space="object"))
        disp.setNamedInput("height", scale, "value")
        self.mat.node("surface_output").setNamedInput("displacement", disp, "displacement")
        self.mat.parm("cycles_displacement_method").set("true")
        self.dest = f.dest.parent / "stylesheet-domains-live"
        self.dest.mkdir(exist_ok=True)
        self.file = self.dest / "surface-repair.exr"
        if self.file.exists():
            self.file.unlink()
        image = self.mat.createNode("cycles::image_texture::1", "surface_image")
        image.setParms(dict(colorspace="data", interpolation="closest"))
        image.parm("filename").set(self.file.as_posix())
        ch.promote_parameter(self.mat, image, "filename", name="image_file")
        coordinates = self.mat.createNode("cycles::texture_coordinate::1", "image_coordinates")
        image.setNamedInput("vector", coordinates, "generated")
        self.shader = shader
        self.image = image
        self.expected_z = {}
        self.retained = set()
        steps = []

        def add(label, action, power=1, z=-0.1, warning=False, retain=False):
            color = tuple(np.array((0.2, 0.4, 0.6)) * power) if np.isscalar(power) else power
            steps.append((label, action, color, warning))
            self.expected_z[label] = z
            if retain:
                self.retained.add(label)

        add("Authored shared value", lambda: None)
        add("Surface category edit", lambda: self.apply(surface={"gain": 2}), 2, retain=True)
        add("Second surface value", lambda: self.apply(surface={"gain": 3}), 3, retain=True)
        add("Height category edit", lambda: self.apply(displacement={"gain": 3}), 1, -0.3)
        add(
            "Independent surface and height",
            lambda: self.apply(surface={"gain": 2}, displacement={"gain": 3}),
            2,
            -0.3,
            retain=True,
        )
        add("Generic value on both outputs", lambda: self.apply(generic={"gain": 4}), 4, -0.4)
        add(
            "Bad surface keeps generic and height",
            lambda: self.apply(generic={"gain": 4}, surface={"gain": "bad"}, displacement={"gain": 3}),
            4,
            -0.3,
            True,
        )
        add(
            "Repair surface keeps height",
            lambda: self.apply(generic={"gain": 4}, surface={"gain": 2}, displacement={"gain": 3}),
            2,
            -0.3,
            retain=True,
        )
        add(
            "Bad height keeps generic and surface",
            lambda: self.apply(generic={"gain": 4}, surface={"gain": 2}, displacement={"gain": "bad"}),
            2,
            -0.4,
            True,
        )
        add("Repair height", lambda: self.apply(surface={"gain": 2}, displacement={"gain": 3}), 2, -0.3)
        add(
            "Scoped CVEX values",
            lambda: self.apply(surface={"gain": self.script(3)}, displacement={"gain": self.script(2)}),
            3,
            -0.2,
        )
        add(
            "Broken surface CVEX",
            lambda: self.apply(surface={"gain": self.script(None)}, displacement={"gain": self.script(2)}),
            1,
            -0.2,
            True,
            True,
        )
        add(
            "Repair surface CVEX",
            lambda: self.apply(surface={"gain": self.script(3)}, displacement={"gain": self.script(2)}),
            3,
            -0.2,
            retain=True,
        )
        add("Heavy scoped surface value", lambda: self.heavy_rule(0.3), 3, -0.2, retain=True)
        add("Heavy scoped surface edit", lambda: self.heavy_rule(0.6), 3, -0.2, retain=True)
        add("Broken heavy scoped field", lambda: self.heavy_rule("bad"), 3, -0.2, True, True)
        add("Repair heavy scoped field", lambda: self.heavy_style.set(""), 3, -0.2, retain=True)
        add("Missing surface image preserves height", self.enable_image, (1, 0, 1), -0.3, True)
        add(
            "Repair surface image without parameter edit",
            lambda: write_image(self.file, (0.2, 0.5, 0.7)),
            (0.2, 0.5, 0.7),
            -0.3,
            retain=True,
        )
        add("Delete surface image keeps height", lambda: self.file.unlink(), (1, 0, 1), -0.3, True, True)
        add(
            "Recreate surface image",
            lambda: write_image(self.file, (0.7, 0.2, 0.4)),
            (0.7, 0.2, 0.4),
            -0.3,
            retain=True,
        )
        add("Return to shared control", self.restore, 1, -0.1)
        add("Camera navigation", lambda: f.camera.parm("tx").set(0.3), retain=True)
        add("Packed object transform", lambda: f.heavy.parm("tx").set(0.5), retain=True)
        self.steps = steps
        self.report["scope"] = (
            "Independent surface/displace values, CVEX/type/image repairs, stable displaced positions, one worker/viewer and twelve shared heavy instances"
        )

    @staticmethod
    def script(value):
        return {
            "script": {
                "code": "cvex broken export float result=0){}"
                if value is None
                else "cvex gain(export float result=0){result=" + str(value) + ";}",
                "return": "result",
            }
        }

    def apply(self, generic=None, surface=None, displacement=None):
        self.control.set(
            sheet(
                {
                    key: value
                    for key, value in (
                        ("materialParameters", generic),
                        ("surfaceParameters", surface),
                        ("displaceParameters", displacement),
                    )
                    if value is not None
                }
            )
        )

    def heavy_rule(self, value):
        self.heavy_style.set(sheet({"surfaceParameters": {"roughness": value}}, dict(type="self")))

    def enable_image(self):
        self.shader.setNamedInput("color", self.image, "color")
        self.shader.setInput(self.shader.inputIndex("strength"), None)
        self.shader.parm("strength").set(1)
        self.apply(surface={"image_file": self.file.as_posix()}, displacement={"gain": 3})

    def restore(self):
        self.shader.setInput(self.shader.inputIndex("color"), None)
        self.shader.parmTuple("color").set((0.2, 0.4, 0.6))
        self.shader.setNamedInput("strength", self.mat.node("shared_gain"), "value")
        self.control.set("")

    def tick(self):
        super().tick()
        if self.phase == "wait" and self.entry and time.monotonic() - getattr(self, "last_observation", 0) > 1:
            self.last_observation = time.monotonic()
            frame = self.entry.session.frame()
            self.report["waiting"] = dict(
                index=self.index,
                status=self.entry.session.status(),
                pixel=self.f.pixels(frame)[32, 32, :3].tolist() if frame else None,
            )
            Base.save(self)

    def save(self):
        if self.report["events"] and self.phase != "closing":
            event = self.report["events"][-1]
            label = event["label"]
            status = event["status"]
            np.testing.assert_allclose(
                self.f.pixels(self.entry.session.frame(), "position")[32, 32, 2], self.expected_z[label], atol=3e-5
            )
            if len(self.report["events"]) > 1 and label in self.retained:
                assert (
                    status["displacement_rebuilds"] == self.report["events"][-2]["status"]["displacement_rebuilds"]
                ), label
            if label in ("Heavy scoped surface value", "Heavy scoped surface edit", "Broken heavy scoped field"):
                refs = [
                    r
                    for r in self.entry.session.native_material_references()
                    if r["path"] == self.heavy_material.path()
                ]
                assert refs
                expected = (
                    0.3
                    if label == "Heavy scoped surface value"
                    else 0.6
                    if label == "Heavy scoped surface edit"
                    else "bad"
                )
                for ref in refs:
                    fields = {k: v for text in ref["surface_overrides"] for k, v in ast.literal_eval(text).items()}
                    assert fields["roughness"] == expected, fields
        super().save()


if __name__ == "__main__":
    workflow = Workflow()
