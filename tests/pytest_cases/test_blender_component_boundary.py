# SPDX-License-Identifier: Apache-2.0
"""Licence and process boundary checks for the Blender component and its fixture generators.

The shipped Blender component and the standalone fixture generators use GPL-3.0-or-later. The
Houdini integration exchanges files, command-line arguments and pipe messages with the component
in a separate Blender process. The Blender side may import the shared Apache-2.0 modules in SHARED.
"""

import ast
import hashlib
import re
from pathlib import Path

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

COMPONENT = "blender"
PACKAGE = "cycles_blender"
SPDX = "# SPDX-License-Identifier: GPL-3.0-or-later"
# The GNU GPL version 3 text as published by the FSF (https://www.gnu.org/licenses/gpl-3.0.txt).
GPL3_SHA256 = "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"
BLENDER_API = {"bpy", "bmesh", "mathutils", "_bpy", "bpy_extras", "gpu", "aud", "freestyle", "bl_math"}
HOST_API = {"hou", "soho", "sohog", "PySide6", "_cycles_houdini"}
# Apache-2.0 modules of the Houdini package that the Blender component may import. They need neither
# Houdini nor Blender (checked below); the package inits are imported with them.
SHARED = {
    "cycles_houdini",
    "cycles_houdini.blender",
    "cycles_houdini.blender.blend_worker",  # the pipe protocol (and the Houdini-side client)
    "cycles_houdini.blender.blend_viewport",  # hull budgets and validators
    "cycles_houdini.blender.blend_names",  # attribute field names, texture file names
    "cycles_houdini.blender.blend_ids",  # Cycles object id hashes
    "cycles_houdini.blender.blend_cache",  # byte-budget caches
}
# Source-only GPL developer utilities run inside Blender to make test fixtures.
BLENDER_FIXTURE_GENERATORS = {
    "scripts/generate-blender-hue-ramps.py",
    "scripts/generate-blender-ramp-curve-gate.py",
    "scripts/generate-blender-rgb-ramps.py",
    "scripts/generate-blender-rgba-ramps.py",
    "scripts/generate-blender-shader-curves.py",
    "scripts/generate-legacy-ramp-tables.py",
}
# The folders of the published source that hold Python (internal review notes are not published).
FOLDERS = ("blender", "houdini", "scripts", "tests", "examples", "launchers", "packaging", "src", "docs")
_LOADERS = ("spec_from_file_location", "run_path", "SourceFileLoader")
_WORD = re.compile(r"\b" + PACKAGE + r"\b")


def _imports(tree):
    """(module, line) of every import, at any depth; relative imports are left out. For
    `from package import name` the name is reported as package.name when it may be a module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno
            if node.module in ("cycles_houdini", "cycles_houdini.blender", PACKAGE):
                yield from ((node.module + "." + alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            argument = node.args[0] if node.args else None
            if name == "import_module" and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                yield argument.value, node.lineno
            elif name == "import_blender":
                yield PACKAGE, node.lineno


def _import_time_modules(tree):
    """Modules imported while the module itself is imported (not inside functions)."""
    pending, found = list(tree.body), set()
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
        pending.extend(ast.iter_child_nodes(node))
    return found


def _names_component(node):
    return any(
        isinstance(child, ast.Constant) and isinstance(child.value, str) and "blender/" + PACKAGE in child.value
        for child in ast.walk(node)
    )


def _loads_component(tree):
    """Line of a by-path load (spec_from_file_location, run_path, SourceFileLoader) whose arguments
    name a file under blender/, directly or through a variable assigned from such a path."""
    tainted = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _names_component(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if name in _LOADERS and any(
                _names_component(argument)
                or any(isinstance(child, ast.Name) and child.id in tainted for child in ast.walk(argument))
                for argument in arguments
            ):
                return node.lineno
    return None


def _docstrings(tree):
    nodes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return {
        id(node.body[0].value)
        for node in nodes
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }


def _root(name):
    return name.split(".")[0]


def violations(root):
    """Every breach of the boundary in the tree at `root`, as readable strings."""
    root = Path(root)
    found = []
    component = root / COMPONENT
    license_file = component / "LICENSE"
    if not license_file.is_file() or hashlib.sha256(license_file.read_bytes()).hexdigest() != GPL3_SHA256:
        found.append("blender/LICENSE is not the GNU GPL version 3 text")
    if not (component / "README.md").is_file():
        found.append("blender/README.md is missing")
    sources = sorted(
        p for folder in FOLDERS for p in (root / folder).rglob("*.py") if "__pycache__" not in p.relative_to(root).parts
    )
    for path in sources:
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, relative)
        modules = list(_imports(tree))
        roots = {_root(module) for module, _ in modules}
        in_component = relative.startswith(COMPONENT + "/")
        is_generator = relative in BLENDER_FIXTURE_GENERATORS
        if (in_component or is_generator) and text.splitlines()[:1] != [SPDX]:
            found.append(relative + ": first line is not " + SPDX)
        if in_component:
            for module, line in modules:
                if _root(module) in HOST_API:
                    found.append(f"{relative}:{line}: the Blender component imports {module}")
                if _root(module) == "cycles_houdini" and module not in SHARED:
                    found.append(f"{relative}:{line}: the Blender component imports {module}, not a shared module")
            continue
        uses_blender = sorted({module for module, _ in modules if _root(module) in BLENDER_API})
        loads = [line for module, line in modules if _root(module) == PACKAGE]
        by_path = _loads_component(tree)
        if by_path is not None:
            loads.append(by_path)
        if relative.startswith("houdini/"):
            skip = _docstrings(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                    if _WORD.search(node.value):
                        found.append(f"{relative}:{node.lineno}: names the Blender component in code")
        module_name = (
            ".".join(path.relative_to(root / "houdini/python3.13libs").with_suffix("").parts)
            if relative.startswith("houdini/python3.13libs/")
            else ""
        )
        module_name = module_name.removesuffix(".__init__")
        if module_name in SHARED:
            for module in sorted(_import_time_modules(tree)):
                if _root(module) in BLENDER_API | HOST_API:
                    found.append(f"{relative}: shared module imports {module} when it is imported")
        allowed = relative.startswith("tests/") or is_generator
        if uses_blender and not allowed:
            found.append(f"{relative}: imports {', '.join(uses_blender)} outside the Blender component")
        if loads and not allowed:
            found.append(f"{relative}:{loads[0]}: Apache-2.0 code loads the Blender component")
        if loads and allowed and "hou" in roots:
            found.append(f"{relative}:{loads[0]}: loads the Blender component together with hou")
    return found


def test_the_repository_keeps_the_boundary():
    missing = sorted(path for path in BLENDER_FIXTURE_GENERATORS if not (ROOT / path).is_file())
    assert missing == []
    assert violations(ROOT) == []


def test_every_component_file_is_gpl_and_the_folder_is_not_on_houdinis_path():
    files = sorted((ROOT / COMPONENT).rglob("*.py"))
    assert {p.name for p in files} >= {"worker_main.py", "asset_prefs_main.py", "export_main.py", "blend_export.py"}
    assert not (ROOT / "houdini/python3.13libs" / PACKAGE).exists()
    assert not (ROOT / "houdini/python3.13libs/cycles_houdini/blender/blend_export.py").exists()


def _plant(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_planted_breaches_are_reported(tmp_path):
    (tmp_path / COMPONENT).mkdir()
    (tmp_path / COMPONENT / "LICENSE").write_text("MIT License\n")
    _plant(tmp_path, "blender/cycles_blender/__init__.py", SPDX + "\n")
    _plant(tmp_path, "blender/cycles_blender/apache.py", "# SPDX-License-Identifier: Apache-2.0\nimport bpy\n")
    _plant(
        tmp_path,
        "blender/cycles_blender/hostly.py",
        SPDX + "\nimport hou\nfrom cycles_houdini.scene import native_scene\n",
    )
    _plant(tmp_path, "houdini/python3.13libs/cycles_houdini/client.py", "from cycles_blender import blend_export\n")
    _plant(tmp_path, "houdini/python3.13libs/cycles_houdini/lazy.py", "def f():\n    import bpy\n")
    _plant(
        tmp_path,
        "houdini/python3.13libs/cycles_houdini/dyn.py",
        "import importlib\nimportlib.import_module('cycles_blender.blend_export')\n",
    )
    _plant(tmp_path, "houdini/python3.13libs/cycles_houdini/path.py", "NAME = 'cycles_blender.blend_export'\n")
    _plant(tmp_path, "houdini/python3.13libs/cycles_houdini/blender/blend_names.py", "import bpy\n")
    _plant(
        tmp_path,
        "houdini/python3.13libs/cycles_houdini/fine.py",
        '"""Mentions cycles_blender in a docstring."""\nATTR = "cycles_blender_frame"\n',
    )
    _plant(tmp_path, "scripts/tool.py", "import bpy\n")
    _plant(tmp_path, "scripts/generate-blender-rgb-ramps.py", "# SPDX-License-Identifier: Apache-2.0\nimport bpy\n")
    _plant(
        tmp_path,
        "scripts/generate-blender-ramp-curve-gate.py",
        SPDX + "\nimport bpy\nfrom cycles_blender import blend_material_export\n",
    )
    _plant(tmp_path, "tests/houdini_mixed_smoke.py", "import hou\nfrom cycles_blender import blend_export\n")
    _plant(
        tmp_path,
        "tests/byhand_smoke.py",
        "import hou, importlib.util\nS = 'blender/cycles_blender/x.py'\nimportlib.util.spec_from_file_location('x', S)\n",
    )
    _plant(tmp_path, "tests/blender_ok_smoke.py", "import bpy\nfrom cycles_blender import blend_export\n")
    found = "\n".join(violations(tmp_path))
    for expected in (
        "blender/LICENSE is not the GNU GPL version 3 text",
        "blender/README.md is missing",
        "blender/cycles_blender/apache.py: first line is not",
        "blender/cycles_blender/hostly.py:2: the Blender component imports hou",
        "blender/cycles_blender/hostly.py:3: the Blender component imports cycles_houdini.scene, not a shared module",
        "cycles_houdini/client.py:1: Apache-2.0 code loads the Blender component",
        "cycles_houdini/lazy.py: imports bpy outside the Blender component",
        "cycles_houdini/dyn.py:2: Apache-2.0 code loads the Blender component",
        "cycles_houdini/path.py:1: names the Blender component in code",
        "cycles_houdini/blender/blend_names.py: shared module imports bpy when it is imported",
        "scripts/tool.py: imports bpy outside the Blender component",
        "scripts/generate-blender-rgb-ramps.py: first line is not " + SPDX,
        "tests/houdini_mixed_smoke.py:2: loads the Blender component together with hou",
        "tests/byhand_smoke.py:3: loads the Blender component together with hou",
    ):
        assert expected in found, expected
    assert "fine.py" not in found and "blender_ok_smoke.py" not in found
    assert "generate-blender-ramp-curve-gate.py" not in found
