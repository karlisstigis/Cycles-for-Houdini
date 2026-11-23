# SPDX-License-Identifier: Apache-2.0
"""Copy-to-Points fields whose exact values feed discontinuous float hashes."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from pathlib import Path
import os
import hou, numpy as np
from packed_instance_attribute_fixture import Fixture as PackedFixture


class Fixture(PackedFixture):
    width, height = 384, 240

    def __init__(self, device="CPU"):
        super().__init__(device)
        self.dest = artifacts_root() / "stage3/materials/constant-attributes"
        self.dest.mkdir(parents=True, exist_ok=True)
        pass_fixture.parm(self.fields, "snippet").set("")
        pass_fixture.parm(self.copies, "targetattribs").set(1)
        pass_fixture.parm(self.copies, "applyattribs1").set("rand counter seed2 seed3")
        self.noise = self.material.createNode("cycles::white_noise_texture::1", "attribute_hash")
        pass_fixture.parm(self.noise, "dimensions").set("1D")
        self.scale.setNamedInput("value1", self.rand, "fac")
        pass_fixture.parm(self.scale, "value2").set(17.123)
        self.noise.setNamedInput("w", self.scale, "value")
        self.bump = self.material.createNode("cycles::bump::1", "hash_bump")
        # The default this test was written for (new nodes now take Blender's).
        self.bump.setParms(dict(distance=0.1))
        self.bump.setNamedInput("height", self.noise, "value")
        pass_fixture.set_parameters(
            self.rop, dict(samples=1, ipr_samples=1, pass_position=1, pass_normal=1, transparent_background=1)
        )
        self.configure()
        self.shade()

    def configure(self, offset=0, broken=False, animation=False):
        code = (
            "v@N={0,0,1};v@up={0,1,0};float k=@ptnum;f@rand=.1234567+.1732051*k+"
            + str(offset)
            + ";i@counter=@ptnum+7;u@seed2=set(f@rand,.7182818);v@seed3=set(f@rand,.7182818,.3141592);"
        )
        if animation:
            code += "f@rand+=.037*@Frame;"
        if broken:
            code += 'if(@ptnum==1)f@rand=atof("nan");'
        pass_fixture.parm(self.orientation, "snippet").set(code)

    def shade(self, mode="noise", field="rand"):
        self.mode = mode
        self.field = field
        pass_fixture.parm(self.rand, "attribute").set(field)
        vector = field in ("seed2", "seed3")
        pass_fixture.parm(self.noise, "dimensions").set("3D" if vector else "1D")
        if vector:
            self.noise.setNamedInput("vector", self.rand, "vector")
        source, socket = (
            (self.rand, "vector" if vector else "fac")
            if mode == "raw"
            else (self.scale, "value")
            if mode == "math"
            else (self.noise, "color" if vector else "value")
        )
        if mode == "bump":
            source, socket = self.bump, "normal"
        self.emission.setNamedInput("color", source, socket)

    def groups(self, frame, uniform=True, reference=None, warning=False):
        def plane(name):
            p = frame["planes"][name]
            return np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])

        image = plane("combined")
        position = plane("position")
        assert np.isfinite(image).all() and np.isfinite(position).all()
        values = []
        offset = self.obj.worldTransform().extractTranslates()
        for p in self.orientation.geometry().points():
            x, y, z = p.position() + offset
            mask = (image[:, :, 3] > 0.999) & (abs(position[:, :, 0] - x) < 0.17) & (abs(position[:, :, 1] - y) < 0.17)
            pixels = image[:, :, :3][mask]
            assert len(pixels) > 3, (p.number(), len(pixels))
            if uniform and not warning:
                assert np.all(pixels == pixels[0]), (self.mode, self.field, p.number(), pixels.min(0), pixels.max(0))
            if reference is not None and not warning:
                np.testing.assert_allclose(
                    pixels, np.broadcast_to(reference[p.number()], pixels.shape), rtol=0, atol=2e-6
                )
            if self.mode == "raw" and not warning:
                source = np.atleast_1d(p.attribValue(self.field))
                expected = np.repeat(source, 3) if len(source) == 1 else np.pad(source, (0, 3 - len(source)))
                np.testing.assert_allclose(pixels, np.broadcast_to(expected, pixels.shape), rtol=0, atol=2e-6)
            values.append(pixels.mean(0, dtype=np.float64).tolist())
        if warning:
            assert np.any(np.all(image[:, :, :3] == (1, 0, 1), axis=2))
        else:
            assert not np.any(np.all(image[:, :, :3] == (1, 0, 1), axis=2))
        return values

    def varying(self, frame):
        p = frame["planes"]["combined"]
        image = np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])
        p = frame["planes"]["position"]
        position = np.frombuffer(p["pixels"], np.float32).reshape(self.height, self.width, p["channels"])
        mask = image[:, :, 3] > 0.999
        expected = 0.4 + 0.07 * position[:, :, 0] + 0.11 * position[:, :, 1]
        np.testing.assert_allclose(image[:, :, 0][mask], expected[mask], rtol=0, atol=2e-6)
        assert np.ptp(image[:, :, 0][mask]) > 0.1
