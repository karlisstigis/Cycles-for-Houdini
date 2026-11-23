# SPDX-License-Identifier: Apache-2.0
"""Normal import, alias identity, saved-path and optional runtime contracts."""

import importlib
import ast
import json
from pathlib import Path
import pickle
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from support.paths import BLENDER, PACKAGE
from support.package_loader import load_module
from support.workers import ordinary_python, pure_python_environment

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def package_environment():
    # SDK Qt lives beside its ordinary interpreter, without Houdini startup.
    paths = [PACKAGE.parent]
    forced = ordinary_python().parent / "lib/site-packages-forced"
    if forced.is_dir():
        paths.append(forced)
    return pure_python_environment(paths)


def test_pure_modules_import_without_houdini_or_environment():
    names = [
        "core.execution",
        "core.sdk_dependencies",
        "ipr.queued",
        "ipr.worker_liveness",
        "ipr.display_queue",
        "viewer.viewer_timing",
        "materials.override_expressions",
        "materials.shader_curves",
        "blender.blend_cache",
        "blender.blend_preview",
        "rop.background_jobs",
        "rop.rop_layout",
    ]
    code = (
        'import importlib,sys; assert "hou" not in sys.modules; import cycles_houdini as ch; '
        + "; ".join(f'importlib.import_module("cycles_houdini.{name}")' for name in names)
        + '; assert ch._native._module is None; assert "hou" not in sys.modules; '
        + 'assert "_cycles_houdini" not in sys.modules'
    )
    env = pure_python_environment([PACKAGE.parent])
    subprocess.run([str(ordinary_python()), "-c", code], env=env, check=True, timeout=30)


@pytest.mark.parametrize("canonical,legacy", json.loads((PACKAGE / "_module_aliases.json").read_text()).items())
def test_alias_table_has_one_implementation(canonical, legacy):
    if canonical in ("core.api", "materials.graph"):
        return
    source = PACKAGE.joinpath(*canonical.split(".")).with_suffix(".py")
    assert source.is_file()
    shim = PACKAGE / (legacy + ".py")
    assert shim.is_file()
    assert "exec(compile" not in source.read_text(encoding="utf-8")
    assert "import_module" in shim.read_text(encoding="utf-8")


def test_package_contains_no_source_execution_or_metadata_rewrites():
    assert not (PACKAGE / "_compat.py").exists()
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "exec(compile" not in source, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                assert not any(
                    isinstance(target, ast.Name) and target.id in ("__file__", "__package__") for target in targets
                ), path


@pytest.mark.parametrize("first", ["legacy", "canonical"])
def test_every_alias_keeps_module_state_and_saved_callback_identity(first):
    code = r"""
import importlib, inspect, json, os, pickle, sys, types
from pathlib import Path
root = Path(PACKAGE_PATH)
sdk_bin = Path(sys.executable).resolve().parent.parent / 'bin'
if os.name == 'nt' and (sdk_bin / 'Qt6Core.dll').is_file():
    sdk_dlls = os.add_dll_directory(str(sdk_bin))
for name in ('hou', 'soho', 'sohog'):
    sys.modules[name] = types.ModuleType(name)
events = ('ParmTupleChanged', 'InputDataChanged', 'ParmTupleAnimated', 'ParmTupleChannelChanged',
          'InputRewired', 'NameChanged', 'BeingDeleted', 'FlagChanged', 'SpareParmTemplatesChanged',
          'ChildCreated', 'ChildDeleted')
sys.modules['hou'].ParmTuple = types.SimpleNamespace(eval=lambda self: ())
sys.modules['hou'].nodeEventType = types.SimpleNamespace(**{name: name for name in events})
# viewer.light_viewer_state builds its unit axes at import time, as Houdini allows.
sys.modules['hou'].Vector3 = tuple
for canonical, legacy in json.loads((root / '_module_aliases.json').read_text()).items():
    names = ['cycles_houdini' + ('.' + legacy if legacy else ''), 'cycles_houdini.' + canonical]
    if FIRST == 'canonical': names.reverse()
    before, after = map(importlib.import_module, names)
    assert before is after, names
    before._migration_probe = object()
    assert after._migration_probe is before._migration_probe
    after._migration_probe = object()
    assert before._migration_probe is after._migration_probe
    expected = root / ('__init__.py' if canonical == 'core.api' else
                       'materials/__init__.py' if canonical == 'materials.graph' else
                       canonical.replace('.', '/') + '.py')
    assert Path(before.__file__).resolve() == expected.resolve(), (names, before.__file__)
    assert before.__spec__.origin == before.__file__
    assert before.__package__ == before.__spec__.parent
    for name, value in tuple(vars(before).items()):
        if (name.startswith('_') or not (inspect.isclass(value) or inspect.isfunction(value))
                or value.__module__ != before.__name__ or '<' in value.__qualname__):
            continue
        assert pickle.loads(pickle.dumps(value)) is value, (canonical, name)
        historical = ('c' + names[FIRST == 'canonical'] + '\n' + name + '\n.').encode()
        assert pickle.loads(historical) is value, (legacy, name)
from cycles_houdini.scene import tessellation, light_materials
from cycles_houdini.viewer import panel
from cycles_houdini.ipr import registry
calls = []
tessellation.refresh = lambda node: calls.append(('hip', node))
light_materials.action = lambda kwargs: calls.append(('material', kwargs['node']))
scope = dict(kwargs=dict(node='saved-node'))
exec("from cycles_houdini import tessellation; tessellation.refresh(kwargs['node'])", scope)
exec("from cycles_houdini.light_materials import action; action(kwargs)", scope)
exec("from cycles_houdini.panel import RenderPanel", scope)
assert calls == [('hip', 'saved-node'), ('material', 'saved-node')]
assert scope['RenderPanel'] is panel.RenderPanel
assert panel.registry is registry
assert registry._entries is importlib.import_module('cycles_houdini.registry')._entries
from cycles_houdini.viewer import viewer_icons
from cycles_houdini.rop import background_render, rop_help
host = root.parents[1]
assert viewer_icons.ASSET_ROOT == host / 'config/Icons/cycles_viewer/plain'
assert viewer_icons.ASSET_ROOT.is_dir()
assert Path(background_render.__file__).resolve().parents[3] == host
os.environ.pop('CYCLES_HOUDINI_HOST_ROOT', None)
os.environ['CYCLES_HOUDINI_SOURCE_ROOT'] = str(root.parents[2])
assert rop_help.link_target('integration') == (root.parents[2] / 'LICENSE').as_uri()
import cycles_houdini as ch
assert ch._native._module is None and '_cycles_houdini' not in sys.modules
""".replace("FIRST", repr(first)).replace("PACKAGE_PATH", repr(str(PACKAGE)))
    subprocess.run([str(ordinary_python()), "-c", code], env=package_environment(), check=True, timeout=30)


def test_isolated_loader_returns_canonical_patchable_pickle_identity(monkeypatch):
    # The fake host only supplies the API consumed by the actual controller.
    original = dict(sys.modules)
    package = ModuleType("isolated_package_contract")
    package.Session = object
    package._native = object()
    package.default_device = lambda: "CPU"
    try:
        module = load_module(package, "queued")
        assert module is importlib.import_module(package.__name__ + ".ipr.queued")
        assert module is importlib.import_module(package.__name__ + ".queued")
        assert Path(module.__file__) == PACKAGE / "ipr/queued.py"
        assert module.__package__ == package.__name__ + ".ipr"
        assert module._native is package._native
        monkeypatch.setattr(module, "_native", object())
        assert importlib.import_module(package.__name__ + ".queued")._native is module._native
        assert pickle.loads(pickle.dumps(module.WorkerSession)) is module.WorkerSession
    finally:
        for name in set(sys.modules) - set(original):
            if name.startswith(package.__name__):
                sys.modules.pop(name)


@pytest.mark.parametrize("script", ["worker_main.py", "export_main.py"])
def test_blender_script_entrypoint_bootstraps_without_host_environment(script):
    env = pure_python_environment()
    result = subprocess.run(
        [str(ordinary_python()), "-I", str(BLENDER / script), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("first", ["legacy", "canonical"])
def test_alias_identity_monkeypatch_and_pickle(first):
    code = r"""
import importlib, pickle
from pathlib import Path
PACKAGE = Path(PACKAGE_PATH)
names = ['cycles_houdini.blend_cache', 'cycles_houdini.blender.blend_cache']
if FIRST == 'canonical': names.reverse()
a,b = map(importlib.import_module, names)
assert a is b
assert Path(a.__file__).resolve() == PACKAGE / 'blender/blend_cache.py'
assert a.__package__ == 'cycles_houdini.blender'
a._compatibility_probe = object()
assert b._compatibility_probe is a._compatibility_probe
assert pickle.loads(pickle.dumps(a.ByteBudgetCache)) is b.ByteBudgetCache
assert pickle.loads(b'ccycles_houdini.blend_cache\nByteBudgetCache\n.') is b.ByteBudgetCache
a.ByteBudgetCache._probe = object()
assert b.ByteBudgetCache._probe is a.ByteBudgetCache._probe
import cycles_houdini.materials as materials
assert importlib.import_module('cycles_houdini.materials.graph') is materials
import cycles_houdini as ch
assert importlib.import_module('cycles_houdini.core.api') is ch
assert ch.__spec__.submodule_search_locations is not None
api = importlib.import_module('cycles_houdini.core.api')
original = ch._native
api._native = object()
assert ch._native is api._native
ch._native = original
assert api._native is original
assert Path(ch.__file__).resolve() == PACKAGE / '__init__.py'
assert Path(materials.__file__).resolve() == PACKAGE / 'materials/__init__.py'
""".replace("FIRST", repr(first)).replace("PACKAGE_PATH", repr(str(PACKAGE)))
    env = package_environment()
    subprocess.run([str(ordinary_python()), "-c", code], env=env, check=True, timeout=30)


def test_runtime_load_is_atomic_and_retryable():
    spec = importlib.util.spec_from_file_location("tested_native_runtime", PACKAGE / "core/native.py")
    runtime_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime_module)
    NativeRuntime = runtime_module.NativeRuntime
    calls = []
    module = SimpleNamespace(capabilities=lambda: {"image_iris": True})

    def loader():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("repair the host configuration")
        time.sleep(0.01)
        return module

    runtime = NativeRuntime(loader)
    with pytest.raises(RuntimeError, match="repair"):
        runtime.load()
    results = []
    threads = [threading.Thread(target=lambda: results.append(runtime.load())) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 2 and results == [module] * 12
    assert runtime.capabilities()["image_iris"] is True


@pytest.mark.parametrize("first", ["file", "package"])
def test_houdini_flat_material_loader_supports_canonical_children(first):
    code = """
import importlib.util, pickle, sys
from pathlib import Path
import cycles_houdini
if FIRST == 'package':
    previous = __import__('cycles_houdini.materials', fromlist=['materials'])
path=Path(cycles_houdini.__file__).with_name('materials.py')
spec=importlib.util.spec_from_file_location('cycles_houdini.materials',path)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
from cycles_houdini.materials.graph_values import constant_node
assert constant_node('value','float',3)[0]['parameters']=={'value':3.}
assert module.__path__==spec.submodule_search_locations
actual = __import__('cycles_houdini.materials', fromlist=['materials'])
assert actual is sys.modules[spec.name]
assert actual is __import__('cycles_houdini.materials.graph', fromlist=['graph'])
assert module.create_material is actual.create_material
assert pickle.loads(pickle.dumps(module.MaterialSourceError)) is actual.MaterialSourceError
actual._loader_probe = object()
assert module._loader_probe is actual._loader_probe
assert Path(actual.__file__) == path.with_name('materials') / '__init__.py'
assert actual.__package__ == 'cycles_houdini.materials'
if FIRST == 'package': assert actual is previous
""".replace("FIRST", repr(first))
    subprocess.run([str(ordinary_python()), "-c", code], env=package_environment(), check=True, timeout=30)
