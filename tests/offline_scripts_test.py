# SPDX-License-Identifier: Apache-2.0
"""Saved callback/menu guards stay usable with the entire integration absent."""

import builtins
import contextlib
import io
from pathlib import Path
import runpy
import sys
import textwrap
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = runpy.run_path(str(ROOT / "scripts/offline-scripts.py"))
GUARD = SCRIPTS["guard"]


class OfflineScriptsTest(unittest.TestCase):
    def setUp(self):
        self.callbacks = []
        self.hou = types.SimpleNamespace(
            session=types.SimpleNamespace(),
            hipFile=types.SimpleNamespace(addEventCallback=self.callbacks.append),
            hipFileEventType=types.SimpleNamespace(BeforeLoad="load", AfterClear="clear"),
        )
        self.stdout = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(patch.dict(sys.modules, hou=self.hou))

    def importer(self, error):
        original = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("cycles_houdini"):
                raise error
            return original(name, *args, **kwargs)

        return patch("builtins.__import__", side_effect=fake_import)

    def menu(self, code):
        namespace = {}
        exec("def menu(kwargs):\n" + textwrap.indent(GUARD(code, menu=True), "    "), namespace)
        return namespace["menu"]

    def test_menu_preserves_saved_token_without_evaluating_or_setting(self):
        parm = Mock()
        parm.rawValue.return_value = "saved_material"
        menu = self.menu("from cycles_houdini import materials\nreturn materials.menu()")
        with self.importer(ModuleNotFoundError("missing integration", name="cycles_houdini")):
            for _ in range(50):
                self.assertEqual(menu(dict(parm=parm)), ["saved_material", "saved_material"])
        parm.eval.assert_not_called()
        parm.evalAsString.assert_not_called()
        parm.set.assert_not_called()
        self.assertEqual(self.stdout.getvalue().count("Cycles renderer unavailable:"), 1)
        self.assertEqual(len(self.callbacks), 1)
        self.callbacks[0]("load")
        self.assertFalse(self.hou.session._cycles_missing_notice)

    def test_menu_genuine_dependency_and_initialization_failures_are_visible(self):
        menu = self.menu("from cycles_houdini import materials\nreturn materials.menu()")
        for error in (
            ModuleNotFoundError("dependency missing", name="numpy"),
            ModuleNotFoundError("own broken module", name="cycles_houdini.materials"),
            RuntimeError("native initialization failed"),
        ):
            with self.importer(error), self.assertRaises(type(error)):
                menu(dict(parm=Mock()))
        self.assertEqual(self.stdout.getvalue(), "")

    def test_saved_onloaded_does_not_require_startup_module(self):
        code = (ROOT / "houdini/hda_source/cycles_ipr/Driver_1cycles__ipr/OnLoaded").read_text()
        with self.importer(ModuleNotFoundError("absent", name="cycles_houdini_startup")):
            for _ in range(20):
                exec(code, dict(kwargs=dict(node=Mock())))
        self.assertEqual(self.stdout.getvalue().count("Cycles renderer unavailable:"), 1)
        self.assertEqual(len(self.callbacks), 1)

    def test_saved_onloaded_does_not_hide_broken_dependencies(self):
        code = (ROOT / "houdini/hda_source/cycles_ipr/Driver_1cycles__ipr/OnLoaded").read_text()
        with self.importer(ModuleNotFoundError("bad import", name="cycles_houdini.bad_internal")):
            with self.assertRaises(ModuleNotFoundError):
                exec(code, dict(kwargs=dict(node=Mock())))

    def test_healthy_builtin_expression_menu_is_byte_exact(self):
        code = "__import__('toolutils').parseDialogScriptMenu('FBres', forhscript=False)"
        parm = types.SimpleNamespace(
            name=lambda: "res_menu", itemGeneratorScript=lambda: code, setItemGeneratorScript=Mock()
        )
        group = types.SimpleNamespace(entries=lambda: (parm,), replace=Mock())
        SCRIPTS["guard_menus"](group)
        parm.setItemGeneratorScript.assert_not_called()
        group.replace.assert_called_once_with("res_menu", parm)


if __name__ == "__main__":
    unittest.main(verbosity=2)
