# SPDX-License-Identifier: Apache-2.0
"""Selection syntax preserves exact Blender identities behind artist aliases."""

import json

import pytest

from cycles_houdini.blender.blend_targets import menu_entries, resolve_targets

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_multi_name_wildcards_and_exclusion_keep_catalogue_order_and_deduplicate():
    names = ["Cube", "Rock Large", "Rock Small", "Camera", "Rock Hidden"]
    selected, warnings = resolve_targets("Cube Rock* ^Rock_Hidden Cube", names)
    assert selected == ["Cube", "Rock Large", "Rock Small"]
    assert warnings == []


def test_alias_resolves_exact_original_without_rewriting_blender_identity():
    selected, warnings = resolve_targets("Object_with_spaces", ["Object with spaces", "Other"])
    assert selected == ["Object with spaces"]
    assert warnings == []


def test_alias_collision_is_not_silently_bound_to_an_exact_underscore_name():
    names = ["Object with spaces", "Object_with_spaces"]
    selected, warnings = resolve_targets("Object_with_spaces", names)
    assert selected == []
    assert warnings and any("ambig" in warning.lower() for warning in warnings)
    for exact in names:
        selected, warnings = resolve_targets(json.dumps(exact), names)
        assert selected == [exact]
        assert warnings == []


def test_quoted_names_are_literal_even_with_wildcards_and_exclusion_characters():
    names = ["Cube*", "Cube1", "^Camera", 'a "quoted" \\ name 片']
    for exact in names:
        selected, warnings = resolve_targets(json.dumps(exact, ensure_ascii=False), names)
        assert selected == [exact]
        assert warnings == []


def test_exact_typed_target_without_catalogue_still_supports_render_time_lookup():
    selected, warnings = resolve_targets("Cube", [])
    assert selected == ["Cube"]
    assert warnings == []


def test_menu_tokens_quote_exact_names_and_labels_expose_alias_collisions():
    names = ["Object with spaces", "Object_with_spaces", '片 "quoted"']
    entries = menu_entries(names)
    assert len(entries) == len(names) * 2
    tokens, labels = entries[::2], entries[1::2]
    assert [json.loads(token) for token in tokens] == names
    assert len(set(labels)) == len(names)
    assert all("Object_with_spaces" in label for label in labels[:2])
    for token, exact in zip(tokens, names):
        assert resolve_targets(token, names) == ([exact], [])


def test_unique_menu_choices_append_readable_underscore_aliases():
    assert menu_entries(["Wine Bottle", "Cube"]) == ["Wine_Bottle", "Wine_Bottle", "Cube", "Cube"]
    assert resolve_targets("Cube Wine_Bottle", ["Wine Bottle", "Cube"]) == (["Cube", "Wine Bottle"], [])


def test_saved_alias_binding_survives_new_collision_and_never_retargets_missing_original():
    bindings = {"Wine_Bottle": "Wine Bottle"}
    assert resolve_targets("Wine_Bottle", ["Wine Bottle", "Wine_Bottle"], bindings=bindings) == (["Wine Bottle"], [])
    selected, warnings = resolve_targets("Wine_Bottle", ["Wine_Bottle"], bindings=bindings)
    assert selected == []
    assert warnings and "missing" in warnings[0].lower()
    assert resolve_targets("Wine_Bottle", [], bindings=bindings) == (["Wine Bottle"], [])


def test_production_matcher_receives_pattern_for_both_exact_and_alias_names():
    calls = []

    def matcher(pattern, name):
        calls.append((pattern, name))
        return pattern == "Rock*" and name == "Rock_Large"

    selected, warnings = resolve_targets("Rock*", ["Rock Large", "Cube"], matcher=matcher)
    assert selected == ["Rock Large"]
    assert warnings == []
    assert ("Rock*", "Rock_Large") in calls
