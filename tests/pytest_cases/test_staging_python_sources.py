# SPDX-License-Identifier: Apache-2.0
"""Source-owned staging helpers must load implementations, not legacy shims."""

import ast
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

from support.paths import PACKAGE, ROOT
from support.workers import ordinary_python, pure_python_environment

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_maintained_file_loaders_do_not_target_flat_forwarders():
    aliases = json.loads((PACKAGE / "_module_aliases.json").read_text(encoding="utf-8"))
    flat_paths = {"cycles_houdini/" + name + ".py" for name in aliases.values() if name}
    for directory in ("scripts", "examples", "tests", "houdini", "src"):
        for path in (ROOT / directory).rglob("*.py"):
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(call, ast.Call):
                    continue
                name = call.func.attr if isinstance(call.func, ast.Attribute) else ""
                if name not in ("run_path", "spec_from_file_location", "SourceFileLoader"):
                    continue
                for node in ast.walk(call):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        value = node.value.replace("\\", "/")
                        assert not any(value.endswith(flat) for flat in flat_paths), (path, call.lineno, value)


def test_finalizer_loads_the_actual_rop_help_dictionary():
    path = ROOT / "scripts/finalize-assets.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "help_tools" for target in node.targets)
    )
    scope = dict(__file__=str(path), Path=Path, runpy=runpy)
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(path), "exec"), scope)
    help_tools = scope["help_tools"]
    assert Path(help_tools["__file__"]) == PACKAGE / "rop/rop_help.py"
    assert all(callable(help_tools[name]) for name in ("about_templates", "apply_tooltips", "help_page"))


def test_sdk_tools_load_the_source_owned_pure_helper(tmp_path):
    namespace = runpy.run_path(str(ROOT / "scripts/sdk-dependencies.py"))
    assert Path(namespace["sdk"].__file__) == PACKAGE / "core/sdk_dependencies.py"
    assert callable(namespace["sdk"].inspect_sdk)
    release = runpy.run_path(str(ROOT / "scripts/release-package.py"))
    helper = tmp_path / "houdini/python3.13libs/cycles_houdini/core/sdk_dependencies.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("def inspect_sdk(hfs, policy): return dict(source='canonical', policy=policy)\n")
    assert release["sdk_dependency_record"](tmp_path, tmp_path / "sdk", {"test": True}) == dict(
        source="canonical", policy={"test": True}
    )


def test_help_generator_loads_the_actual_shader_help(monkeypatch):
    monkeypatch.setitem(sys.modules, "hou", ModuleType("hou"))
    help_package = ModuleType("houdinihelp")
    help_package.api = ModuleType("houdinihelp.api")
    monkeypatch.setitem(sys.modules, "houdinihelp", help_package)
    namespace = runpy.run_path(str(ROOT / "scripts/generate-node-help.py"))
    assert Path(namespace["help_data"].__file__) == PACKAGE / "materials/shader_help.py"
    assert callable(namespace["help_data"].visible_parameters)


def test_standalone_installer_runs_outside_checkout(tmp_path):
    fixture = runpy.run_path(str(ROOT / "tests/release_package_test.py"))
    archive = tmp_path / "fixture.zip"
    manifest = fixture["fixture_archive"](archive)
    helper = tmp_path / "tools/release-package.py"
    helper.parent.mkdir()
    shutil.copyfile(ROOT / "scripts/release-package.py", helper)

    def run(*arguments):
        result = subprocess.run(
            [str(ordinary_python()), "-I", str(helper), *map(str, arguments)],
            env=pure_python_environment(),
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert run("verify", archive)["package_id"] == manifest["package_id"]
    assert run("identity", "--houdini", "22.0.429")["houdini"] == "22.0.429"
    run(
        "install",
        archive,
        "--install-root",
        tmp_path / "install",
        "--preference-root",
        tmp_path / "prefs",
        "--data-root",
        tmp_path / "data",
        "--allow-internal",
        "--isolated",
    )
    assert run("status", "--install-root", tmp_path / "install")["state"]["active"] == manifest["package_id"]
    run("uninstall", "--install-root", tmp_path / "install")
