# SPDX-License-Identifier: Apache-2.0
"""Include Disabled Collections is part of the reference, so no cache mixes the two selections."""

from copy import deepcopy
import importlib
import json
import sys
import types

import pytest

from cycles_houdini.blender import blend_bounds as bounds
from cycles_houdini.blender import blend_catalogue as catalogue
from cycles_houdini.blender import blend_preview as preview
from cycles_houdini.blender import node_bounds as nb
from cycles_houdini.blender.blend_targets import (
    INCLUDE_DISABLED_COLLECTIONS,
    REFERENCE_KINDS,
    reference_kind,
    row_reference,
    row_reference_kind,
)

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def row(kind="collection", include=False, **values):
    return dict(
        dict(
            file="$HIP/rig.blend",
            kind=kind,
            target="Rig Asset",
            frame=1.0,
            name="Rig_Asset",
            include_disabled_collections=include,
        ),
        **values,
    )


def test_grammar_marks_only_collections_and_round_trips_through_the_selector():
    assert REFERENCE_KINDS == ("object", "collection", "collection+disabled")
    assert reference_kind("collection", True) == INCLUDE_DISABLED_COLLECTIONS
    assert reference_kind("collection", False) == "collection"
    # The checkbox is hidden for object rows; a stale value never changes them.
    assert reference_kind("object", True) == "object"
    assert row_reference_kind(dict(kind="collection")) == "collection"
    assert row_reference(row()) == "$HIP/rig.blend#collection=Rig Asset"
    assert row_reference(row(include=True)) == "$HIP/rig.blend#collection+disabled=Rig Asset"
    assert row_reference(row("object", True, target="Body")) == "$HIP/rig.blend#object=Body"


@pytest.fixture
def assets(monkeypatch, tmp_path):
    fake_hou = types.SimpleNamespace(
        text=types.SimpleNamespace(expandString=lambda value: value.replace("$HIP", str(tmp_path))),
        getenv=lambda name: None,
        hipFile=types.SimpleNamespace(eventCallbacks=lambda: [], addEventCallback=lambda callback: None),
    )
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    monkeypatch.setitem(
        sys.modules,
        "PyOpenColorIO",
        types.SimpleNamespace(GetCurrentConfig=lambda: types.SimpleNamespace(getCacheID=lambda: "working")),
    )
    module = importlib.import_module("cycles_houdini.blender.blender_assets")
    monkeypatch.setattr(module, "hou", fake_hou)
    executable = tmp_path / "blender.exe"
    executable.touch()
    monkeypatch.setattr(module, "_blender", lambda: executable)
    (tmp_path / "rig.blend").write_bytes(b"synthetic")
    return module


def test_disabled_variant_has_its_own_prototype_key_and_requests_excluded_children(assets):
    plain = row_reference(row())
    disabled = row_reference(row(include=True))
    key, request = assets._resolve_spec((plain, 1.0, True))
    disabled_key, disabled_request = assets._resolve_spec((disabled, 1.0, True))
    assert key != disabled_key
    assert key[:8] == disabled_key[:8] and key[2] == "COLLECTION"
    assert (key[8], disabled_key[8]) == (False, True)
    assert "include_excluded_collections" not in request
    assert disabled_request == dict(request, include_excluded_collections=True)
    assert assets._bundles._describe(disabled_key).endswith("#collection+disabled=Rig Asset frame 1")
    assert assets._bundles._describe(key).endswith("#collection=Rig Asset frame 1")
    # Upper-case spelling uses the same case-insensitive grammar.
    assert assets._resolve_spec((plain.replace("#collection=", "#COLLECTION+DISABLED="), 1.0, True))[0] == disabled_key
    # Objects have no disabled variant.
    with pytest.raises(ValueError, match="path.blend#"):
        assets._resolve_spec((row_reference(row("object")).replace("#object=", "#object+disabled="), 1.0, True))


def test_bounds_hull_refresh_passes_the_option_to_the_blender_request(assets, monkeypatch):
    monkeypatch.setattr(bounds, "_start_refresh", lambda job: None)
    monkeypatch.setattr(bounds, "_refresh_job", None)
    monkeypatch.setattr(bounds, "_preview_jobs", {})
    monkeypatch.setattr(assets.blend_worker, "_epoch", lambda source: 0)
    monkeypatch.setattr(assets.blend_worker, "launch_signature", lambda *args: "launch")
    monkeypatch.setattr(bounds, "publication_generation", lambda: (0, 0))
    specs = [(row_reference(row()), 1.0, True), (row_reference(row(include=True)), 1.0, True)]
    job = bounds.refresh_many(specs, hulls=True, hull_vertices=32)
    (group,) = job.groups
    assert [request.get("include_excluded_collections", False) for request in group["requests"]] == [False, True]
    assert all(request["viewport_hulls"] and request["max_hull_vertices"] == 32 for request in group["requests"])
    assert len(set(job.aliases.values())) == 2


def manifest(**values):
    return dict(
        dict(
            source="rig.blend",
            selector_type="COLLECTION",
            selector="Rig Asset",
            frame=1.0,
            dependency_versions={"rig.blend": [1, 2, 3, 4]},
            meshes=[],
            instances=[],
            materials=[],
            diagnostics=[],
        ),
        **values,
    )


def test_evaluated_record_carries_the_option_in_its_identity_and_resident_records():
    plain = bounds.evaluated_record(manifest(), {})
    disabled = bounds.evaluated_record(manifest(include_excluded_collections=True), {})
    assert "include_excluded_collections" not in plain
    assert disabled["include_excluded_collections"] is True
    assert disabled["source_identity"] != plain["source_identity"]
    # Records of the default selection keep their previous identity.
    assert bounds.evaluated_record(manifest(include_excluded_collections=False), {}) == plain


def test_resident_prototype_keeps_the_option_for_render_bounds(assets):
    result = assets._prepare_result(manifest(include_excluded_collections=True), {}, "working")
    assert result["bounds_manifest"]["include_excluded_collections"] is True
    entry = dict(result, cache_key=(None,) * 7 + (True, True))
    package = importlib.import_module("cycles_houdini")
    previous = getattr(package, "_native", None)
    package._native = types.SimpleNamespace(blend_mesh_bounds=lambda capsule: None)
    try:
        record = bounds.prepared_record(entry)
    finally:
        package._native = previous
    assert record["include_excluded_collections"] is True
    assert assets._prepare_result(manifest(), {}, "working")["bounds_manifest"]["include_excluded_collections"] is False


def saved(**values):
    return dict(
        dict(
            schema_version=1,
            known=True,
            reference="$HIP/rig.blend#collection=Rig Asset",
            frame=1.0,
            source="rig.blend",
            selector_type="COLLECTION",
            selector="Rig Asset",
            bounds=[0, 0, 0, 1, 1, 1],
            source_identity="a" * 64,
            dependency_versions={"rig.blend": [1, 2, 3, 4]},
        ),
        **values,
    )


@pytest.mark.parametrize(
    ("values", "valid"),
    [
        (dict(), True),
        (dict(include_excluded_collections=False), True),
        (dict(include_excluded_collections=True), False),
        (dict(reference="$HIP/rig.blend#collection+disabled=Rig Asset"), False),
        (dict(reference="$HIP/rig.blend#collection+disabled=Rig Asset", include_excluded_collections=True), True),
        (dict(reference="$HIP/rig.blend#collection+disabled=Rig Asset", include_excluded_collections=1), False),
        (dict(reference="$HIP/rig.blend#collection+disabled=Rig Asset", include_excluded_collections="1"), False),
        (
            dict(
                reference="$HIP/rig.blend#object+disabled=Body",
                selector_type="OBJECT",
                selector="Body",
                include_excluded_collections=True,
            ),
            False,
        ),
        (
            dict(
                reference="$HIP/rig.blend#object=Body",
                selector_type="OBJECT",
                selector="Body",
                include_excluded_collections=True,
            ),
            False,
        ),
    ],
)
def test_saved_bounds_and_previews_pair_only_with_their_own_spelling(values, valid):
    record = saved(**values)
    assert bool(nb.valid_record(record)) is valid
    assert bool(preview.valid_record(dict(record, max_hull_vertices=64))) is valid
    envelope = nb.encode_records([record])
    assert (nb.decode_records(envelope) == [record]) is valid


def test_saved_records_for_both_spellings_coexist_on_one_node():
    plain = saved()
    disabled = saved(
        reference="$HIP/rig.blend#collection+disabled=Rig Asset",
        include_excluded_collections=True,
        source_identity="b" * 64,
        bounds=[0, 0, 0, 1, 3, 1],
    )
    assert nb.decode_records(nb.encode_records([plain, disabled])) == [plain, disabled]
    previews = [dict(plain, max_hull_vertices=64), dict(disabled, max_hull_vertices=64)]
    assert preview.decode_records(preview.encode_records(previews)) == previews
    assert len({preview.record_key(record) for record in previews}) == 2


class Parm:
    def __init__(self, value):
        self.value = value

    def eval(self):
        return self.value

    def evalAtTime(self, _):
        return self.value

    def unexpandedString(self):
        return self.value


class Node:
    def __init__(self, rows):
        self.rows = rows
        self.values = dict(
            shape_frame=Parm(1.0),
            max_hull_vertices=Parm(64),
            viewport_shape=Parm("bounds"),
            viewport_wireframe=Parm(1),
            saved_viewport_hulls=Parm(""),
        )

    def parm(self, name):
        return self.values.get(name)


@pytest.fixture
def rows_node(monkeypatch):
    rows = [row(), row(include=True, name="Rig_Asset_all"), row("object", True, target="Body", name="Body")]
    node = Node([dict(item, file=SOURCE) for item in rows])
    monkeypatch.setattr(catalogue, "resolved_reference_rows", lambda current, time=None: (deepcopy(current.rows), []))
    monkeypatch.setitem(sys.modules, "hou", types.SimpleNamespace(text=types.SimpleNamespace(expandString=str)))
    package = importlib.import_module("cycles_houdini")
    monkeypatch.setattr(
        package, "_native", types.SimpleNamespace(blender_reference_preview_valid=lambda *args: True), raising=False
    )
    return node


SOURCE = "/project/rig.blend"
EXPECTED = [
    SOURCE + "#collection=Rig Asset",
    SOURCE + "#collection+disabled=Rig Asset",
    SOURCE + "#object=Body",
]


def test_node_rows_build_the_same_references_for_bounds_previews_and_packed_primitives(rows_node):
    assert [reference for reference, _, _ in nb._rows(rows_node)] == EXPECTED
    assert [reference for reference, _, _ in preview.preview_rows(rows_node)] == EXPECTED
    result = catalogue.resolve_node_references(rows_node)
    assert result[:2] == ["", ""], result[:2]
    fields = [result[index : index + 10] for index in range(2, len(result), 10)]
    # The native SOP rebuilds file#kind=target from these fields.
    assert [field[0] + "#" + field[1] + "=" + field[2] for field in fields] == EXPECTED
    assert [field[1] for field in fields] == ["collection", "collection+disabled", "object"]


def test_node_rows_select_saved_records_by_their_own_reference(rows_node):
    located = dict(source=SOURCE, dependency_versions={SOURCE: [1, 2, 3, 4]}, max_hull_vertices=64)
    plain = saved(reference=EXPECTED[0], **located)
    disabled = saved(reference=EXPECTED[1], include_excluded_collections=True, source_identity="b" * 64, **located)
    rows_node.values["saved_viewport_hulls"].value = preview.encode_records([plain, disabled])
    rows_node.rows = rows_node.rows[:2]
    assert preview.matching_record(rows_node, EXPECTED[0]) == plain
    assert preview.matching_record(rows_node, EXPECTED[1]) == disabled
    result = catalogue.resolve_node_references(rows_node)
    assert [json.loads(result[index + 8])["source_identity"] for index in (2, 12)] == ["a" * 64, "b" * 64]
    assert preview.has_current(rows_node)
    rows_node.values["saved_viewport_hulls"].value = preview.encode_records([plain])
    # Turning the checkbox on needs its own evaluation; the default record is not reused.
    assert not preview.has_current(rows_node)
