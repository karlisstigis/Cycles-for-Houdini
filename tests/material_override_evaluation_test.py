# SPDX-License-Identifier: Apache-2.0
"""Pure owned-override contracts; no Houdini, DLL loading or renderer process."""

import argparse
import ast
from support.package_loader import load_module
import json
import posixpath
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPE = types.SimpleNamespace(**{key: key for key in ("Float", "Int", "String", "Toggle", "Menu", "Ramp", "Folder")})
NAMING = types.SimpleNamespace(RGBA="rgba", XYZW="xyzw", Base1="base1")
FOLDER = types.SimpleNamespace(MultiparmBlock="multiparm")


class Template:
    def __init__(
        self, name, kind=TYPE.Float, size=1, items=(), tags=None, children=(), naming=NAMING.Base1, ramp="float"
    ):
        self.identifier, self.kind, self.size, self.items, self._tags, self.children, self.naming, self.ramp = (
            name,
            kind,
            size,
            items,
            tags or {},
            children,
            naming,
            ramp,
        )

    def name(self):
        return self.identifier

    def type(self):
        return self.kind

    def numComponents(self):
        return self.size

    def menuItems(self):
        return self.items

    def tags(self):
        return self._tags

    def folderType(self):
        return FOLDER.MultiparmBlock

    def parmTemplates(self):
        return self.children

    def namingScheme(self):
        return self.naming

    def parmType(self):
        return self.ramp


class Ramp:
    def __init__(self, keys, values, color=False):
        self.k, self.v, self.color = keys, values, color

    def keys(self):
        return self.k

    def values(self):
        return self.v

    def isColor(self):
        return self.color


class Node:
    nodes = {}

    def __init__(self, path):
        self.identifier = path
        self.parameters = {}
        self.tuples = {}
        self.operator = "cycles::float_curve::1"
        Node.nodes[path] = self

    def path(self):
        return self.identifier

    def parms(self):
        return tuple(self.parameters.values())

    def type(self):
        return types.SimpleNamespace(name=lambda: self.operator)

    def parm(self, name):
        absolute = posixpath.normpath(name if name.startswith("/") else self.path() + "/" + name)
        owner = Node.nodes.get(posixpath.dirname(absolute))
        return owner.parameters.get(posixpath.basename(absolute)) if owner else None

    def parmTuple(self, name):
        return self.tuples.get(name)

    def add(self, name, value=0.0, kind=TYPE.Float, template=None, expression=None, language="hscript"):
        p = Parm(self, name, value, template or Template(name, kind), expression, language)
        self.parameters[name] = p
        self.tuples[name] = (p,)
        return p

    def vector(self, name, values, naming=NAMING.RGBA):
        template = Template(name, size=len(values), naming=naming)
        suffixes = "rgba" if naming == NAMING.RGBA else "xyzw"
        result = tuple(self.add(name + suffix, value, template=template) for suffix, value in zip(suffixes, values))
        self.tuples[name] = result
        return result


class Parm:
    def __init__(self, node, name, value, template, expression, language):
        self.owner, self.identifier, self.value, self.template, self.code, self.language = (
            node,
            name,
            value,
            template,
            expression,
            language,
        )
        self.consumers = []
        self.referenced = self
        self.evaluations = 0

    def node(self):
        return self.owner

    def name(self):
        return self.identifier

    def path(self):
        return self.owner.path() + "/" + self.identifier

    def parmTemplate(self):
        return self.template

    def parmsReferencingThis(self):
        return self.consumers

    def keyframes(self):
        return (types.SimpleNamespace(isExpressionSet=lambda: True),) if self.code is not None else ()

    def expression(self):
        return self.code

    def expressionLanguage(self):
        return self.language

    def evalAtTime(self, now):
        self.evaluations += 1
        return self.value(now) if callable(self.value) else self.value

    def evalAsRampAtFrame(self, frame):
        return self.value

    def evalAsIntAtFrame(self, frame):
        return len(self.value.keys()) if isinstance(self.value, Ramp) else int(self.evalAtTime((frame - 1) / 24))

    def getReferencedParm(self):
        return self.referenced

    def isTimeDependent(self):
        return callable(self.value)


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_hou = sys.modules.get("hou")
        hou = types.ModuleType("hou")
        hou.parmTemplateType = TYPE
        hou.parmNamingScheme = NAMING
        hou.folderType = FOLDER
        hou.exprLanguage = types.SimpleNamespace(Hscript="hscript", Python="python")
        hou.rampParmType = types.SimpleNamespace(Color="color", Float="float")
        hou.OperationFailed = type("OperationFailed", (Exception,), {})
        hou.Error = hou.OperationFailed
        hou.FolderParmTemplate = Template
        hou.MenuParmTemplate = lambda name, label, items: Template(name, TYPE.Menu, items=items)
        hou.FloatParmTemplate = lambda name, label, size, naming_scheme=NAMING.Base1: Template(
            name, size=size, naming=naming_scheme
        )
        hou.timeToFrame = lambda now: now * 24 + 1
        sys.modules["hou"] = hou
        package = types.ModuleType("_cycles_override_unit")
        package.__path__ = [str(ROOT / "houdini/python3.13libs/cycles_houdini")]
        sys.modules[package.__name__] = package
        cls.module = load_module(package, "material_overrides")

    @classmethod
    def tearDownClass(cls):
        if cls.previous_hou is None:
            sys.modules.pop("hou", None)
        else:
            sys.modules["hou"] = cls.previous_hou
        for name in tuple(sys.modules):
            if name.startswith("_cycles_override_unit"):
                sys.modules.pop(name, None)

    def setUp(self):
        Node.nodes = {}
        self.owner = Node("/mat/test")
        self.shader = Node("/mat/test/shader")
        self.gain = self.owner.add("gain", 2.0)
        self.offset = self.owner.add("offset", 3.0, TYPE.Int)
        self.displacement = self.owner.add("displacement", 0.125)

    def evaluate(self, *layers):
        return self.module.Evaluation(self.owner, layers)

    def recovery(self, evaluation, operation):
        for _ in range(520):
            try:
                return operation()
            except self.module.UnsupportedOverride as error:
                evaluation.omit(error)
        self.fail("Recovery did not converge")

    def test_partial_literal_retains_lower_and_independent_fields(self):
        e = self.evaluate("{'gain':4}", "{'gain':__import__('os').system('never'), 'displacement':.25}")
        self.assertEqual(e.parm(self.gain, 0), 4)
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertEqual(len(e.diagnostics), 1)

    def test_numeric_and_menu_validation_is_local(self):
        menu = self.owner.add("mode", 0, TYPE.Menu, Template("mode", TYPE.Menu, items=("a", "b")))
        e = self.evaluate("{'gain':1e309,'mode':4,'displacement':.5}")
        self.assertEqual(e.parm(self.gain, 0), 2)
        self.assertEqual(e.parm(menu, 0), 0)
        self.assertEqual(e.parm(self.displacement, 0), 0.5)
        self.assertEqual(len(e.diagnostics), 2)

    def test_rgba_color3_keeps_alpha_and_later_component_wins(self):
        channels = self.owner.vector("tint", (0.1, 0.2, 0.3, 0.4))
        e = self.evaluate("{'tint':(.8,.6,.2),'tintg':.7}")
        self.assertEqual(e.tuple(channels, 0), (0.8, 0.7, 0.2, 0.4))

    def test_rgba_vector4_replaces_alpha(self):
        channels = self.owner.vector("tint", (0.1, 0.2, 0.3, 0.4))
        e = self.evaluate("{'tint':(.8,.6,.2,.9)}")
        self.assertEqual(e.tuple(channels, 0), (0.8, 0.6, 0.2, 0.9))

    def test_unrelated_vector4_does_not_accept_three_values(self):
        channels = self.owner.vector("vector4", (1.0, 2.0, 3.0, 4.0), NAMING.XYZW)
        e = self.evaluate("{'vector4':(.8,.6,.2)}")
        self.assertEqual(e.tuple(channels, 0), (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(len(e.diagnostics), 1)

    def test_safe_compound_multiple_float_and_int_references(self):
        p = self.shader.add("strength", -999.0, expression='-(ch("../gain") * 2 - ch("../offset")) / 4')
        self.gain.consumers.append(p)
        self.offset.consumers.append(p)
        e = self.evaluate("{'gain':7,'offset':5}")
        self.assertEqual(e.parm(p, 0), -2.25)
        self.assertEqual(p.evaluations, 0)
        self.assertFalse(e.diagnostics)

    def test_chi_is_not_an_invented_hscript_function(self):
        p = self.shader.add("strength", 0.0, expression='chi("../offset")')
        self.offset.consumers.append(p)
        e = self.evaluate("{'offset':7,'displacement':.5}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 0)
        self.assertEqual(e.omitted, {"offset"})
        self.assertEqual(e.parm(self.displacement, 0), 0.5)

    def test_chained_compound_preserves_requested_time(self):
        animated = self.owner.add("animated", lambda t: 1 + t)
        middle = self.owner.add("middle", -999.0, expression='ch("gain") * ch("animated")')
        p = self.shader.add("strength", -999.0, expression='ch("../middle") + 1')
        self.gain.consumers.append(middle)
        middle.consumers.append(p)
        e = self.evaluate("{'gain':4}")
        self.assertEqual(e.parm(p, 0), 5)
        self.assertEqual(e.parm(p, 0.5), 7)
        self.assertEqual((middle.evaluations, p.evaluations), (0, 0))
        self.assertEqual(animated.evaluations, 2)
        self.assertIn(self.shader, e.animated_nodes)

    def test_python_contribution_omits_only_its_field(self):
        p = self.shader.add("strength", 6.0, expression='hou.parm("../gain").eval()*2', language="python")
        self.gain.consumers.append(p)
        e = self.evaluate("{'gain':9,'displacement':.25}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 6)
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertEqual(e.omitted, {"gain"})

    def test_unsupported_call_never_executes_and_recovers(self):
        p = self.shader.add("strength", 6.0, expression='__import__("os").system(ch("../gain"))')
        self.gain.consumers.append(p)
        e = self.evaluate("{'gain':9}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 6)
        self.assertEqual(len(e.diagnostics), 1)

    def test_unused_python_expression_is_quiet(self):
        p = self.shader.add("unused", 0.0, expression='__import__("os").system(ch("../gain"))', language="python")
        self.gain.consumers.append(p)
        e = self.evaluate("{'gain':9}")
        self.assertEqual(e.parm(self.displacement, 0), 0.125)
        self.assertEqual(p.evaluations, 0)
        self.assertFalse(e.diagnostics)

    def test_direct_string_reference(self):
        source = self.owner.add("image", "base.exr", TYPE.String)
        p = self.shader.add("filename", "bad", TYPE.String, expression='chs("../image")')
        source.consumers.append(p)
        e = self.evaluate("{'image':'override.exr'}")
        self.assertEqual(e.parm(p, 0), "override.exr")

    def test_zero_division_recovers_locally(self):
        p = self.shader.add("strength", 2.0, expression='1 / ch("../gain")')
        self.gain.consumers.append(p)
        e = self.evaluate("{'gain':0,'displacement':.25}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 2)
        self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_expression_recursion_and_nonfinite_result_are_bounded(self):
        p = self.shader.add("strength", 1.0, expression='ch("../gain") * 3e38')
        self.gain.consumers.append(p)
        e = self.evaluate("{'gain':3}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 1)
        self.assertEqual(e.omitted, {"gain"})

    def make_ramp(self, color=False):
        template = Template("palette", TYPE.Ramp, ramp="color" if color else "float")
        count = self.owner.add(
            "palette",
            Ramp((0.0, 1.0), ((0.1, 0.2, 0.3), (0.8, 0.6, 0.4)) if color else (0.2, 0.8), color),
            template=template,
        )
        self.shader.add("ramp", count.value, template=template).referenced = count
        for i, (position, value) in enumerate(zip(count.value.keys(), count.value.values()), 1):
            self.owner.add(f"palette{i}pos", position)
            self.owner.add(
                f"palette{i}interp", 1, TYPE.Menu, Template("mode", TYPE.Menu, items=tuple(str(k) for k in range(7)))
            )
            if color:
                self.owner.vector(f"palette{i}c", value)
            else:
                self.owner.add(f"palette{i}value", value)
        return count

    def test_native_float_new_stop_is_owned_without_mutation(self):
        self.make_ramp()
        before = tuple(self.owner.parms())
        e = self.evaluate("{'palette':3,'palette2pos':.5,'palette3pos':1,'palette3value':.4,'palette3interp':1}")
        controls = e.ramp(self.shader, 0)
        self.assertEqual(controls["keys"], [0.0, 0.5, 1.0])
        self.assertEqual(controls["values"], [0.2, 0.8, 0.4])
        self.assertEqual(tuple(self.owner.parms()), before)
        self.assertIsNone(self.owner.parm("palette3value"))

    def test_count_override_never_evaluates_discarded_broken_stop(self):
        count = self.make_ramp()
        broken = self.owner.parm("palette2value")

        def fail(now):
            raise AssertionError("Dormant stop evaluated")

        broken.value = fail

        def eager(frame):
            raise AssertionError("Whole ramp evaluated")

        count.evalAsRampAtFrame = eager
        e = self.evaluate("{'palette':1,'palette1value':.7}")
        self.assertEqual(e.ramp(self.shader, 0)["values"], [0.7])
        self.assertEqual(broken.evaluations, 0)

    def test_native_animated_count_is_attributed_without_whole_ramp_read(self):
        count = self.make_ramp()
        count.isTimeDependent = lambda: True

        def eager(frame):
            raise AssertionError("Whole ramp evaluated")

        count.evalAsRampAtFrame = eager
        e = self.evaluate("{'displacement':.5}")
        self.assertEqual(e.count(count, 0.5, self.shader), 2)
        self.assertIn(self.shader, e.animated_nodes)

    def test_native_color_new_tuple_and_components(self):
        self.make_ramp(True)
        e = self.evaluate(
            "{'palette':3,'palette2pos':.5,'palette3pos':1,'palette3c':(.4,.5,.6),'palette3cg':.9,'palette3interp':1}"
        )
        self.assertEqual(e.ramp(self.shader, 0)["values"][2], (0.4, 0.9, 0.6))

    def test_native_rgb_color_output_and_independent_alpha_ramp(self):
        # Color Ramp Alpha comes from its own promoted native float ramp (here
        # opaque), not from a fourth Color component.
        self.make_ramp(True)
        self.shader.operator = "cycles::color_ramp::1"
        self.shader.add("ramp_color_space", "RGB", TYPE.String)
        self.shader.add("ramp_interpolation", "LINEAR", TYPE.String)
        self.shader.add("ramp_hue_interpolation", "NEAR", TYPE.String)
        template = Template("palette_alpha", TYPE.Ramp, ramp="float")
        alpha = self.owner.add("palette_alpha", Ramp((0.0, 1.0), (1.0, 1.0)), template=template)
        self.shader.add("alpha_ramp", alpha.value, template=template).referenced = alpha
        for i, position in enumerate((0.0, 1.0), 1):
            self.owner.add(f"palette_alpha{i}pos", position)
            self.owner.add(f"palette_alpha{i}value", 1.0)
            self.owner.add(
                f"palette_alpha{i}interp",
                1,
                TYPE.Menu,
                Template("mode", TYPE.Menu, items=tuple(str(k) for k in range(7))),
            )
        e = self.evaluate(
            "{'palette':3,'palette2pos':.5,'palette3pos':1,'palette3c':(.4,.5,.6),'palette3interp':1,'palette_alpha2value':.25}"
        )
        ramps = __import__("_cycles_override_unit.ramps", fromlist=["node_controls"])
        self.assertEqual(ramps.node_controls(self.shader, 0, e)["values"][2], (0.4, 0.5, 0.6))
        source = ramps.lower_color_output(self.shader, "test", "alpha", 0, e)[0][0]["parameters"]["source"]
        self.assertEqual(source[2], 2)
        self.assertEqual(source[10:13], [1065353216] * 3)
        self.assertEqual(source[15:18], [1048576000] * 3)

    def make_rgba(self):
        self.shader.operator = "cycles::color_ramp::1"
        children = (
            Template("palette#pos"),
            Template("palette#c", size=4, naming=NAMING.RGBA),
            Template("palette#interp", TYPE.Menu, items=tuple(str(k) for k in range(7))),
        )
        count = self.owner.add(
            "palette", 2, template=Template("palette", TYPE.Folder, tags={"cycles_rgba_ramp": "1"}, children=children)
        )
        self.shader.add("ramp", 2, template=count.template).referenced = count
        for i, position in enumerate((0.0, 1.0), 1):
            self.owner.add(f"palette{i}pos", position)
            self.owner.vector(f"palette{i}c", (0.2, 0.4, 0.6, 0.5))
            self.owner.add(f"palette{i}interp", 1, TYPE.Menu)
        self.owner.add("palette_color_space", "RGB", TYPE.String)
        self.owner.add("palette_interpolation", "LINEAR", TYPE.String)
        self.owner.add("palette_hue_interpolation", "NEAR", TYPE.String)
        return __import__("_cycles_override_unit.ramps", fromlist=["lower_color_output"])

    def test_authored_bad_rgb_retains_independent_alpha_override(self):
        ramps = self.make_rgba()
        self.owner.parm("palette1cr").value = float("nan")
        e = self.evaluate("{'palette1ca':.8}")
        result = self.recovery(e, lambda: ramps.lower_color_output(self.shader, "test", "color", 0, e))
        self.assertEqual(result[0][0]["parameters"]["source"][1], 3)
        self.assertEqual(e.omitted, set())
        self.assertEqual(e.parm(self.owner.parm("palette1ca"), 0), 0.8)
        controls = ramps.ColorRampControls(self.shader, e)
        ramps.node_controls(controls, 0, e)
        self.assertNotIn(self.owner.parm("palette1ca"), controls.used)

    def test_authored_bad_alpha_retains_independent_rgb_override(self):
        ramps = self.make_rgba()
        self.owner.parm("palette1ca").value = float("nan")
        e = self.evaluate("{'palette1c':(.9,.7,.5)}")
        result = self.recovery(e, lambda: ramps.lower_color_output(self.shader, "test", "alpha", 0, e))
        self.assertEqual(result[0][0]["parameters"]["source"][1], 3)
        self.assertEqual(e.omitted, set())
        self.assertEqual(e.tuple(self.owner.parmTuple("palette1c"), 0)[:3], (0.9, 0.7, 0.5))
        controls = ramps.ColorRampControls(self.shader, e)
        ramps.node_controls(controls, 0, e, False, False)
        self.assertNotIn(self.owner.parm("palette1cr"), controls.used)

    def test_incomplete_new_stop_omits_count_preserves_valid_fields(self):
        self.make_ramp()
        e = self.evaluate("{'palette':3,'palette1value':.7,'displacement':.25}")
        controls = self.recovery(e, lambda: e.ramp(self.shader, 0))
        self.assertEqual(controls["values"], [0.7, 0.8])
        self.assertEqual(e.omitted, {"palette"})
        self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_invalid_key_recovers_position_only(self):
        self.make_ramp()
        e = self.evaluate("{'palette2pos':-.2,'palette1value':.7,'displacement':.25}")
        controls = self.recovery(e, lambda: e.ramp(self.shader, 0))
        self.assertEqual(controls["values"], [0.7, 0.8])
        self.assertEqual(e.omitted, {"palette2pos"})

    def test_authored_bad_native_position_does_not_omit_valid_value_override(self):
        self.make_ramp()
        self.owner.parm("palette1pos").value = float("nan")
        e = self.evaluate("{'palette1value':.7}")
        ramps = __import__("_cycles_override_unit.ramps", fromlist=["RampSourceError"])
        with self.assertRaises(ramps.RampSourceError):
            self.recovery(e, lambda: e.ramp(self.shader, 0))
        self.assertEqual(e.omitted, set())
        self.assertEqual(e.parm(self.owner.parm("palette1value"), 0), 0.7)

    def test_unknown_or_out_of_range_stop_is_local(self):
        self.make_ramp()
        e = self.evaluate("{'palette33value':1,'palette':33,'displacement':.25}")
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertEqual(len(e.diagnostics), 2)

    def test_complete_32_stop_rgba_expanded_dictionary_fits_budget(self):
        children = (
            Template("palette#pos"),
            Template("palette#c", size=4, naming=NAMING.RGBA),
            Template("palette#interp", TYPE.Menu, items=tuple(str(k) for k in range(7))),
        )
        count = self.owner.add(
            "palette", 2, template=Template("palette", TYPE.Folder, tags={"cycles_rgba_ramp": "1"}, children=children)
        )
        fields = {"palette": 32}
        for i in range(1, 33):
            fields.update(
                {f"palette{i}pos": (i - 1) / 31, f"palette{i}interp": 1, **{f"palette{i}c{c}": 0.5 for c in "rgba"}}
            )
        e = self.evaluate(repr(fields))
        self.assertEqual(len(fields), 193)
        self.assertFalse(e.diagnostics)
        self.assertEqual(e.parm(count, 0), 32)
        self.assertEqual(e.tuple(e.parameters(self.owner, "palette32c"), 0), (0.5,) * 4)

    def test_513_fields_fail_bounded_layer_only(self):
        e = self.evaluate("{'gain':4}", repr({f"x{i}": i for i in range(513)}))
        self.assertEqual(e.parm(self.gain, 0), 4)
        self.assertEqual(len(e.diagnostics), 1)
        self.assertIn("512", e.diagnostics[0]["message"])

    def test_byte_and_ast_limits_remain(self):
        e = self.evaluate("{'gain':4}", repr({"unknown": "x" * 65536}))
        self.assertEqual(e.parm(self.gain, 0), 4)
        self.assertEqual(len(e.diagnostics), 1)
        e = self.evaluate("{'gain':4}", "{'unknown':" + repr([0] * 4096) + "}")
        self.assertEqual(e.parm(self.gain, 0), 4)
        self.assertIn("bounded", e.diagnostics[0]["message"])

    def make_curve(self):
        """A Vector Curves X channel promoted as a native float ramp."""
        self.shader.operator = "cycles::vector_curves::1"
        self.shader.vector("curve_domain", (-1.0, 1.0), NAMING.XYZW)
        self.shader.add("curve_extension", 0, TYPE.Menu)
        template = Template("shape_x", TYPE.Ramp, ramp="float")
        count = self.owner.add("shape_x", Ramp((0.0, 1.0), (-1.0, 1.0)), template=template)
        self.shader.add("curve_x", count.value, template=template).referenced = count
        for i, (position, value) in enumerate(zip(count.value.keys(), count.value.values()), 1):
            self.owner.add(f"shape_x{i}pos", position)
            self.owner.add(f"shape_x{i}value", value)
            self.owner.add(
                f"shape_x{i}interp", 1, TYPE.Menu, Template("mode", TYPE.Menu, items=tuple(str(k) for k in range(7)))
            )
        return count

    def test_promoted_curve_channel_new_stop_is_owned_without_mutation(self):
        self.make_curve()
        before = tuple(self.owner.parms())
        e = self.evaluate(
            "{'shape_x':3,'shape_x2pos':.5,'shape_x2value':.8,'shape_x3pos':1.,'shape_x3value':2.5,'shape_x3interp':2}"
        )
        controls = e.ramp(self.shader, 0, "curve_x")
        self.assertEqual(controls["keys"], [0.0, 0.5, 1.0])
        self.assertEqual(controls["values"], [-1.0, 0.8, 2.5])
        self.assertEqual(controls["bases"][2], "catmull_rom")
        self.assertFalse(controls["color"])
        self.assertEqual(tuple(self.owner.parms()), before)

    def test_incomplete_curve_stop_omits_count_preserves_valid_fields(self):
        self.make_curve()
        e = self.evaluate("{'shape_x':3,'shape_x1value':-.5,'displacement':.25}")
        controls = self.recovery(e, lambda: e.ramp(self.shader, 0, "curve_x"))
        self.assertEqual(controls["values"], [-0.5, 1.0])
        self.assertEqual(e.omitted, {"shape_x"})
        self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_missing_curve_channel_is_a_local_ramp_fault(self):
        self.make_curve()
        e = self.evaluate("{'displacement':.25}")
        ramps = __import__("_cycles_override_unit.ramps", fromlist=["RampSourceError"])
        with self.assertRaises(ramps.RampSourceError):
            e.ramp(self.shader, 0, "curve_y")
        self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def lazy_reference(self, consumer, source, phase="query"):
        def discovered():
            if consumer not in source.consumers:
                source.consumers.append(consumer)

        if phase == "query":
            original = consumer.isTimeDependent

            def query():
                discovered()
                return original()

            consumer.isTimeDependent = query
        else:
            original = consumer.evalAtTime

            def read(now):
                discovered()
                return original(now)

            consumer.evalAtTime = read

    def test_first_active_hscript_dependency_uses_owned_override(self):
        p = self.shader.add("strength", 4.0, expression='ch("../gain")*2')
        self.lazy_reference(p, self.gain)
        e = self.evaluate("{'gain':9,'displacement':.25}")
        e.animated_nodes.add(self.owner)
        self.assertEqual(e.parm(p, 0), 18)
        self.assertEqual(p.evaluations, 0)
        self.assertIn(self.owner, e.animated_nodes)
        self.assertFalse(e.diagnostics)

    def test_first_active_python_dependency_warns_before_fallback(self):
        p = self.shader.add("strength", 4.0, expression='hou.parm("../gain").eval()*2', language="python")
        self.lazy_reference(p, self.gain)
        e = self.evaluate("{'gain':9,'displacement':.25}")
        with self.assertRaises(self.module.UnsupportedOverride) as raised:
            e.parm(p, 0)
        self.assertEqual(raised.exception.field, "gain")
        self.assertEqual(p.evaluations, 0)
        e.omit(raised.exception)
        self.assertEqual(e.parm(p, 0), 4)
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertEqual(e.omitted, {"gain"})
        self.assertEqual(len(e.diagnostics), 1)

    def test_dependency_registered_during_active_native_eval_is_revisited(self):
        p = self.shader.add("strength", 4.0, expression='ch("../gain")*2')
        self.lazy_reference(p, self.gain, "eval")
        e = self.evaluate("{'gain':9}")
        self.assertEqual(e.parm(p, 0), 18)
        self.assertEqual(p.evaluations, 1)

    def test_unused_lazy_python_is_not_queried_or_evaluated(self):
        p = self.shader.add("unused", 4.0, expression='hou.parm("../gain").eval()*2', language="python")

        def fail():
            raise AssertionError("Unused expression queried")

        p.isTimeDependent = fail
        e = self.evaluate("{'gain':9,'displacement':.25}")
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertFalse(e.diagnostics)
        self.assertEqual(p.evaluations, 0)

    def test_constant_reads_do_not_repeat_dependency_observation(self):
        p = self.shader.add("constant", 3.0)
        e = self.evaluate("{'gain':9}")
        observations = []
        if hasattr(e, "_observe_dependencies"):
            original = e._observe_dependencies

            def observe():
                observations.append(True)
                original()

            e._observe_dependencies = observe
        self.assertEqual(e.parm(p, 0), 3)
        self.assertEqual(e.parm(p, 1), 3)
        self.assertEqual(observations, [])

    def test_native_count_lazy_dependency_never_reads_whole_ramp(self):
        count = self.make_ramp()
        count.code = 'ch("gain")'
        self.lazy_reference(count, self.gain)

        def fail(frame):
            raise AssertionError("Owned count should not evaluate native ramp data")

        count.evalAsRampAtFrame = fail
        count.evalAsIntAtFrame = fail
        e = self.evaluate("{'gain':3}")
        self.assertEqual(e.count(count, 0, self.shader), 3)

    def test_python_dependency_registered_during_eval_warns_locally(self):
        p = self.shader.add("strength", 4.0, expression='hou.parm("../gain").eval()*2', language="python")
        self.lazy_reference(p, self.gain, "eval")
        e = self.evaluate("{'gain':9,'displacement':.25}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 4)
        self.assertEqual(e.omitted, {"gain"})
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertEqual(len(e.diagnostics), 1)

    def test_native_count_dependencies_discovered_by_integer_read(self):
        count = self.make_ramp()
        count.code = 'ch("gain")'
        reads = []

        def integer_read(frame):
            reads.append(frame)
            self.gain.consumers.append(count)
            return 2

        def fail(frame):
            raise AssertionError("Whole ramp must not be evaluated")

        count.evalAsIntAtFrame = integer_read
        count.evalAsRampAtFrame = fail
        e = self.evaluate("{'gain':3}")
        self.assertEqual(e.count(count, 0, self.shader), 3)
        self.assertEqual(len(reads), 1)

    def test_native_ramp_accepts_exact_integral_expression_count(self):
        count = self.make_ramp()
        count.code = 'ch("gain")'
        self.lazy_reference(count, self.gain)
        e = self.evaluate("{'gain':3,'palette2pos':.5,'palette3pos':1,'palette3value':.4,'palette3interp':1}")
        controls = e.ramp(self.shader, 0)
        self.assertEqual(controls["keys"], [0.0, 0.5, 1.0])
        self.assertEqual(controls["values"], [0.2, 0.8, 0.4])
        self.assertFalse(e.diagnostics)

    def test_fractional_expression_count_omits_only_its_contribution(self):
        count = self.make_ramp()
        count.code = 'ch("gain")'
        self.lazy_reference(count, self.gain)
        e = self.evaluate("{'gain':3.5,'palette1value':.7,'displacement':.25}")
        controls = self.recovery(e, lambda: e.ramp(self.shader, 0))
        self.assertEqual(controls["values"], [0.7, 0.8])
        self.assertEqual(e.omitted, {"gain"})
        self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_expression_observation_is_bounded_to_active_native_reads(self):
        p = self.shader.add("strength", 4.0, expression='ch("../gain")*2')
        self.lazy_reference(p, self.gain)
        e = self.evaluate("{'gain':9}")
        observations = []
        original = e._observe_dependencies

        def observe():
            observations.append(True)
            original()

        e._observe_dependencies = observe
        self.assertEqual(e.parm(p, 0), 18)
        self.assertEqual(len(observations), 1)
        self.assertEqual(e.parm(p, 1), 18)
        self.assertEqual(len(observations), 1)
        other = self.shader.add("unrelated_expression", 5.0, expression="2+3")
        self.assertEqual(e.parm(other, 0), 5)
        self.assertEqual(len(observations), 3)

    def test_authored_fixture_snapshot_uses_passive_raw_values(self):
        path = ROOT / "tests/material_ramp_override_fixture.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        method = next(
            child
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == "Fixture"
            for child in item.body
            if isinstance(child, ast.FunctionDef) and child.name == "authored"
        )
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
        expression = ["relative Python expression"]

        def fail():
            raise AssertionError("Snapshot primed an authored expression")

        keyed = types.SimpleNamespace(
            path=lambda: "/mat/test/strength", keyframes=fail, eval=fail, rawValue=lambda: expression[0]
        )
        plain = types.SimpleNamespace(path=lambda: "/mat/test/power", keyframes=fail, eval=fail, rawValue=lambda: "2")
        fixture = types.SimpleNamespace(
            material=types.SimpleNamespace(allSubChildren=lambda: (), parms=lambda: (keyed, plain))
        )
        before = namespace["authored"](fixture)
        self.assertEqual(before[0], ("/mat/test/strength", expression[0]))
        self.assertEqual(before[1], ("/mat/test/power", "2"))
        expression[0] = "repaired Hscript expression"
        self.assertNotEqual(namespace["authored"](fixture), before)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args, remaining = parser.parse_known_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = dict(
        result="passed" if result.wasSuccessful() else "failed",
        tests=result.testsRun,
        errors=[str(error) for _, error in result.errors],
        failures=[str(error) for _, error in result.failures],
        native_processes=False,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
