# SPDX-License-Identifier: Apache-2.0
"""Known SDK failures remain explicit without multiplying startup callbacks."""

from support.paths import artifacts_root
from support.package_loader import prepare_package
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class SDKDependencyError(RuntimeError):
    pass


class Node:
    def __init__(self):
        self.cache = {}

    def setCachedUserData(self, name, value):
        self.cache[name] = value

    def destroyCachedUserData(self, name, must_exist=True):
        self.cache.pop(name, None)

    def parm(self, name):
        return None


class StartupTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "test_cycles_startup", ROOT / "houdini/python3.13libs/cycles_houdini_startup.py"
        )
        self.startup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.startup)
        self.hou = types.SimpleNamespace(
            isUIAvailable=lambda: True,
            ui=types.SimpleNamespace(setStatusMessage=Mock()),
            NotAvailable=type("NotAvailable", (RuntimeError,), {}),
            severityType=types.SimpleNamespace(Error="error"),
        )
        self.modules = patch.dict(
            sys.modules,
            {
                "hou": self.hou,
                "cycles_houdini.core.sdk_dependencies": types.SimpleNamespace(SDKDependencyError=SDKDependencyError),
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.stderr = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.stderr)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_many_scene_nodes_and_ui_report_one_failure(self):
        action = Mock(side_effect=SDKDependencyError("wrong loaded OIDN provider"))
        nodes = [Node() for _ in range(50)]
        for node in nodes:
            self.assertFalse(self.startup._run(action, node))
        self.assertFalse(self.startup._run(action))
        action.assert_called_once()
        self.hou.ui.setStatusMessage.assert_called_once()
        self.assertEqual(self.stderr.getvalue().count("Cycles renderer unavailable:"), 1)
        self.assertTrue(all(n.cache["cycles_startup_error"] == self.startup.startup_error() for n in nodes))

    def test_programming_errors_remain_explicit(self):
        for error in (TypeError("bad internal call"), RuntimeError("unexpected failure")):
            with self.assertRaises(type(error)):
                self.startup._run(Mock(side_effect=error), Node())
        self.assertIsNone(self.startup.startup_error())
        self.hou.ui.setStatusMessage.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), "")

    def test_canonical_dependency_type_wins_over_a_stale_legacy_module(self):
        legacy_error = type("LegacySDKDependencyError", (RuntimeError,), {})
        with patch.dict(
            sys.modules,
            {"cycles_houdini.sdk_dependencies": types.SimpleNamespace(SDKDependencyError=legacy_error)},
        ):
            with self.assertRaises(legacy_error):
                self.startup._run(Mock(side_effect=legacy_error("unrelated legacy class")))
            self.assertFalse(self.startup._run(Mock(side_effect=SDKDependencyError("canonical SDK failure"))))
        self.assertEqual(self.startup.startup_error(), "canonical SDK failure")

    def pass_menu(self, native):
        package = types.ModuleType("cycles_houdini")
        package._native = native
        modules = patch.dict(
            sys.modules,
            {
                "cycles_houdini": package,
                "cycles_houdini_startup": self.startup,
                "cycles_houdini.rop.rop_controls": types.SimpleNamespace(value=Mock()),
            },
        )
        modules.start()
        self.addCleanup(modules.stop)
        prepare_package(package)
        spec = importlib.util.spec_from_file_location(
            "cycles_houdini.rop.passes", ROOT / "houdini/python3.13libs/cycles_houdini/rop/passes.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_pass_menus_share_startup_latch_but_explicit_catalogue_still_rejects(self):
        native = types.SimpleNamespace(pass_catalogue=Mock(side_effect=SDKDependencyError("SDK missing")))
        passes = self.pass_menu(native)
        for _ in range(50):
            self.assertEqual(passes.menu(), [])
        native.pass_catalogue.assert_called_once_with()
        self.assertEqual(self.stderr.getvalue().count("Cycles renderer unavailable:"), 1)
        with self.assertRaises(SDKDependencyError):
            passes.catalogue()
        self.assertEqual(native.pass_catalogue.call_count, 2)

    def test_healthy_pass_menu_keeps_catalogue_and_custom_choices(self):
        native = types.SimpleNamespace(pass_catalogue=Mock(return_value=[dict(id="combined"), dict(id="normal")]))
        passes = self.pass_menu(native)
        choices = dict(zip(passes.menu()[::2], passes.menu()[1::2]))
        self.assertEqual(choices["normal"], "Data / Normal")
        self.assertEqual(choices["aov_color"], "Custom / Color AOV")
        self.assertNotIn("combined", choices)
        self.assertIsNone(self.startup.startup_error())

    def test_pass_menu_programming_errors_remain_explicit(self):
        passes = self.pass_menu(types.SimpleNamespace(pass_catalogue=Mock(side_effect=TypeError("broken catalogue"))))
        with self.assertRaises(TypeError):
            passes.menu()
        self.assertIsNone(self.startup.startup_error())

    def test_headless_failure_logs_once_without_ui(self):
        self.hou.isUIAvailable = lambda: False
        action = Mock(side_effect=SDKDependencyError("missing provider metadata"))
        self.assertFalse(self.startup._run(action))
        self.assertFalse(self.startup._run(action))
        action.assert_called_once()
        self.hou.ui.setStatusMessage.assert_not_called()
        self.assertEqual(self.stderr.getvalue().count("missing provider metadata"), 1)

    def test_success_preserves_node_initialization(self):
        final, devices = types.SimpleNamespace(register=Mock()), types.SimpleNamespace(initialize=Mock())
        properties = types.SimpleNamespace(install_scene_refresh=Mock())
        package = types.ModuleType("cycles_houdini")
        package.final_render, package.device_selection, package.object_properties = final, devices, properties
        node = Node()
        node.cache["cycles_startup_error"] = "old transient error"
        with patch.dict(
            sys.modules,
            {
                "cycles_houdini": package,
                "cycles_houdini.output": types.SimpleNamespace(ensure_viewer_resolution_precision=Mock()),
                "cycles_houdini.rop_layout": types.SimpleNamespace(ensure_ipr_settings_layout=Mock()),
            },
        ):
            prepare_package(package)
            self.assertTrue(self.startup.initialize_rop(node, created=True))
        final.register.assert_called_once_with(node)
        devices.initialize.assert_called_once_with(node, created=True)
        self.assertNotIn("cycles_startup_error", node.cache)

    def test_loading_before_status_bar_retains_failure_without_dialog(self):
        self.hou.ui.setStatusMessage.side_effect = self.hou.NotAvailable("UI is loading")
        action = Mock(side_effect=SDKDependencyError("wrong provider"))
        self.assertFalse(self.startup._run(action, Node()))
        self.assertFalse(self.startup._run(action, Node()))
        action.assert_called_once()
        self.assertEqual(self.startup.startup_error(), "wrong provider")

    def test_ui_installs_both_tools(self):
        preview, materials = Mock(), Mock()
        with patch.dict(
            sys.modules,
            {
                "cycles_houdini.network_preview": types.SimpleNamespace(install=preview),
                "cycles_houdini.material_previews": types.SimpleNamespace(install=materials),
            },
        ):
            prepare_package(types.ModuleType("cycles_houdini"))
            self.assertTrue(self.startup.install_ui())
        preview.assert_called_once_with()
        materials.assert_called_once_with()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StartupTest))
    path = artifacts_root() / "startup-dependency-guard-unit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            dict(
                result="passed" if result.wasSuccessful() else "failed",
                tests=result.testsRun,
                failures=len(result.failures),
                errors=len(result.errors),
            ),
            indent=2,
        )
        + "\n"
    )
    raise SystemExit(not result.wasSuccessful())
