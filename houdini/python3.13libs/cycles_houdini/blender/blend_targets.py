# SPDX-License-Identifier: Apache-2.0
"""Pure target expressions. Quoted menu tokens retain exact Blender identity."""

import fnmatch
import json
import re


def alias(name):
    return name.replace(" ", "_")


# Reference grammar: path.blend#object=Name, path.blend#collection=Name, and
# path.blend#collection+disabled=Name. The last also keeps the child collections
# that the source file excludes from its view layer. The option is part of the
# reference itself, so every bounds, preview and prototype cache keyed by the
# reference string keeps the two selections apart.
INCLUDE_DISABLED_COLLECTIONS = "collection+disabled"
REFERENCE_KINDS = ("object", "collection", INCLUDE_DISABLED_COLLECTIONS)


def reference_kind(kind, include_disabled_collections=False):
    """Reference grammar kind for a row's Object / Collection choice and checkbox."""
    return INCLUDE_DISABLED_COLLECTIONS if kind == "collection" and include_disabled_collections else kind


def row_reference_kind(row):
    return reference_kind(row["kind"], row.get("include_disabled_collections", False))


def row_reference(row):
    return row["file"] + "#" + row_reference_kind(row) + "=" + row["target"]


def menu_entries(names):
    names = list(dict.fromkeys(names))
    counts = {}
    for name in names:
        counts[alias(name)] = counts.get(alias(name), 0) + 1
    result = []
    for name in names:
        label = alias(name)
        if counts[label] > 1:
            label += " (" + name + ")"
        token = json.dumps(name, ensure_ascii=False) if counts[alias(name)] > 1 else alias(name)
        # A quoted exact token is also necessary when an alias contains syntax.
        if any(c.isspace() for c in token) or any(c in token for c in "*?[]^'\"\\"):
            token = json.dumps(name, ensure_ascii=False)
        result.extend((token, label))
    return result


def _tokens(expression):
    # JSON menu tokens round-trip quotes, backslashes and Unicode exactly.
    decoder = json.JSONDecoder()
    position = 0
    while position < len(expression):
        if expression[position].isspace():
            position += 1
            continue
        exclude = expression[position] == "^"
        if exclude:
            position += 1
        if position < len(expression) and expression[position] == '"':
            value, end = decoder.raw_decode(expression, position)
            if not isinstance(value, str) or end < len(expression) and not expression[end].isspace():
                raise ValueError("Separate Blender names with spaces")
            yield value, True, exclude
            position = end
        elif position < len(expression) and expression[position] == "'":
            end = expression.find("'", position + 1)
            if end < 0 or end + 1 < len(expression) and not expression[end + 1].isspace():
                raise ValueError("Unterminated quoted Blender name")
            yield expression[position + 1 : end], True, exclude
            position = end + 1
        else:
            end = position
            while end < len(expression) and not expression[end].isspace():
                end += 1
            if end == position:
                raise ValueError("Missing Blender name after ^")
            yield expression[position:end], False, exclude
            position = end


def resolve_targets(expression, names, matcher=None, bindings=None):
    """Resolve a Houdini name pattern against one ID namespace, without I/O.

    A quoted token is always an exact original ID, even if it contains wildcard
    characters. Unquoted aliases never silently choose between colliding IDs.
    """
    names = list(dict.fromkeys(names))
    matcher = matcher or (lambda pattern, value: fnmatch.fnmatchcase(value, pattern))
    selected, warnings = [], []
    for token, exact, exclude in _tokens(expression):
        wildcard = not exact and bool(re.search(r"[*?\[\]]", token))
        if exact:
            matches = [token] if not names or token in names else []
        elif wildcard:
            matches = [name for name in names if matcher(token, name) or matcher(token, alias(name))]
            if not names:
                warnings.append("Refresh the Blender catalogue to resolve pattern " + repr(token))
        elif token in (bindings or {}):
            bound = bindings[token]
            matches = [bound] if not names or bound in names else []
            if not matches:
                warnings.append("Previously selected Blender name is missing: " + repr(bound))
                continue
        else:
            matches = [name for name in names if token in (name, alias(name))]
            if len(matches) > 1:
                warnings.append("Ambiguous Blender name " + repr(token) + "; choose a quoted exact name from the menu")
                continue
            if not names:
                matches = [token]
        if not matches and not exclude and (names or exact):
            warnings.append("Blender name did not match: " + repr(token))
        if exclude:
            selected = [name for name in selected if name not in matches]
        else:
            selected.extend(name for name in matches if name not in selected)
        if len(selected) > 128:
            raise ValueError("Blender Reference accepts at most 128 resolved targets")
    return selected, warnings
