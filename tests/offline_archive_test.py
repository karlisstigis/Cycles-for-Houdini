# SPDX-License-Identifier: Apache-2.0
"""Conservative archive ownership, validation and recovery failure contracts."""

import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class OfflineArchiveTest(unittest.TestCase):
    def setUp(self):
        self.carrier = Mock()
        self.hou = types.SimpleNamespace(node=Mock(return_value=self.carrier))
        with patch.dict(sys.modules, hou=self.hou):
            spec = importlib.util.spec_from_file_location(
                "test_offline_runtime", ROOT / "houdini/python3.13libs/cycles_houdini_offline.py"
            )
            self.offline = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.offline)
        definitions = patch.object(self.offline, "_prepare_scene_definitions")
        self.definitions = definitions.start()
        self.addCleanup(definitions.stop)
        self.carrier.dataBlockType.return_value = ""
        self.identity = str(uuid.uuid4())

    def archive(self, manifest):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
        self.carrier.dataBlock.return_value = stream.getvalue()

    def test_future_typed_block_is_preserved_on_save(self):
        self.carrier.dataBlockType.return_value = "future-native-type"
        with patch.object(self.offline, "_notice"):
            self.offline.capture()
        self.carrier.setDataBlock.assert_not_called()

    def test_absent_block_uses_actual_builtin_value_error(self):
        self.carrier.dataBlockType.side_effect = ValueError("Unknown data block key")
        self.assertEqual(self.offline._read(), ({}, {}))

    def test_malformed_metadata_is_preserved_on_save(self):
        for manifest in (
            dict(format=2),
            dict(format=1, records=[dict(id=[])]),
            dict(format=1, records=[dict(id=self.identity, path=5)]),
        ):
            self.archive(manifest)
            with patch.object(self.offline, "_notice"):
                self.offline.capture()
        self.carrier.setDataBlock.assert_not_called()

    def test_damaged_placeholder_identity_cannot_be_overwritten_by_healthy_clone(self):
        healthy, damaged = Mock(), Mock()
        for node in (healthy, damaged):
            node.userData.return_value = self.identity
        self.carrier.allSubChildren.return_value = (healthy, damaged)
        with (
            patch.object(self.offline, "_read", return_value=({}, {})),
            patch.object(self.offline, "_roots", return_value=[healthy]),
            patch.object(self.offline, "_is_review", return_value=False),
            patch.object(self.offline, "_notice"),
        ):
            self.offline.capture()
        healthy.parent.assert_not_called()
        self.carrier.setDataBlock.assert_not_called()

    def test_noncanonical_marker_is_never_written_to_new_archive(self):
        node = Mock()
        node.userData.return_value = self.identity.upper()
        self.carrier.allSubChildren.return_value = (node,)
        with (
            patch.object(self.offline, "_read", return_value=({}, {})),
            patch.object(self.offline, "_roots", return_value=[node]),
            patch.object(self.offline, "_is_review", return_value=False),
            patch.object(self.offline, "_notice"),
        ):
            self.offline.capture()
        node.parent.assert_not_called()
        self.carrier.setDataBlock.assert_not_called()

    def test_failed_explicit_load_retains_partial_review_nodes(self):
        destination, partial = Mock(), Mock()
        destination.path.return_value = "/obj/recovery_review"
        destination.children.side_effect = [(), (partial,)]
        destination.loadItemsFromFile.side_effect = RuntimeError("load failure")
        record = dict(path="/mat/authored", type="cycles::material::1")
        with patch.object(
            self.offline, "_read", return_value=({self.identity: record}, {self.identity: b"native payload"})
        ):
            with self.assertRaises(RuntimeError):
                self.offline.recover_copy(self.identity, destination)
        partial.setUserData.assert_called_once_with("cycles.offline.review", "1")
        partial.destroy.assert_not_called()
        destination.destroy.assert_not_called()
        destination.setUserData.assert_not_called()
        self.carrier.setDataBlock.assert_not_called()

    def record(self, nodes=None):
        return dict(
            id=self.identity,
            path="/mat/authored",
            type="cycles::material::1",
            sha256=hashlib.sha256(b"cpio").hexdigest(),
            nodes=nodes if nodes is not None else [dict(path=".", type="cycles::material::1")],
        )

    def test_native_node_paths_must_be_relative_unique_and_include_matching_root(self):
        for nodes in (
            [],
            [dict(path=".", type="null")],
            [dict(path=".", type="cycles::material::1"), dict(path="../outside", type="null")],
            [dict(path=".", type="cycles::material::1"), dict(path="/absolute", type="null")],
            [dict(path=".", type="cycles::material::1")] * 2,
        ):
            self.archive(dict(format=1, records=[self.record(nodes)]))
            with self.assertRaises(self.offline.ArchiveError):
                self.offline._read()

    def test_duplicate_zip_entries_cannot_select_ambiguous_payload(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", json.dumps(dict(format=1, records=[self.record()])))
            archive.writestr(self.identity + ".cpio", b"cpio")
            with self.assertWarns(UserWarning):
                archive.writestr(self.identity + ".cpio", b"other")
        self.carrier.dataBlock.return_value = stream.getvalue()
        with self.assertRaisesRegex(self.offline.ArchiveError, "duplicated archive entry"):
            self.offline._read()

    def test_record_root_paths_must_be_canonical(self):
        for path in ("/", "/mat//authored", "/mat/./authored", "/mat/../authored", "/mat/authored/", "/mat\\authored"):
            record = self.record()
            record["path"] = path
            self.archive(dict(format=1, records=[record]))
            with self.assertRaises(self.offline.ArchiveError):
                self.offline._read()

    def test_unsupported_or_encrypted_zip_preserves_original_block(self):
        with patch.object(self.offline.zipfile, "ZipFile") as archive, patch.object(self.offline, "_notice"):
            for failure in (NotImplementedError("unsupported codec"), RuntimeError("encrypted archive")):
                archive.return_value.__enter__.return_value.read.side_effect = failure
                self.offline.capture()
        self.carrier.setDataBlock.assert_not_called()

    def test_authored_null_is_valid_but_archived_cycles_fallback_is_preserved(self):
        root, child = Mock(), Mock()
        root.allSubChildren.return_value = (child,)
        root.relativePathTo.return_value = "nested/passthrough"
        child.type.return_value.name.return_value = "null"
        child.userData.return_value = None
        self.assertFalse(self.offline._has_damaged_descendants(root, {}))
        self.assertTrue(
            self.offline._has_damaged_descendants(
                root, dict(nodes=[dict(path="nested/passthrough", type="cycles::value::1")])
            )
        )
        child.userData.return_value = "surviving-cycles-identity"
        self.assertTrue(self.offline._has_damaged_descendants(root, {}))

    def test_inspection_does_not_call_changed_or_ambiguous_graph_healthy(self):
        root = Mock()
        root.userData.return_value = self.identity
        root.type.return_value.name.return_value = "cycles::material::1"
        self.carrier.allSubChildren.return_value = (root,)
        self.hou.node.side_effect = lambda path: root if path == "/mat/authored" else self.carrier
        with (
            patch.object(self.offline, "_read", return_value=({self.identity: self.record()}, {})),
            patch.object(self.offline, "_is_review", return_value=False),
            patch.object(self.offline, "_graph_nodes", return_value=[]),
        ):
            self.assertIn("changed graph structure", self.offline.inspect()[0]["status"])
            self.carrier.allSubChildren.return_value = (root, root)
            self.assertIn("duplicate identity", self.offline.inspect()[0]["status"])

    def test_explicit_load_must_match_archived_graph_not_just_root_type(self):
        destination, child = Mock(), Mock()
        destination.path.return_value = "/obj/recovery_review"
        destination.children.side_effect = [(), (child,), (child,)]
        child.type.return_value.name.return_value = "cycles::material::1"
        child.name.return_value = "authored"
        child.userData.return_value = self.identity
        with (
            patch.object(
                self.offline, "_read", return_value=({self.identity: self.record()}, {self.identity: b"cpio"})
            ),
            patch.object(self.offline, "_graph_nodes", return_value=[]),
        ):
            with self.assertRaisesRegex(self.offline.ArchiveError, "differs from saved root"):
                self.offline.recover_copy(self.identity, destination)
        child.destroy.assert_not_called()
        child.setUserData.assert_called_once_with("cycles.offline.review", "1")

    def test_loaded_structural_loss_retains_archive_on_next_save(self):
        self.hou.hipFileEventType = types.SimpleNamespace(
            BeforeSave="save", BeforeLoad="before", AfterClear="clear", AfterLoad="load"
        )
        root = Mock()
        root.userData.return_value = self.identity
        self.carrier.allSubChildren.return_value = (root,)
        row = dict(id=self.identity, path="/mat/authored", archive="/obj/block", status="changed graph structure")
        with (
            patch.object(self.offline, "_inspect", return_value=[row]),
            patch.object(self.offline, "_notice"),
            patch.object(
                self.offline, "_read", return_value=({self.identity: self.record()}, {self.identity: b"good graph"})
            ),
            patch.object(self.offline, "_roots", return_value=[root]),
            patch.object(self.offline, "_is_review", return_value=False),
        ):
            self.offline._scene_event("load")
            self.offline.capture()
        self.assertIn(self.identity, self.offline._unresolved)
        root.parent.assert_not_called()
        self.carrier.setDataBlock.assert_not_called()
        self.offline._scene_event("before")
        self.assertFalse(self.offline._unresolved)

    def test_healthy_load_keeps_subsequent_artist_edits_capturable(self):
        self.hou.hipFileEventType = types.SimpleNamespace(
            BeforeSave="save", BeforeLoad="before", AfterClear="clear", AfterLoad="load"
        )
        row = dict(id=self.identity, status="healthy graph structure")
        with (
            patch.object(self.offline, "_inspect", return_value=[row]),
            patch.object(self.offline, "_read", return_value=({}, {})),
            patch.object(self.offline, "_separate_copies", return_value={}),
        ):
            self.offline._scene_event("load")
        self.assertNotIn(self.identity, self.offline._unresolved)

    def test_explicit_acceptance_only_releases_unique_healthy_original(self):
        root = Mock()
        root.userData.return_value = self.identity
        root.type.return_value.name.return_value = "cycles::material::1"
        self.hou.node.side_effect = lambda path: root if path == "/mat/authored" else self.carrier
        self.carrier.allSubChildren.return_value = (root,)
        self.offline._unresolved.add(self.identity)
        with (
            patch.object(self.offline, "_read", return_value=({self.identity: self.record()}, {})),
            patch.object(self.offline, "_is_review", return_value=False),
            patch.object(self.offline, "_has_damaged_descendants", return_value=False),
        ):
            self.offline.accept_current(self.identity)
            self.assertNotIn(self.identity, self.offline._unresolved)
            self.offline._unresolved.add(self.identity)
            self.carrier.allSubChildren.return_value = (root, root)
            with self.assertRaises(self.offline.ArchiveError):
                self.offline.accept_current(self.identity)
            self.assertIn(self.identity, self.offline._unresolved)

    def capture_fixture(self):
        root = Mock()
        root.userData.return_value = self.identity
        root.path.return_value = "/mat/authored"
        root.type.return_value.name.return_value = "cycles::material::1"
        self.carrier.allSubChildren.return_value = (root,)
        patches = dict(
            _roots=Mock(return_value=[root]),
            _is_review=Mock(return_value=False),
            _has_damaged_descendants=Mock(return_value=False),
            _graph_nodes=Mock(return_value=self.record()["nodes"]),
            _graph_connections=Mock(return_value=[]),
        )
        return root, patches

    def test_identical_native_bytes_skip_only_secondary_archive_and_metadata_walks(self):
        root, patches = self.capture_fixture()
        record = self.record()
        self.offline._capture_proofs[self.identity] = (record, b"cpio")
        with (
            patch.multiple(self.offline, **patches),
            patch.object(self.offline, "_read", return_value=({self.identity: record}, {self.identity: b"cpio"})),
            patch.object(self.offline, "_serialize_graph", return_value=b"cpio") as serialize,
        ):
            self.offline.capture()
            self.offline.capture()
        self.assertEqual(self.definitions.call_count, 2, "Native fallback must republish on every save")
        self.assertEqual(serialize.call_count, 2, "Full native authored bytes remain the change proof")
        patches["_graph_nodes"].assert_not_called()
        patches["_graph_connections"].assert_not_called()
        self.carrier.setDataBlock.assert_not_called()
        self.assertEqual(self.offline.statistics()["unchanged_roots"], 2)

    def test_changed_payload_or_path_recaptures_without_using_parameter_shortcuts(self):
        for payload, path in (
            (b"edited expression, channels, flags or metadata", "/mat/authored"),
            (b"cpio", "/mat/moved"),
        ):
            with self.subTest(payload=payload, path=path):
                root, patches = self.capture_fixture()
                root.path.return_value = path
                with (
                    patch.multiple(self.offline, **patches),
                    patch.object(
                        self.offline, "_read", return_value=({self.identity: self.record()}, {self.identity: b"cpio"})
                    ),
                    patch.object(self.offline, "_serialize_graph", return_value=payload),
                    patch.object(self.offline, "_write") as write,
                ):
                    self.offline.capture()
                self.assertEqual(write.call_args.args[1][self.identity], payload)
                self.assertEqual(write.call_args.args[0][self.identity]["path"], path)
                patches["_graph_nodes"].assert_called_once_with(root)

    def test_loaded_or_replaced_manifest_requires_metadata_recapture_even_with_identical_payload(self):
        root, patches = self.capture_fixture()
        record = self.record()
        records = {self.identity: record}
        with (
            patch.multiple(self.offline, **patches),
            patch.object(self.offline, "_read", return_value=(records, {self.identity: b"cpio"})),
            patch.object(self.offline, "_serialize_graph", return_value=b"cpio"),
            patch.object(self.offline, "_write") as write,
        ):
            self.offline.capture()
            self.assertEqual(write.call_count, 1, "Loaded metadata has no session-local native proof")
            records[self.identity]["nodes"].append(dict(path="external_manifest_edit", type="null"))
            self.offline.capture()
            self.assertEqual(write.call_count, 2, "External metadata replacement must lose its prior proof")

    def test_read_cache_requires_exact_block_bytes_and_does_not_leak_record_mutations(self):
        self.offline._write({self.identity: self.record()}, {self.identity: b"cpio"})
        data = self.carrier.setDataBlock.call_args.args[1]
        self.carrier.dataBlock.return_value = data
        records, _ = self.offline._read()
        records[self.identity]["nodes"].clear()
        with patch.object(self.offline.zipfile, "ZipFile", side_effect=AssertionError("must reuse validated bytes")):
            again, _ = self.offline._read()
        self.assertEqual(again[self.identity]["nodes"], self.record()["nodes"])
        self.carrier.dataBlock.return_value = b"externally replaced future block"
        with self.assertRaises(self.offline.ArchiveError):
            self.offline._read()

    def test_supported_hom_writer_is_always_used_without_low_level_substitution(self):
        root = Mock()
        root.path.return_value = "/mat/authored"
        package = types.SimpleNamespace(
            _native=types.SimpleNamespace(
                serialize_scene_graph=Mock(side_effect=RuntimeError("uncertain native state"))
            )
        )

        def save(items, path):
            self.assertEqual(items, (root,))
            Path(path).write_bytes(b"complete native HOM archive")

        root.parent.return_value.saveItemsToFile.side_effect = save
        with patch.dict(sys.modules, cycles_houdini=package), patch.object(self.offline, "_notice"):
            self.assertEqual(self.offline._serialize_graph(root), b"complete native HOM archive")
        self.assertEqual(self.offline.statistics()["temporary_serializations"], 1)
        package._native.serialize_scene_graph.assert_not_called()

    def test_save_scoped_scratch_truncates_stale_bytes_and_cleans_up_on_failure(self):
        root, patches = self.capture_fixture()
        seen = []

        def failed_save(items, path):
            seen.append(Path(path))
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).read_bytes(), b"")
            Path(path).write_bytes(b"partial output must never be retained")
            raise RuntimeError("native save failed")

        root.parent.return_value.saveItemsToFile.side_effect = failed_save
        with (
            patch.multiple(self.offline, **patches),
            patch.object(self.offline, "_read", return_value=({}, {})),
            patch.object(self.offline, "_write") as write,
        ):
            with self.assertRaisesRegex(RuntimeError, "native save failed"):
                self.offline.capture()
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].parent.exists())
        self.assertFalse(self.offline._busy)
        write.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.cpio"
            path.write_bytes(b"previous root bytes")
            with self.assertRaisesRegex(RuntimeError, "native save failed"):
                self.offline._serialize_graph(root, directory)

    def test_empty_native_output_never_reuses_the_previous_root(self):
        root = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.cpio"
            path.write_bytes(b"previous root bytes")
            with self.assertRaisesRegex(self.offline.ArchiveError, "empty archive"):
                self.offline._serialize_graph(root, directory)

    def test_one_scratch_directory_is_shared_by_all_healthy_roots_in_a_save(self):
        root, patches = self.capture_fixture()
        second = Mock()
        second.userData.return_value = str(uuid.uuid4())
        second.path.return_value = "/mat/second"
        second.type.return_value.name.return_value = "cycles::material::1"
        patches["_roots"].return_value = [root, second]
        self.carrier.allSubChildren.return_value = (root, second)
        directories = []

        def serialize(node, directory):
            self.assertTrue(Path(directory).is_dir())
            directories.append(directory)
            return node.path().encode()

        with (
            patch.multiple(self.offline, **patches),
            patch.object(self.offline, "_read", return_value=({}, {})),
            patch.object(self.offline, "_serialize_graph", side_effect=serialize),
            patch.object(self.offline, "_write"),
        ):
            self.offline.capture()
        self.assertEqual(len(directories), 2)
        self.assertEqual(directories[0], directories[1])
        self.assertFalse(Path(directories[0]).exists())
        self.assertEqual(self.offline.statistics()["temporary_directories"], 1)

    def test_external_archive_damage_after_valid_cache_never_overwrites_new_bytes(self):
        record = self.record()
        self.offline._write({self.identity: record}, {self.identity: b"cpio"})
        self.offline._capture_proofs[self.identity] = (record, b"cpio")
        self.carrier.setDataBlock.reset_mock()
        self.carrier.dataBlock.return_value = b"externally damaged unknown block"
        with patch.object(self.offline, "_notice"), patch.object(self.offline, "_roots") as roots:
            self.offline.capture()
        roots.assert_not_called()
        self.definitions.assert_called_once_with()
        self.carrier.setDataBlock.assert_not_called()

    def holder(self, path, session, identity=None):
        node = Mock()
        data = {self.offline.MARKER: identity or self.identity}
        node.userData.side_effect = data.get
        node.setUserData.side_effect = data.__setitem__
        node.path.return_value = path
        node.sessionId.return_value = session
        return node

    def separate(self, nodes, roots, records):
        self.carrier.allSubChildren.return_value = tuple(nodes)
        with (
            patch.object(self.offline, "_roots", return_value=list(roots)),
            patch.object(self.offline, "_is_review", return_value=False),
        ):
            return self.offline._separate_copies(records)

    def test_copy_rule_node_at_archived_path_keeps_identity_even_if_newer(self):
        copy, original = self.holder("/mat/authored1", 3), self.holder("/mat/authored", 9)
        copies = self.separate((copy, original), (copy, original), {self.identity: self.record()})
        self.assertEqual(copies, {"/mat/authored1": "/mat/authored"})
        self.assertEqual(original.userData(self.offline.MARKER), self.identity)
        fresh = copy.userData(self.offline.MARKER)
        self.assertEqual(str(uuid.UUID(fresh)), fresh)
        self.assertNotEqual(fresh, self.identity)
        self.assertIn(self.identity, self.offline._separated)

    def test_copy_rule_without_archived_path_the_oldest_node_keeps_identity(self):
        nodes = [self.holder(f"/mat/m{index}", session) for index, session in enumerate((7, 2, 5))]
        copies = self.separate(nodes, nodes, {})
        self.assertEqual(copies, {"/mat/m0": "/mat/m1", "/mat/m2": "/mat/m1"})
        identities = [node.userData(self.offline.MARKER) for node in nodes]
        self.assertEqual(identities[1], self.identity)
        self.assertEqual(len(set(identities)), 3)

    def test_copy_rule_leaves_conflicts_with_placeholder_or_nested_holders_for_review(self):
        healthy, placeholder = self.holder("/mat/authored1", 9), self.holder("/mat/authored", 3)
        self.assertEqual(self.separate((healthy, placeholder), (healthy,), {self.identity: self.record()}), {})
        healthy.setUserData.assert_not_called()
        placeholder.setUserData.assert_not_called()
        self.assertNotIn(self.identity, self.offline._separated)

    def test_unique_identities_are_never_rewritten(self):
        nodes = [self.holder("/mat/a", 1), self.holder("/mat/b", 2, str(uuid.uuid4()))]
        self.assertEqual(self.separate(nodes, nodes, {}), {})
        for node in nodes:
            node.setUserData.assert_not_called()

    def test_only_additions_or_rewiring_count_as_authored_growth(self):
        record = self.record([dict(path=".", type="cycles::material::1"), dict(path="shader", type="cycles::x::1")])
        record["connections"] = [dict(target=".", input=0, source="shader", output=0)]
        root = Mock()
        grown = record["nodes"] + [dict(path="added", type="cycles::mix::1")]
        rewired = [
            dict(target=".", input=0, source="added", output=0),
            dict(target="added", input=0, source="shader", output=0),
        ]
        for nodes, wires, expected in (
            (grown, rewired, True),
            (record["nodes"], record["connections"], True),
            (grown[:1] + grown[2:], rewired, False),  # an archived node vanished
            ([dict(path=".", type="cycles::material::1"), dict(path="shader", type="null")], [], False),  # type changed
            (grown, [], False),  # an archived input lost its wire
        ):
            with (
                patch.object(self.offline, "_graph_nodes", return_value=nodes),
                patch.object(self.offline, "_graph_connections", return_value=wires),
            ):
                self.assertIs(self.offline._authored_growth(root, record), expected, (nodes, wires))

    def test_inspection_accepts_growth_only_for_separated_originals(self):
        root = Mock()
        root.userData.return_value = self.identity
        root.type.return_value.name.return_value = "cycles::material::1"
        self.carrier.allSubChildren.return_value = (root,)
        self.hou.node.side_effect = lambda path: root if path == "/mat/authored" else self.carrier
        with (
            patch.object(self.offline, "_read", return_value=({self.identity: self.record()}, {})),
            patch.object(self.offline, "_is_review", return_value=False),
            patch.object(self.offline, "_same_structure", return_value=False),
            patch.object(self.offline, "_authored_growth", return_value=True),
            patch.object(self.offline, "_has_damaged_descendants", return_value=False),
        ):
            self.assertIn("changed graph structure", self.offline.inspect()[0]["status"])
            self.offline._separated.add(self.identity)
            self.assertTrue(self.offline.inspect()[0]["status"].startswith("healthy"))

    def test_artist_notices_name_the_action(self):
        row = dict(path="/mat/Threads", archive="/obj/block", status="changed graph structure (x)")
        text = self.offline._explain(row)
        self.assertIn("/mat/Threads", text)
        self.assertIn("right-click it > Cycles > Keep Current Graph", text)
        self.assertIn("/mat/new", self.offline._explain(dict(row, status="missing", moved="/mat/new")))

    def test_keep_current_menu_only_on_material_waiting_for_review(self):
        root = Mock()
        root.userData.return_value = self.identity
        root.type.return_value.name.return_value = "cycles::material::1"
        root.path.return_value = "/mat/authored"
        self.hou.isUIAvailable = Mock(return_value=False)
        self.assertFalse(self.offline.needs_review(dict(node=root)))
        self.assertFalse(self.offline.needs_review({}))
        self.assertFalse(self.offline.needs_review(None))
        self.offline._unresolved.add(self.identity)
        self.assertTrue(self.offline.needs_review(dict(node=root)))
        with patch.object(self.offline, "accept_current") as accept, patch("builtins.print"):
            self.assertTrue(self.offline.keep_current(dict(node=root)))
        accept.assert_called_once_with(self.identity)
        with (
            patch.object(self.offline, "accept_current", side_effect=self.offline.ArchiveError("ambiguous")),
            patch("builtins.print"),
        ):
            self.assertFalse(self.offline.keep_current(dict(node=root)))

    def test_copy_event_defers_one_check_and_ignores_hip_loading(self):
        self.hou.hipFile = types.SimpleNamespace(isLoadingHipFile=Mock(return_value=False))
        self.hou.isUIAvailable = Mock(return_value=True)
        deferred = []
        with patch.object(self.offline, "_defer", side_effect=deferred.append):
            self.offline._child_created(node=Mock(), child_node=Mock())
            self.offline._child_created(node=Mock(), child_node=Mock())
            self.assertEqual(deferred, [self.offline._after_copy])
            self.offline._copy_pending = False
            self.hou.hipFile.isLoadingHipFile.return_value = True
            self.offline._child_created(node=Mock(), child_node=Mock())
            self.assertEqual(len(deferred), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
