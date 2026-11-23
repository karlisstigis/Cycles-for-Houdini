# SPDX-License-Identifier: Apache-2.0
"""Nonlinear, independent shutter references for packed instance fields."""

import pass_fixtures as pass_fixture

import json
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import camera_properties, device_selection, native_scene
from packed_instance_attribute_fixture import Fixture as PackedFixture


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def attribute_data_id(attribute):
    """Exact HOM pickle state; data IDs are comparable only in this process."""
    value = attribute.dataId()
    assert value.isValid()
    state = tuple(int(part) for part in value.__getstate__())
    assert len(state) == 2
    return state


class Fixture(PackedFixture):
    width, height = 160, 100
    samples = 512

    def __init__(self, device="CPU"):
        hou.setFps(24)
        hou.setFrame(1)
        self.devices = json.loads(device) if isinstance(device, str) and device.startswith("[") else device
        first = self.devices[0] if isinstance(self.devices, list) else self.devices
        super().__init__(first)
        device_selection.set_devices(self.rop, self.devices if isinstance(self.devices, list) else [self.devices])
        self.dest = self.dest.parent / "sampled-instance-fields"
        self.dest.mkdir(parents=True, exist_ok=True)
        pass_fixture.parm(self.camera, "shutter").set(0.5)
        pass_fixture.set_parameters(
            self.rop,
            dict(
                allowmotionblur=1,
                geometry_motion="transform",
                motion_samples=3,
                samples=self.samples,
                ipr_samples=self.samples,
                pass_position=1,
                pass_depth=1,
                pass_normal=1,
                transparent_background=1,
            ),
        )
        self.scalar_scale = self.material.createNode("cycles::math::1", "sampled_scalar_scale")
        pass_fixture.parm(self.scalar_scale, "math_type").set("multiply")
        self.scalar_scale.setNamedInput("value1", self.rand, "fac")
        self.square = self.material.createNode("cycles::math::1", "nonlinear_scalar_reference")
        pass_fixture.parm(self.square, "math_type").set("multiply")
        self.square.setNamedInput("value1", self.scalar_scale, "value")
        self.square.setNamedInput("value2", self.scalar_scale, "value")
        self.vector_square = self.material.createNode("cycles::vector_math::1", "nonlinear_vector_reference")
        pass_fixture.parm(self.vector_square, "math_type").set("multiply")
        self.color_attribute = self.material.createNode("cycles::vertex_color::1", "sampled_color_attribute")
        pass_fixture.parm(self.color_attribute, "layer_name").set("Cd")
        for name, default in (("temporal_amplitude", 1.0), ("temporal_fault", 0)):
            template = (hou.FloatParmTemplate if isinstance(default, float) else hou.IntParmTemplate)(
                name, name, 1, default_value=(default,)
            )
            self.fields.addSpareParmTuple(template)
        self.configure()
        self.nominal_control = self.obj.createNode("python", "verified_nominal_ids")
        self.nominal_control.setInput(0, self.output)
        pass_fixture.parm(self.nominal_control, "python").set("""import hou
node=hou.pwd()
node.setManagesAttribDataIds(True)
source=node.inputs()[0].geometry()
if abs(hou.frame()-round(hou.frame()))<1e-5:
    cached=node.cachedUserData('requested_frame_geometry')
    if cached is None:
        cached=source.freeze(read_only=True,clone_data_ids=True)
        node.setCachedUserData('requested_frame_geometry',cached)
    node.geometry().copy(cached,clone_data_ids=True)
else:
    node.geometry().copy(source,clone_data_ids=True)
""")
        self.nominal_control.bypass(True)
        self.output.setRenderFlag(False)
        self.output.setDisplayFlag(False)
        self.nominal_control.setRenderFlag(True)
        self.nominal_control.setDisplayFlag(True)

    def stable_nominal(self, enabled):
        """A controlled SOP retains exact nominal data IDs, not just values."""
        self.nominal_control.destroyCachedUserData("requested_frame_geometry", must_exist=False)
        self.nominal_control.bypass(not enabled)

    def nominal_ids(self):
        source = self.nominal_control.geometry()
        attribute = source.findPrimAttrib(self.field)
        assert attribute is not None
        return dict(
            data_id=attribute_data_id(attribute),
            size=attribute.size(),
            values=[primitive.attribValue(attribute) for primitive in source.prims()],
        )

    def _code(self, owner, packed, field, fault=0, duplicates=False, ancestor=False):
        index = dict(primitive="@primnum", point="@ptnum", vertex="@vtxnum", detail="0")[owner]
        divisor = 1 if packed or owner in ("primitive", "detail") else 4
        head = "int k=int(" + index + ")/" + str(divisor) + ';float t=@Frame-1;float a=chf("temporal_amplitude");'
        values = dict(
            rand="f@rand=.4+.08*k+1.6*a*t;",
            counter="i@counter=int(floor(4+k+16*a*t+.5));",
            vector_field="v@vector_field=set(.3+.02*k+.8*a*t,.5-.6*a*t,.2+.4*a*t);",
            Cd="v@Cd=set(.2+.03*k+.4*a*t,.4+.6*a*t,.6-.8*a*t);",
        )
        if ancestor:
            values = dict(
                rand="f@rand=.65+1.2*a*t;",
                counter="i@counter=int(floor(6+12*a*t+.5));",
                vector_field="v@vector_field=set(.65+.4*a*t,.25-.8*a*t,.35+.2*a*t);",
                Cd="v@Cd=set(.6+.2*a*t,.3+.4*a*t,.4-.6*a*t);",
            )
        if fault in (2, 3, 4):
            values.pop(field)
        if fault == 3:
            values[field] = (
                "f@Cd=.4;"
                if field == "Cd"
                else "f@vector_field=.4;"
                if field == "vector_field"
                else "v@" + field + "={.2,.4,.6};"
            )
        code = head + "".join(values.values())
        if fault == 1:
            target = "v@" + field + ".y" if field in ("vector_field", "Cd") else "f@" + field
            # Counter uses a wrong floating storage value only off-frame; its
            # numeric scalar still converts to the existing shader precision.
            if field == "counter":
                code = head + "".join(value for name, value in values.items() if name != "counter")
                code += "f@counter=floor(4+k+16*a*t+.5);"
            code += "if(k==1)" + target + '=atof("nan");'
        if duplicates:
            code += 'i@id=7;s@name="ordinary_duplicate_identity";'
        if fault == 5:
            assert owner == "primitive" and packed
            code += "if(k==3)removeprim(0,@primnum,1);"
        return code

    def configure(
        self,
        field="rand",
        owner="primitive",
        *,
        fault=0,
        amplitude=1.0,
        nested=False,
        ancestor=False,
        packed=True,
        kind=0,
        duplicates=False,
        color_attribute=False,
        velocity=False,
    ):
        self.field, self.owner, self.fault = field, owner, fault
        assert packed or kind == 0, "Expanded reference uses four grid points/corners per mesh copy"
        if fault == 6:
            assert field == "rand" and owner == "primitive" and packed and kind == 0 and not (nested or ancestor)
        self.amplitude, self.ancestor, self.packed, self.kind = amplitude, ancestor, packed, kind
        # Reassigning an unchanged Houdini structural parameter still dirties
        # its SOP and repacks fresh prototype owners. A field edit must change
        # only that field, as in the artist's actual workflow.
        for parameter, value in (
            (pass_fixture.parm(self.copies, "pack"), int(packed)),
            (pass_fixture.parm(self.shape, "input"), kind),
            (pass_fixture.parm(self.output, "input"), int(nested or ancestor)),
            (pass_fixture.parm(self.fields, "class"), pass_fixture.parm(self.fields, "class").menuItems().index(owner)),
        ):
            if parameter.eval() != value:
                parameter.set(value)
        pass_fixture.set_parameters(self.fields, dict(temporal_amplitude=amplitude, temporal_fault=fault))
        good = self._code(owner, packed, field, 4 if ancestor else 0, duplicates)
        bad = self._code(owner, packed, field, 1 if fault == 6 else fault or 1, duplicates)
        if fault == 6:
            good += 'if(k==0)f@rand=atof("nan");'
            bad += 'if(k==0)f@rand=atof("nan");'
        if velocity:
            assert owner in ("primitive", "point") and packed and not (nested or ancestor)
            authored = (
                "v@v={.8,0,0};"
                if owner == "point"
                else 'setpointattrib(0,"v",primpoint(0,@primnum,0),set(.8,0,0),"set");'
            )
            good += authored
            bad += authored
        # Off-frame failures leave requested-frame tuples unchanged. Case 6
        # deliberately combines one nominal-invalid and one shutter-only field.
        expression = (
            "import hou\nn=hou.pwd()\nf=hou.frame()\n"
            "off=abs(f-round(f))>1e-5\n"
            'broken=n.evalParm("temporal_fault") and off\n'
            "return " + repr(bad) + " if broken else " + repr(good)
        )
        pass_fixture.parm(self.fields, "snippet").setExpression(expression, hou.exprLanguage.Python)
        pass_fixture.parm(self.outer, "snippet").deleteAllKeyframes()
        outer = self._code("primitive", True, field, ancestor=True).replace('chf("temporal_amplitude")', str(amplitude))
        pass_fixture.parm(self.outer, "snippet").set(outer)
        pass_fixture.parm(self.rand, "attribute").set(field)
        source = self.color_attribute if color_attribute else self.rand
        output = "color" if color_attribute else "vector"
        self.vector_square.setNamedInput("vector1", source, output)
        self.vector_square.setNamedInput("vector2", source, output)
        pass_fixture.parm(self.scalar_scale, "value2").set(0.1 if field == "counter" else 1.0)
        self.emission.setNamedInput(
            "color",
            self.vector_square if field in ("vector_field", "Cd") else self.square,
            "vector" if field in ("vector_field", "Cd") else "value",
        )

    def temporal_values(self, frame, k):
        t = np.asarray(frame, np.float64) - 1.0
        a = self.amplitude
        if self.owner == "detail":
            k = 0
        if self.ancestor:
            if self.field == "rand":
                value = 0.65 + 1.2 * a * t
            elif self.field == "counter":
                value = np.floor(6 + 12 * a * t + 0.5) * 0.1
            elif self.field == "vector_field":
                return np.stack((0.65 + 0.4 * a * t, 0.25 - 0.8 * a * t, 0.35 + 0.2 * a * t), axis=-1)
            else:
                return np.stack((0.6 + 0.2 * a * t, 0.3 + 0.4 * a * t, 0.4 - 0.6 * a * t), axis=-1)
        elif self.field == "rand":
            value = 0.4 + 0.08 * k + 1.6 * a * t
        elif self.field == "counter":
            value = np.floor(4 + k + 16 * a * t + 0.5) * 0.1
        elif self.field == "vector_field":
            return np.stack((0.3 + 0.02 * k + 0.8 * a * t, 0.5 - 0.6 * a * t, 0.2 + 0.4 * a * t), axis=-1)
        else:
            return np.stack((0.2 + 0.03 * k + 0.4 * a * t, 0.4 + 0.6 * a * t, 0.6 - 0.8 * a * t), axis=-1)
        return np.repeat(np.asarray(value)[..., None], 3, axis=-1)

    def expected(self, k, *, held=None):
        if self.fault == 6 and k == 0:
            return np.asarray((1.0, 0.0, 1.0))
        settings = native_scene.motion_configuration(self.rop, hou.time())
        nominal = self.temporal_values(hou.frame(), k)
        if held is None:
            held = bool(self.fault and (self.fault not in (1, 6) or k == 1))
        object_blur = pass_fixture.parm(self.obj, "cycles_motion_blur")
        if not settings["enabled"] or (object_blur and object_blur.eval() == "off") or self.velocity_mode():
            held = True
        if held:
            return nominal * nominal
        times = np.asarray(native_scene.motion_times(settings, hou.time())) * hou.fps() + 1
        values = self.temporal_values(times, k)
        # Exact box-shutter integral of the square of each linearly interpolated
        # sampled tuple; a linear emission average would conceal centre holding.
        return ((values[:-1] ** 2 + values[:-1] * values[1:] + values[1:] ** 2) / 3).mean(axis=0)

    def shutter_reference(self):
        settings = native_scene.motion_configuration(self.rop, hou.time())
        sampling = camera_properties.sampling_configuration(self.rop, hou.time(), settings)
        assert (
            not sampling["rolling_shutter"] and not sampling["shutter_curve"] and not sampling["source_diagnostics"]
        ), sampling
        authored = pass_fixture.parm(self.camera, "cycles_use_shutter_curve")
        if authored and authored.eval():
            values = pass_fixture.parm(self.camera, "cycles_shutter_curve").eval().values()
            assert values and values[0] > 0 and all(value == values[0] for value in values), values
        return dict(
            profile="Ideal uniform box shutter; independent squared linear-segment integral",
            authored_uniform_curve=bool(authored and authored.eval()),
            enabled=settings["enabled"],
            vector_only=settings["vectors"],
            shutter_frames=settings["shutter_frames"],
            shutter_offset=settings["offset"],
            sample_frames=[time * hou.fps() + 1 for time in native_scene.motion_times(settings, hou.time())],
        )

    def inspect(self, frame, *, tolerance=0.003):
        # Alpha must measure actual coverage, including point/hair silhouettes;
        # an opaque background would admit partially covered black pixels.
        assert pass_fixture.evaluate(self.rop, "transparent_background") == 1
        beauty, position = plane(frame, "combined"), plane(frame, "position")
        assert np.isfinite(beauty).all() and np.isfinite(position).all()
        delta = np.asarray(self.obj.worldTransform().extractTranslates())
        rows = []
        self.last_inspection = rows
        for k, point in enumerate(self.targets.geometry().points()):
            center = np.asarray(point.position()) + delta
            mask = (
                (np.abs(position[:, :, 0] - center[0]) < 0.24)
                & (np.abs(position[:, :, 1] - center[1]) < 0.18)
                & (beauty[:, :, 3] > 0.999)
            )
            assert mask.sum() > 5, (k, mask.sum())
            actual = beauty[:, :, :3][mask].mean(axis=0)
            expected = self.expected(k)
            error = float(np.max(np.abs(actual - expected)))
            limit = max(tolerance, float(np.sqrt(np.max(np.abs(expected)))) * 0.002)
            rows.append(
                dict(
                    copy=k,
                    pixels=int(mask.sum()),
                    minimum_alpha=float(beauty[:, :, 3][mask].min()),
                    actual=actual.tolist(),
                    expected=expected.tolist(),
                    error=error,
                    tolerance=limit,
                )
            )
            assert error < limit, rows[-1]
        return rows

    def update(self):
        value = native_scene.capture(self.rop)
        value["passes"] = ["combined", "position", "depth", "normal"]
        return value

    def capture_info(self, session):
        acknowledged = session._controller.acknowledged
        assert acknowledged is not None
        return ch._native.snapshot_info(acknowledged)

    def prototype_ownership(self):
        if not self.packed:
            return None
        geometry = self.copies.geometry()
        rows, identities = [], []
        for primitive in geometry.prims():
            assert isinstance(primitive, hou.PackedGeometry)
            identity = primitive.intrinsicValue("geometryid")
            identities.append(identity)
            if any(row["geometry_id"] == identity for row in rows):
                continue
            source = primitive.getEmbeddedGeometry()
            rows.append(
                dict(
                    geometry_id=identity,
                    points=len(source.points()),
                    primitives=len(source.prims()),
                    positions=[list(point.position()) for point in source.points()],
                    P=attribute_data_id(source.findPointAttrib("P")),
                    topology=dict(
                        valid=source.topologyDataId().isValid(), state=source.topologyDataId().__getstate__()
                    ),
                    primitive_intrinsics=dict(
                        valid=source.primitiveIntrinsicsDataId().isValid(),
                        state=source.primitiveIntrinsicsDataId().__getstate__(),
                    ),
                )
            )
        return dict(
            copy_cooks=self.copies.cookCount(),
            prototype_cooks=self.prototype_fields.cookCount(),
            shape_cooks=self.shape.cookCount(),
            instance_geometry_ids=identities,
            prototypes=rows,
        )

    def velocity_mode(self):
        """Velocity holds instance fields at the requested frame and samples no shutter roots (task M1)."""
        override = pass_fixture.parm(self.obj, "cycles_geometry_motion")
        if override is not None and override.eval() != "inherit":
            return override.eval() == "velocity"
        return pass_fixture.evaluate(self.rop, "geometry_motion") == "velocity"

    def expected_incomplete(self):
        if self.velocity_mode():
            return 0
        components = 3 if self.kind == 3 else 1
        return (
            0
            if not self.fault or (self.fault == 1 and self.owner == "detail")
            else components * (1 if self.fault in (1, 6) else 4)
        )
