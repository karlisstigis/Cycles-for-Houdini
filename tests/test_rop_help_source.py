# SPDX-License-Identifier: Apache-2.0
"""Source-level coverage and installation-link checks without a Houdini license."""

from support.paths import artifacts_root

import ast
import json
import os
from pathlib import Path
import re
import runpy
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELP = runpy.run_path(str(ROOT / "houdini/python3.13libs/cycles_houdini/rop/rop_help.py"))


class RopHelpSourceTest(unittest.TestCase):
    def test_visible_control_coverage(self):
        source = ROOT / "houdini/hda_source/cycles_ipr/Driver_1cycles__ipr/DialogScript"
        available = set(re.findall(r'parm \{ name "([^"]+)"[^\n]*?help "', source.read_text()))
        for filename in ("finalize-assets.py", "pass-schema.py"):
            tree = ast.parse((ROOT / "scripts" / filename).read_text())
            for node in ast.walk(tree):
                # Action buttons use (name, label, action, help_text) rows.
                # Audit their authored help just like literal constructors.
                if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
                    fields = [entry.id for entry in node.target.elts if isinstance(entry, ast.Name)]
                    if "help_text" in fields and isinstance(node.iter, (ast.List, ast.Tuple)):
                        for row in node.iter.elts:
                            values = ast.literal_eval(row)
                            self.assertGreaterEqual(len(values[fields.index("help_text")].split()), 5)
                            available.add(values[fields.index("name")])
                if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                    if any(keyword.arg == "help" for keyword in node.keywords):
                        available.add(str(node.args[0].value))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "setHelp":
                    if isinstance(node.func.value, ast.Subscript) and isinstance(node.func.value.slice, ast.Constant):
                        available.add(node.func.value.slice.value)
        # The dicing constructors use a compact (name, label, default) loop.
        available.update(("dicing_rate", "ipr_dicing_rate"))
        available.update(HELP["TOOLTIPS"])

        def leaves(entries):
            for entry in entries:
                if "children" in entry:
                    yield from leaves(entry["children"])
                elif not entry.get("separator") and not entry["name"].startswith("label_"):
                    yield entry["name"]

        missing = []
        for name in leaves(json.loads((ROOT / "scripts/rop-layout.json").read_text())):
            if name not in available and not name.startswith("world_visibility_"):
                if not (name.startswith("ipr_") and name[4:] in available):
                    missing.append(name)
        self.assertEqual(missing, [])
        self.assertTrue(all(len(text.split()) >= 5 for text in HELP["TOOLTIPS"].values()))

    def test_local_links_prefer_installed_version(self):
        temporary_root = artifacts_root() / "rop-help-source-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            root = Path(directory)
            host = root / "version/host"
            source = root / "source"
            host.mkdir(parents=True)
            source.mkdir()
            for base, relative in (
                (host.parent, "LICENSE"),
                (source, "LICENSE"),
                (host.parent, "THIRD_PARTY_NOTICES.md"),
                (host.parent, "notices/dlss-headers/LICENSE.txt"),
                (source, "THIRD_PARTY_NOTICES.md"),
                (source, "packaging/notices/dlss-headers/LICENSE.txt"),
            ):
                path = base / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test notice")
            with patch.dict(
                os.environ, {"CYCLES_HOUDINI_HOST_ROOT": str(host), "CYCLES_HOUDINI_SOURCE_ROOT": str(source)}
            ):
                self.assertEqual(HELP["link_target"]("notices"), (host.parent / "THIRD_PARTY_NOTICES.md").as_uri())
                self.assertEqual(HELP["link_target"]("integration"), (host.parent / "LICENSE").as_uri())
                installed = host.parent / "notices/dlss-headers/LICENSE.txt"
                self.assertEqual(HELP["link_target"]("nvidia"), installed.as_uri())
                installed.unlink()
                self.assertEqual(
                    HELP["link_target"]("nvidia"), (source / "packaging/notices/dlss-headers/LICENSE.txt").as_uri()
                )
                (host.parent / "THIRD_PARTY_NOTICES.md").unlink()
                self.assertEqual(HELP["link_target"]("notices"), (source / "THIRD_PARTY_NOTICES.md").as_uri())
                self.assertEqual(HELP["link_target"]("cycles"), HELP["CYCLES_URL"])


if __name__ == "__main__":
    unittest.main()
