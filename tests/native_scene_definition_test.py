# SPDX-License-Identifier: Apache-2.0
"""Pure contracts for native HIP definitions and retained graph wire metadata."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import posixpath
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "houdini/python3.13libs/cycles_houdini_offline.py"
SCRIPT = '{\n name "native class"\n parm { name "full_class_parameter" default { "kept" } }\n}\n'


class Kind:
    def __init__(self, name="cycles::emission::1", category="vop", definition=None, script=SCRIPT):
        self._name, self._category, self._definition, self.script = name, category, definition, script
        self.dialog_requests = []

    def name(self):
        return self._name

    def category(self):
        return self._category

    def definition(self):
        return self._definition

    def parmTemplateGroup(self):
        return self

    def asDialogScript(self, **kwargs):
        self.dialog_requests.append(kwargs)
        return self.script


class Node:
    def __init__(self, path, kind=None, inputs=(), outputs=(), children=(), parent=None):
        self._path, self._kind = path, kind or Kind()
        self.inputs, self.outputs, self.children, self._parent = inputs, outputs, children, parent
        self.wires = []
        self.children_calls = []

    def path(self):
        return self._path

    def type(self):
        return self._kind

    def inputNames(self):
        return self.inputs

    def outputNames(self):
        return self.outputs

    def parent(self):
        return self._parent

    def allSubChildren(self, **kwargs):
        self.children_calls.append(kwargs)
        return self.children

    def inputConnections(self):
        return tuple(self.wires)

    def relativePathTo(self, other):
        return posixpath.relpath(other.path(), self.path())


class Wire:
    def __init__(self, source, input_index=0, output_index=0, indirect=None):
        self.source, self.i, self.o, self.indirect = source, input_index, output_index, indirect

    def inputNode(self):
        return self.source

    def inputIndex(self):
        return self.i

    def outputIndex(self):
        return self.o

    def subnetIndirectInput(self):
        return self.indirect


class NativeSceneDefinitionTest(unittest.TestCase):
    def setUp(self):
        self.scene = Node("/")
        self.carrier = Mock()
        self.hou = types.SimpleNamespace(
            node=Mock(side_effect=lambda path: self.scene if path == "/" else self.carrier),
            vopNodeTypeCategory=lambda: "vop",
            hipFile=Mock(),
            hipFileEventType=types.SimpleNamespace(
                BeforeSave="before_save", BeforeLoad="before_load", AfterClear="after_clear", AfterLoad="after_load"
            ),
        )
        self.native = types.SimpleNamespace(
            scene_definition_fallback=Mock(return_value=dict(registered=True, updated=True)),
            clear_scene_definitions=Mock(),
        )
        package = types.ModuleType("cycles_houdini")
        package._native = self.native
        modules = patch.dict(sys.modules, hou=self.hou, cycles_houdini=package)
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location("scene_definitions_under_test", PRODUCT)
        self.offline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.offline)

    def test_static_native_port_order_preserves_full_class_script_and_escapes_names(self):
        kind = Kind()
        names = ("zeta", 'a"quote', "slash\\name", "")
        first = Node("/mat/m/first", kind, inputs=names, outputs=("color", "alpha"))
        second = Node("/mat/m/second", kind, inputs=names, outputs=("color", "alpha"))
        self.scene.children = (first, second)
        self.offline._prepare_scene_definitions()
        self.native.scene_definition_fallback.assert_called_once()
        path, script, ins, outs = self.native.scene_definition_fallback.call_args.args
        self.assertEqual(path, first.path())
        self.assertEqual((ins, outs), (3, 2))
        expected_ports = "".join(
            direction + " float " + json.dumps(name) + " " + json.dumps(name) + "\n"
            for direction, ports in (("input", ("zeta", 'a"quote', "slash\\name")), ("output", ("color", "alpha")))
            for name in ports
        )
        close = SCRIPT.rfind("}")
        self.assertEqual(script, SCRIPT[:close] + expected_ports + SCRIPT[close:])
        self.assertEqual(kind.dialog_requests, [dict(full_info=True)])
        # The selected representative path can change; definition content and
        # class port order must be independent of scene traversal/instance order.
        self.scene.children = (second, first)
        self.offline._prepare_scene_definitions()
        self.assertEqual(self.native.scene_definition_fallback.call_args.args[1:], (script, ins, outs))

    def test_conflicting_same_type_input_or_output_order_is_rejected(self):
        kind = Kind()
        schemas = (
            (("left", "right"), ("right", "left"), ("color", "alpha"), ("color", "alpha")),
            (("left", "right"), ("left", "right"), ("color", "alpha"), ("alpha", "color")),
            (("left", "right"), ("left",), ("color", "alpha"), ("color", "alpha")),
            (("left", "right"), ("left", "right"), ("color", "alpha"), ("color", "alpha", "extra")),
        )
        for first_inputs, second_inputs, first_outputs, second_outputs in schemas:
            with self.subTest(inputs=second_inputs, outputs=second_outputs):
                self.native.scene_definition_fallback.reset_mock()
                self.scene.children = (
                    Node("/mat/first", kind, inputs=first_inputs, outputs=first_outputs),
                    Node("/mat/second", kind, inputs=second_inputs, outputs=second_outputs),
                )
                with self.assertRaises(self.offline.ArchiveError):
                    self.offline._prepare_scene_definitions()
                # Native ownership is established before enforcing our static
                # schema rule; another DSO may own a Cycles-namespaced type.
                self.native.scene_definition_fallback.assert_called_once()
                self.assertEqual(self.native.scene_definition_fallback.call_args.args[0], "/mat/first")

    def test_foreign_varying_schema_is_skipped_without_blocking_owned_next_type(self):
        foreign = Kind("cycles::foreign_dynamic::1")
        self.scene.children = (
            Node("/mat/foreign_a", foreign, inputs=("left", "right"), outputs=("alpha", "color")),
            Node("/mat/foreign_b", foreign, inputs=("right", "left"), outputs=("color", "alpha")),
            Node("/mat/owned", Kind("cycles::emission::1"), inputs=("color", "strength"), outputs=("emission",)),
        )
        self.native.scene_definition_fallback.side_effect = [dict(unowned=True), dict(registered=True, updated=True)]
        self.offline._prepare_scene_definitions()
        self.assertEqual(
            [call.args[0] for call in self.native.scene_definition_fallback.call_args_list],
            ["/mat/foreign_a", "/mat/owned"],
        )
        self.assertEqual(self.offline.statistics()["native_definition_types"], 1)

    def test_only_native_definition_scan_visits_loaded_locked_children_without_delayed_sync(self):
        self.scene.children = (Node("/mat/native"),)
        self.offline._prepare_scene_definitions()
        self.assertEqual(self.scene.children_calls, [dict(recurse_in_locked_nodes=True, sync_delayed_definition=False)])
        # Definition discovery can describe already-loaded classes inside a
        # locked asset. Archive graph traversal keeps its existing boundary;
        # this mock test makes no native locked-asset roundtrip claim.
        self.offline._graph_nodes(self.scene)
        self.offline._graph_connections(self.scene)
        self.assertEqual(self.scene.children_calls[1:], [dict(recurse_in_locked_nodes=False)] * 2)

    def test_builtin_subnets_and_connectors_with_different_dynamic_interfaces_are_excluded(self):
        self.scene.children = (
            Node("/mat/first", Kind("subnet"), inputs=("hue", "gain"), outputs=("rgb",)),
            Node("/mat/second", Kind("subnet"), inputs=("factor",), outputs=("alpha", "color")),
            Node("/mat/first/input", Kind("subnetconnector"), outputs=("hue",)),
            Node("/mat/second/input", Kind("subnetconnector"), outputs=("factor",)),
        )
        self.offline._prepare_scene_definitions()
        self.native.scene_definition_fallback.assert_not_called()

    def test_unowned_native_class_skips_only_itself_and_registers_remaining_types(self):
        self.scene.children = (Node("/mat/a", Kind("cycles::foreign::1")), Node("/mat/b", Kind("cycles::emission::1")))
        self.native.scene_definition_fallback.side_effect = [dict(unowned=True), dict(registered=True, updated=True)]
        self.offline._prepare_scene_definitions()
        self.assertEqual(self.native.scene_definition_fallback.call_count, 2)
        self.assertEqual(self.offline.statistics()["native_definition_types"], 1)
        self.assertEqual(self.offline.statistics()["native_definition_updates"], 1)

    def test_noncycles_nonvop_and_hda_types_are_not_registered(self):
        self.scene.children = (
            Node("/mat/a", Kind("add")),
            Node("/obj/b", Kind(category="obj")),
            Node("/mat/c", Kind(definition=object())),
            Node("/mat/native", Kind("cycles::emission::1")),
        )
        self.offline._prepare_scene_definitions()
        self.assertEqual(self.native.scene_definition_fallback.call_args.args[0], "/mat/native")
        self.native.scene_definition_fallback.assert_called_once()

    def test_invalid_class_script_and_failed_registration_are_errors(self):
        self.scene.children = (Node("/mat/bad", Kind(script="missing closing brace")),)
        with self.assertRaisesRegex(self.offline.ArchiveError, "invalid native operator dialog"):
            self.offline._prepare_scene_definitions()
        self.native.scene_definition_fallback.assert_not_called()
        self.scene.children = (Node("/mat/valid"),)
        self.native.scene_definition_fallback.return_value = dict(registered=False)
        with self.assertRaisesRegex(self.offline.ArchiveError, "registration failed"):
            self.offline._prepare_scene_definitions()

    def test_native_definitions_register_before_unknown_archive_is_preserved(self):
        self.scene.children = (Node("/mat/native"),)
        order = []
        self.native.scene_definition_fallback.side_effect = lambda *args: order.append("register") or dict(
            registered=True, updated=False
        )

        def unreadable():
            order.append("read archive")
            raise self.offline.ArchiveError("unknown future format")

        with patch.object(self.offline, "_read", side_effect=unreadable), patch.object(self.offline, "_notice"):
            self.offline.capture()
        self.assertEqual(order, ["register", "read archive"])
        self.carrier.setDataBlock.assert_not_called()
        self.assertFalse(self.offline._busy)

    def test_scene_load_and_clear_clear_native_cache_camera_events_do_not(self):
        self.offline._reported.add("old")
        self.offline._unresolved.add("old")
        self.offline._scene_event("camera_parameter_edit")
        self.native.clear_scene_definitions.assert_not_called()
        self.native.scene_definition_fallback.assert_not_called()
        self.offline._scene_event(self.hou.hipFileEventType.BeforeLoad)
        self.assertFalse(self.offline._reported)
        self.assertFalse(self.offline._unresolved)
        self.offline._scene_event(self.hou.hipFileEventType.AfterClear)
        self.assertEqual(self.native.clear_scene_definitions.call_count, 2)
        with (
            patch.object(self.offline, "_has_cycles_content", return_value=True),
            patch.object(self.offline, "capture") as capture,
        ):
            self.offline._scene_event(self.hou.hipFileEventType.BeforeSave)
        capture.assert_called_once_with()
        with (
            patch.object(self.offline, "_has_cycles_content", return_value=False),
            patch.object(self.offline, "capture") as capture,
        ):
            self.offline._scene_event(self.hou.hipFileEventType.BeforeSave)
        capture.assert_not_called()
        self.offline.install()
        self.offline.install()
        self.hou.hipFile.addEventCallback.assert_called_once_with(self.offline._scene_event)

    def test_cache_clear_does_not_import_absent_plugin(self):
        with patch.dict(sys.modules, {"cycles_houdini": None}):
            self.offline._clear_scene_definitions()
        self.native.clear_scene_definitions.assert_not_called()

    def test_cache_clear_never_loads_an_uninitialized_runtime(self):
        spec = importlib.util.spec_from_file_location(
            "scene_definition_lazy_runtime", ROOT / "houdini/python3.13libs/cycles_houdini/core/native.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        load = Mock(side_effect=RuntimeError("SDK unavailable"))
        proxy = module.NativeRuntime(load)
        with patch.dict(sys.modules, {"cycles_houdini": types.SimpleNamespace(_native=proxy)}):
            self.offline._scene_event(self.hou.hipFileEventType.BeforeLoad)
            self.offline._scene_event(self.hou.hipFileEventType.AfterClear)
            load.assert_not_called()
            proxy._module = self.native
            self.offline._scene_event(self.hou.hipFileEventType.AfterClear)
        self.native.clear_scene_definitions.assert_called_once_with()

    def test_update_statistics_count_only_native_reported_changes(self):
        self.scene.children = (Node("/mat/native"),)
        self.native.scene_definition_fallback.side_effect = [
            dict(registered=True, updated=True),
            dict(registered=True, updated=False),
        ]
        self.offline._prepare_scene_definitions()
        self.offline._prepare_scene_definitions()
        self.assertEqual(self.offline.statistics()["native_definition_updates"], 1)
        self.assertEqual(self.offline.statistics()["native_definition_types"], 1)

    def graph(self):
        root = Node("/mat/authored", Kind("cycles::material::1"))
        source = Node(root.path() + "/source", parent=root)
        subnet = Node(root.path() + "/nested", parent=root)
        child = Node(subnet.path() + "/child", parent=subnet)
        target = Node(root.path() + "/target", parent=root)
        root.children = (target, child, source, subnet)
        return root, source, subnet, child, target

    def test_connection_metadata_preserves_indirect_inputs_and_skips_boundaries_and_disconnected_sources(self):
        root, source, subnet, child, target = self.graph()
        target.wires = [
            Wire(source, input_index=3, output_index=2),
            Wire(None, input_index=8),
            Wire(Node("/mat/unrelated"), input_index=7),
        ]
        indirect = types.SimpleNamespace(number=lambda: 4)
        wire = Wire(None, input_index=1, output_index=99, indirect=indirect)
        wire.inputNode = Mock(side_effect=AssertionError("Indirect source must use parent, not inputNode"))
        child.wires = [wire]
        root.wires = [Wire(Node("/mat/external"), input_index=0)]
        self.assertEqual(
            self.offline._graph_connections(root),
            [
                dict(target="nested/child", input=1, source="nested", output=4, indirect=True),
                dict(target="target", input=3, source="source", output=2),
            ],
        )
        wire.inputNode.assert_not_called()

    def test_same_structure_rejects_a_lost_or_redirected_wire_without_requiring_node_loss(self):
        root, source, subnet, child, target = self.graph()
        target.wires = [Wire(source, input_index=1, output_index=2)]
        record = dict(
            nodes=list(reversed(self.offline._graph_nodes(root))), connections=self.offline._graph_connections(root)
        )
        self.assertTrue(self.offline._same_structure(root, record))
        target.wires = []
        self.assertFalse(self.offline._same_structure(root, record))
        target.wires = [Wire(subnet, input_index=1, output_index=2)]
        self.assertFalse(self.offline._same_structure(root, record))
        # Legacy metadata has no wire evidence and remains explicitly limited
        # to node structure, so old scenes stay inspectable.
        self.assertTrue(self.offline._same_structure(root, dict(nodes=record["nodes"])))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    product_hash = hashlib.sha256(PRODUCT.read_bytes()).hexdigest()
    outcome = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(NativeSceneDefinitionTest)
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                dict(
                    result="passed" if outcome.wasSuccessful() else "failed",
                    tests=outcome.testsRun,
                    failures=len(outcome.failures),
                    errors=len(outcome.errors),
                    test_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    product_sha256=product_hash,
                    product_unchanged=product_hash == hashlib.sha256(PRODUCT.read_bytes()).hexdigest(),
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
