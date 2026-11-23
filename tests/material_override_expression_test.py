# SPDX-License-Identifier: Apache-2.0
"""Pure numeric/channel/time override evaluation against exact stock-HFS rows."""

from support.paths import artifacts_root
import argparse
import json
import math
from pathlib import Path
import sys
import unittest
import material_override_evaluation_test as baseline

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = json.loads((ROOT / "tests/fixtures/houdini-material-expressions.json").read_text())
CONSUMERS = json.loads((ROOT / "tests/fixtures/houdini-material-expression-consumers.json").read_text())


class Tests(baseline.Tests):
    def expression(self, code, value=99.0, layer="{'gain':.35,'displacement':.25}"):
        parm = self.shader.add("result", value, expression=code)
        self.gain.consumers.append(parm)
        return parm, self.evaluate(layer)

    def test_all_native_numeric_channel_and_time_references(self):
        rows = [row for row in REFERENCES["cases"] if not row["native_errors"]]
        self.assertEqual(len(rows), 304)
        for row in rows:
            with self.subTest(id=row["id"], fps=row["fps"], frame=row["frame"]):
                self.setUp()
                fps = row["fps"]
                self.module.hou.timeToFrame = lambda now: now * fps + 1
                self.module.hou.frameToTime = lambda frame: (frame - 1) / fps
                self.owner.add("animated", lambda now: 1 + now * 8)
                parm, e = self.expression(row["expression"])
                self.assertAlmostEqual(e.parm(parm, row["time"]), row["result"], places=12)
                self.assertEqual(parm.evaluations, 0, "Native authored result must not replace the owned input")

    def test_native_unknown_chi_and_bad_arity_are_local(self):
        for row in [row for row in REFERENCES["cases"] if row["native_errors"]]:
            with self.subTest(id=row["id"]):
                self.setUp()
                parm, e = self.expression(row["expression"], value=0.9)
                self.assertEqual(self.recovery(e, lambda: e.parm(parm, 0)), 0.9)
                self.assertEqual(e.omitted, {"gain"})
                self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_rejects_execution_dynamic_paths_and_unknown_syntax(self):
        codes = [
            '__import__("os").system("never")+ch("../gain")',
            'hou.parm("../gain").eval()',
            'ch("../"+"gain")',
            'ch("../$F")',
            'ch("../gain",1)',
            'min(ch("../gain"),a=2)',
            'ch("../gain")**2',
            'ch("../gain")%2',
            'True+ch("../gain")',
            '[ch("../gain")][0]',
            'abs.__call__(ch("../gain"))',
            '$FPS+ch("../gain")',
            'sin(ch("../gain"))',
            'chs("../gain")+"x"',
        ]
        for code in codes:
            with self.subTest(code=code):
                self.setUp()
                parm, e = self.expression(code, value=0.8)
                self.assertEqual(self.recovery(e, lambda: e.parm(parm, 0)), 0.8)
                self.assertEqual(e.omitted, {"gain"})
                self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_division_zero_and_nonfinite_results_omit_only_contributing_field(self):
        for code in ['ch("../gain")/0', 'ch("../gain")*1e38*1e38', 'abs(1e309)+ch("../gain")']:
            self.setUp()
            parm, e = self.expression(code, value=0.7)
            self.assertEqual(self.recovery(e, lambda: e.parm(parm, 0)), 0.7)
            self.assertEqual(e.omitted, {"gain"})

    def test_requested_time_not_global_timeline_controls_variables(self):
        self.module.hou.timeToFrame = lambda now: now * 24 + 1
        for code, now, expected in [
            ('$F+ch("../gain")', 1.5 / 24, 3.35),
            ('$FF+ch("../gain")', 1.5 / 24, 2.85),
            ('$T+ch("../gain")', 0.125, 0.475),
        ]:
            self.setUp()
            parm, e = self.expression(code)
            self.assertAlmostEqual(e.parm(parm, now), expected)
            self.assertIn(self.shader, e.animated_nodes)

    def test_explicit_frame_and_time_sample_the_owned_dependency_at_requested_offset(self):
        self.module.hou.timeToFrame = lambda now: now * 24 + 1
        self.module.hou.frameToTime = lambda frame: (frame - 1) / 24
        source = self.owner.add("sampled", lambda now: 2 + now * 4)
        for code, expected in [
            ('chf("../sampled",7)+ch("../gain")', 3.35),
            ('cht("../sampled",.5)+ch("../gain")', 4.35),
        ]:
            parm, e = self.expression(code)
            self.assertAlmostEqual(e.parm(parm, 99), expected)
            self.assertIn(self.shader, e.animated_nodes)
        self.assertEqual(source.evaluations, 2)

    def test_sampled_target_override_is_owned_and_never_native_evaluated(self):
        source = self.owner.add("sampled", lambda now: 999)
        self.module.hou.frameToTime = lambda frame: (frame - 1) / 24
        parm, e = self.expression(
            'chf("../sampled",48)+cht("../sampled",-1)+ch("../gain")', layer="{'sampled':.8,'gain':.35}"
        )
        self.assertAlmostEqual(e.parm(parm, 0), 1.95)
        self.assertEqual(source.evaluations, 0)

    def test_missing_sampled_target_then_recreated_target_repairs_locally(self):
        self.module.hou.frameToTime = lambda frame: (frame - 1) / 24
        parm, e = self.expression('ch("../gain")+chf("../missing",8)', value=0.2)
        self.assertEqual(self.recovery(e, lambda: e.parm(parm, 0)), 0.2)
        self.assertEqual(e.omitted, {"gain"})
        self.owner.add("missing", lambda now: 0.8 if now > 0 else 0.2)
        repaired = self.evaluate("{'gain':.35,'displacement':.25}")
        self.assertAlmostEqual(repaired.parm(parm, 0), 1.15)
        self.assertFalse(repaired.diagnostics)
        self.assertEqual(repaired.parm(self.displacement, 0), 0.25)

    def test_explicit_sample_only_edit_changes_result_without_requested_value_change(self):
        self.module.hou.frameToTime = lambda frame: (frame - 1) / 24
        source = self.owner.add("sampled", lambda now: 0.2 if now == 0 else 0.8)
        parm, e = self.expression('ch("../gain")+chf("../sampled",8)')
        self.assertAlmostEqual(e.parm(parm, 0), 1.15)
        self.assertEqual(source.evalAtTime(0), 0.2)
        source.value = lambda now: 0.2 if now == 0 else 1.2
        refreshed = self.evaluate("{'gain':.35,'displacement':.25}")
        self.assertAlmostEqual(refreshed.parm(parm, 0), 1.55)
        self.assertEqual(source.evalAtTime(0), 0.2)
        self.assertFalse(refreshed.diagnostics)
        self.assertIn(self.shader, refreshed.animated_nodes)

    def test_active_lazy_function_dependency_is_discovered_before_native_value(self):
        parm = self.shader.add("result", 99.0, expression='fit(ch("../gain"),0,1,2,6)')

        def discover():
            self.gain.consumers.append(parm)
            return False

        parm.isTimeDependent = discover
        e = self.evaluate("{'gain':.35}")
        self.assertAlmostEqual(e.parm(parm, 0), 3.4)
        self.assertEqual(parm.evaluations, 0)

    def test_inactive_unsupported_expression_and_external_sources_are_not_evaluated(self):
        bad = self.shader.add(
            "dormant", lambda now: self.fail("Dormant authored expression evaluated"), expression='chi("../gain")'
        )
        self.gain.consumers.append(bad)
        e = self.evaluate("{'gain':.35,'displacement':.25}")
        self.assertEqual(e.parm(self.displacement, 0), 0.25)
        self.assertFalse(e.diagnostics)
        self.assertFalse(e.animated_nodes)

    def test_integral_function_driven_native_ramp_count_preserves_lazy_count(self):
        self.make_ramp()
        count = self.owner.parm("palette")
        count.code = 'int(ch("gain"))'
        self.gain.consumers.append(count)
        e = self.evaluate("{'gain':2.9}")
        controls = e.ramp(self.shader, 0)
        self.assertEqual(controls["values"], [0.2, 0.8])
        self.assertEqual(count.evaluations, 0)

    def test_direct_string_reference_remains_supported(self):
        source = self.owner.add("texture", "authored", baseline.TYPE.String)
        target = self.shader.add("filename", "authored", baseline.TYPE.String, expression='chs("../texture")')
        source.consumers.append(target)
        e = self.evaluate("{'texture':'fixed.exr'}")
        self.assertEqual(e.parm(target, 0), "fixed.exr")

    def test_expression_bounds_and_syntax_cache_are_bounded(self):
        module = sys.modules[self.module.__package__ + ".override_expressions"]
        for expression in [
            'ch("../gain")+' + "1+" * 200 + "1",
            "-" * 80 + 'ch("../gain")',
            " " * 4097 + 'ch("../gain")',
        ]:
            with self.assertRaises((ValueError, SyntaxError, RecursionError)):
                module.evaluate(expression, lambda *args: 0.35, lambda name: 0)
        for index in range(300):
            module.evaluate(str(index), lambda *args: 0, lambda name: 0)
        self.assertLessEqual(module._syntax.cache_info().currsize, 256)

    def test_deferred_integer_toggle_and_menu_match_native_coercion(self):
        for row in CONSUMERS["cases"]:
            if (
                abs(row["gain"]) > 2**63 - 1
                or row["kind"] == "integer"
                and not -2147483648 <= row["value"] <= 2147483647
            ):
                continue
            with self.subTest(kind=row["kind"], value=row["gain"]):
                self.setUp()
                kind = {
                    "integer": baseline.TYPE.Int,
                    "bounded": baseline.TYPE.Int,
                    "toggle": baseline.TYPE.Toggle,
                    "mode": baseline.TYPE.Menu,
                }[row["kind"]]
                template = baseline.Template("typed", kind, items=("a", "b") if row["kind"] == "mode" else ())
                if row["kind"] == "bounded":
                    template.minIsStrict = lambda: True
                    template.maxIsStrict = lambda: True
                    template.minValue = lambda: 0
                    template.maxValue = lambda: 32767
                p = self.shader.add("typed", 0, kind, template, expression='ch("../gain")')
                self.gain.consumers.append(p)
                e = self.evaluate(repr({"gain": row["gain"], "displacement": 0.25}))
                self.assertEqual(e.parm(p, 0), row["value"])
                self.assertIs(type(e.parm(p, 0)), int)
                if row["kind"] == "mode":
                    self.assertEqual(e.token(p, 0), row["string"])
                self.assertFalse(e.diagnostics)

    def test_deferred_out_of_precision_integer_results_recover_before_token_lookup(self):
        for kind in (baseline.TYPE.Int, baseline.TYPE.Menu, baseline.TYPE.Toggle):
            self.setUp()
            p = self.shader.add(
                "typed",
                0,
                kind,
                baseline.Template("typed", kind, items=("a", "b") if kind == baseline.TYPE.Menu else ()),
                expression='floor(ch("../gain"))',
            )
            self.gain.consumers.append(p)
            e = self.evaluate("{'gain':3e38,'displacement':.25}")
            self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 0)
            self.assertEqual(e.omitted, {"gain"})
            self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_safe_native_integer_still_requires_final_shader_representability(self):
        p = self.shader.add("typed", 0, baseline.TYPE.Int, expression='int(ch("../gain"))')
        self.gain.consumers.append(p)
        e = self.evaluate("{'gain':1e12,'displacement':.25}")
        self.assertEqual(self.recovery(e, lambda: e.parm(p, 0)), 0)
        self.assertEqual(e.omitted, {"gain"})
        self.assertEqual(e.parm(self.displacement, 0), 0.25)

    def test_deferred_integer_control_honors_declared_hard_bounds(self):
        template = baseline.Template("typed", baseline.TYPE.Int)
        template.minIsStrict = lambda: True
        template.maxIsStrict = lambda: True
        template.minValue = lambda: 0
        template.maxValue = lambda: 32767
        p = self.shader.add("typed", 0, baseline.TYPE.Int, template, expression='floor(ch("../gain"))')
        self.gain.consumers.append(p)
        for source, expected in ((40000, 32767), (-2, 0), (4.8, 4)):
            e = self.evaluate(repr({"gain": source}))
            self.assertEqual(e.parm(p, 0), expected)

    def test_intermediate_integer_channels_apply_native_coercion_before_outer_arithmetic(self):
        middle = self.owner.add("integer", 0, baseline.TYPE.Int, expression='ch("gain")')
        self.gain.consumers.append(middle)
        p = self.shader.add("outer", 99.0, expression='ch("../integer")/2')
        middle.consumers.append(p)
        e = self.evaluate("{'gain':2.6}")
        self.assertEqual(e.parm(p, 0), 1.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=artifacts_root() / "material-override-expressions-unit.json")
    args = parser.parse_args()
    names = [name for name in Tests.__dict__ if name.startswith("test_")]
    suite = unittest.TestSuite(Tests(name) for name in names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            dict(
                result="passed" if result.wasSuccessful() else "failed",
                tests=result.testsRun,
                reference_rows=len(REFERENCES["cases"]),
                consumer_reference_rows=len(CONSUMERS["cases"]),
                errors=[str(e) for e in result.errors],
                failures=[str(e) for e in result.failures],
            ),
            indent=2,
        )
        + "\n"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)
