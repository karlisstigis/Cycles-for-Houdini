# SPDX-License-Identifier: Apache-2.0
"""Blender asset catalog file parsing into a tree."""

import pytest

from cycles_houdini.assets import catalogs

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"
D = "44444444-4444-4444-8444-444444444444"


def test_parse_builds_sorted_tree_with_implicit_parents():
    text = "\n".join(
        [
            "\ufeff# This is an Asset Catalog Definition file for Blender.",
            "",
            "VERSION 1",
            f"{A}:Materials/metal/Rusty Steel:Materials-metal-Rusty Steel",
            f"{B}:  /Materials//Wood/ :Materials-Wood",
            f"{C}:props:props",
        ]
    )
    tree = catalogs.parse_catalog_text(text)
    assert tree.warnings == []
    assert [child.name for child in tree.root.children] == ["Materials", "props"]
    materials = tree.root.children[0]
    assert materials.uuids == []  # implicit parent
    assert [child.path for child in materials.children] == ["Materials/metal", "Materials/Wood"]
    assert tree.node_for_uuid(A).path == "Materials/metal/Rusty Steel"
    assert tree.node_for_uuid(B.upper()).path == "Materials/Wood"
    assert tree.subtree_uuids("Materials") == {A, B}
    assert tree.subtree_uuids("") == {A, B, C}
    assert tree.subtree_uuids("missing") == set()


def test_simple_names_keep_colons_and_paths_keep_spaces():
    tree = catalogs.parse_catalog_text(f"VERSION 1\n{A}:My Stuff/Sub Folder:a:b:c\n")
    node = tree.node_for_uuid(A)
    assert node.path == "My Stuff/Sub Folder" and node.name == "Sub Folder"
    assert node.simple_names == ["a:b:c"]


def test_duplicate_uuid_first_wins_and_shared_paths_merge():
    tree = catalogs.parse_catalog_text(f"{A}:one:x\n{A}:two:y\n{B}:one:z\n")
    assert tree.node_for_uuid(A).path == "one"
    assert tree.node_for_path("one").uuids == [A, B]
    assert tree.node_for_path("one").simple_names == ["x", "z"]
    assert tree.node_for_path("two") is None
    assert len(tree.warnings) == 1 and "Duplicate" in tree.warnings[0]


def test_invalid_lines_and_versions_are_warnings():
    text = f"VERSION 2\nnonsense\nnot-a-uuid:path:name\n{catalogs.NULL_UUID}:null:n\n{C}:   :empty\n{D}:ok\n"
    tree = catalogs.parse_catalog_text(text)
    assert len(tree.warnings) == 5
    assert "version" in tree.warnings[0]
    assert tree.node_for_uuid(D).simple_names == [""]


def test_children_sort_case_insensitively():
    tree = catalogs.parse_catalog_text(f"{A}:beta:b\n{B}:Alpha:a\n{C}:gamma:g\n")
    assert [child.name for child in tree.root.children] == ["Alpha", "beta", "gamma"]


def test_load_missing_file_is_empty(tmp_path):
    tree = catalogs.load_catalogs(tmp_path)
    assert tree.is_empty() and tree.warnings == []
    (tmp_path / catalogs.CATALOG_FILENAME).write_bytes(f"\ufeffVERSION 1\n{A}:x:x\n".encode())
    assert catalogs.load_catalogs(tmp_path).node_for_uuid(A).path == "x"


def test_listing_catalog_entries():
    tree = catalogs.tree_from_entries(
        [{"path": "a/b", "uuids": [A, B], "simple_name": "a-b"}, {"path": "c"}, "junk", {"path": "d", "uuids": ["x"]}]
    )
    assert tree.node_for_path("a/b").uuids == [A, B]
    assert tree.subtree_uuids("a") == {A, B}
    assert len(tree.warnings) == 3


def test_normalize_uuid():
    assert catalogs.normalize_uuid(catalogs.NULL_UUID) == ""
    assert catalogs.normalize_uuid(None) == ""
    assert catalogs.normalize_uuid(" " + A.upper()) == A
