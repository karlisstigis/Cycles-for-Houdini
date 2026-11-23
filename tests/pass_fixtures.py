# SPDX-License-Identifier: Apache-2.0
"""Fixture shorthand authored into the current pass multiparm.

Only tests use this adapter: it keeps unrelated scene recipes concise during
the schema replacement. It neither patches HOM nor adds old ROP parameters.
The layout/output acceptance tests use the new controls directly.
"""

import re
import hou
from cycles_houdini import passes


def _rop(node):
    return isinstance(node, hou.RopNode) and node.type().name() == "cycles_ipr"


def _indices(node, kinds):
    return [i for i in range(1, node.evalParm("pass_count") + 1) if node.evalParm("pass_type" + str(i)) in kinds]


def _builtin(node, kind, create=True):
    if kind == "combined":
        return None
    if kind not in passes.menu()[::2]:
        return None
    found = _indices(node, {kind})
    if not found and create:
        found = [passes.add(node, kind, enable=False)]
    return node.parm("pass_enable" + str(found[0])) if found else None


class _Count:
    def __init__(self, node, kinds):
        self.node, self.kinds = node, kinds

    def eval(self):
        return len(_indices(self.node, self.kinds))

    def set(self, count):
        for index in reversed(_indices(self.node, self.kinds)):
            self.node.parm("pass_count").removeMultiParmInstance(index - 1)
        for index in range(int(count)):
            passes.add(self.node, sorted(self.kinds)[0], source="fixture_" + str(index + 1))


class _Levels:
    def __init__(self, node):
        self.node = node

    def eval(self):
        return max(
            [
                self.node.evalParm("pass_levels" + str(i))
                for i in _indices(self.node, {"crypto_object", "crypto_material", "crypto_asset"})
            ]
            or [6]
        )

    def set(self, value):
        for i in _indices(self.node, {"crypto_object", "crypto_material", "crypto_asset"}):
            self.node.parm("pass_levels" + str(i)).set(value)


class _AovType:
    def __init__(self, node, index):
        self.parm = node.parm("pass_type" + str(index))

    def set(self, value):
        self.parm.set("aov_" + value)

    def eval(self):
        return self.parm.eval()[4:]


def parm(node, name):
    if not _rop(node):
        return node.parm(name)
    if name in ("aov_count", "lightgroup_count"):
        return _Count(node, {"aov_color", "aov_value"} if name == "aov_count" else {"lightgroup"})
    if name == "crypto_levels":
        return _Levels(node)
    if name in ("crypto_object", "crypto_material", "crypto_asset"):
        return _builtin(node, name)
    if name.startswith("pass_") and name not in ("pass_alpha_threshold", "pass_count"):
        kind = name[5:]
        if kind in passes.menu()[::2] or kind == "combined":
            return _builtin(node, kind)
    match = re.fullmatch(r"(aov|lightgroup)_(name|type|enable)(\d+)", name)
    if match:
        family, field, index = match.groups()
        indices = _indices(node, {"aov_color", "aov_value"} if family == "aov" else {"lightgroup"})
        actual = indices[int(index) - 1]
        if field == "type":
            return _AovType(node, actual)
        return node.parm("pass_" + ("source" if field == "name" else "enable") + str(actual))
    return node.parm(name)


def evaluate(node, name):
    if _rop(node) and (
        name.startswith("pass_")
        and name[5:] in passes.menu()[::2]
        or name.startswith("crypto_")
        and name != "crypto_levels"
    ):
        p = _builtin(node, name[5:] if name.startswith("pass_") else name, create=False)
        return p.eval() if p is not None else 0
    p = parm(node, name)
    return p.eval() if p is not None else node.evalParm(name)


def set_parameters(node, values):
    if not _rop(node):
        return node.setParms(values)
    values = dict(values)
    for key in ("aov_count", "lightgroup_count"):
        if key in values:
            parm(node, key).set(values.pop(key))
    normal = {}
    for name, value in values.items():
        if node.parm(name) is not None or node.parmTuple(name) is not None:
            normal[name] = value
            continue
        target = parm(node, name)
        if target is None:
            raise ValueError("Unknown fixture parameter: " + name)
        target.set(value)
    if normal:
        node.setParms(normal)
