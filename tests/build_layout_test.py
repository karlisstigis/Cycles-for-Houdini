# SPDX-License-Identifier: Apache-2.0
"""Build defaults agree across CMake, PowerShell and Python staging; the one-folder layout and its link safety."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from support.workers import ordinary_python

ROOT = Path(__file__).resolve().parents[1]
# hython's embedded interpreter needs Houdini bootstrap state and cannot start
# with -I. Use the matching SDK's ordinary Python for isolated subprocesses.
ISOLATED_PYTHON = ordinary_python()
sys.path.insert(0, str(ROOT / "scripts"))
from build_layout import default_build_directory, earlier_build_directory, validate_build_source  # noqa: E402
from project_layout import project_root, work_path  # noqa: E402

PWSH = shutil.which("pwsh")
WINDOWS = os.name == "nt"


def run_git(repository, *args):
    return subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def powershell(script, **env):
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
        env=dict(os.environ, **env),
        capture_output=True,
        text=True,
        timeout=120,
    )


class BuildLayoutContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "production source"
        self.source.mkdir()
        shutil.copyfile(ROOT / "CMakePresets.json", self.source / "CMakePresets.json")

    def cache(self, build, source=None, cached_build=None):
        build.mkdir(parents=True, exist_ok=True)
        (build / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={source or self.source}\n"
            f"CMAKE_CACHEFILE_DIR:INTERNAL={cached_build or build}\n",
            encoding="utf-8",
        )

    def test_variants_versions_and_separate_checkouts(self):
        for preset, suffix in (("normal", "release"), ("normal-no-iris", "no-iris"), ("solaris", "solaris")):
            self.assertEqual(
                default_build_directory(self.source, "22.0.466", preset),
                self.source / "_work" / "dev" / f"windows-h22.0.466-{suffix}",
            )
        other = self.base / "private checkout"
        other.mkdir()
        shutil.copyfile(self.source / "CMakePresets.json", other / "CMakePresets.json")
        self.assertNotEqual(default_build_directory(self.source), default_build_directory(other))
        self.assertEqual(default_build_directory(other, "22.0.999").name, "windows-h22.0.999-release")

    def test_build_cache_is_inside_the_project_and_never_the_one_file_builders_folder(self):
        presets = json.loads((self.source / "CMakePresets.json").read_text(encoding="utf-8-sig"))
        for preset in presets["configurePresets"]:
            for value in [preset.get("binaryDir", "")] + list(preset.get("cacheVariables", {}).values()):
                self.assertNotIn("${sourceParentDir}", str(value), preset["name"])
        build = default_build_directory(self.source, "22.0.508")
        self.assertTrue(build.is_relative_to(self.source / "_work"))
        # The one-file builder (scripts/easy-common.ps1) uses <project>/_work/windows-h<build>-release.
        self.assertNotEqual(build, self.source / "_work" / "windows-h22.0.508-release")
        self.assertNotIn(build.parent.name, ("tests", "installer-details", "_work"))

    def test_earlier_sibling_cache_is_only_found(self):
        build = default_build_directory(self.source)
        self.assertIsNone(earlier_build_directory(self.source, build))
        old = self.base / "production source-build" / build.name
        (old / "hosts").mkdir(parents=True)
        self.assertEqual(earlier_build_directory(self.source, build), old)
        self.assertTrue((old / "hosts").is_dir())

    @unittest.skipUnless(PWSH, "PowerShell 7 is unavailable")
    def test_powershell_names_an_earlier_cache_in_one_sentence(self):
        old = self.base / "production source-build" / "windows-h22.0.466-release"
        script = (
            ". $env:CH_LAYOUT_HELPER; "
            "$b=Get-DefaultBuildDirectory -SourceRoot $env:CH_LAYOUT_SOURCE -HoudiniVersion 22.0.466; "
            "$n=Get-EarlierBuildNotice -SourceRoot $env:CH_LAYOUT_SOURCE -BuildDirectory $b; "
            "if($n){$n}else{'none'}"
        )
        env = dict(CH_LAYOUT_HELPER=str(ROOT / "scripts/build-layout.ps1"), CH_LAYOUT_SOURCE=str(self.source))
        self.assertEqual(powershell(script, **env).stdout.strip(), "none")
        old.mkdir(parents=True)
        notice = powershell(script, **env).stdout.strip()
        self.assertEqual(notice.count("\n"), 0)
        self.assertIn(str(old), notice)
        self.assertIn("no longer used", notice)
        self.assertTrue(old.is_dir())

    def test_project_root_of_checkout_worktree_and_made_up_trees(self):
        main = self.base / "project"
        main.mkdir()
        run_git(main, "init", "-q")
        (main / ".gitignore").write_text("/_work/\n")
        run_git(main, "add", ".gitignore")
        run_git(main, "commit", "-q", "-m", "root")
        reviewed = main / "_work" / "reviewed"
        run_git(main, "worktree", "add", "-q", "--detach", str(reviewed))
        self.assertEqual(project_root(main), main.resolve())
        self.assertEqual(project_root(reviewed), main.resolve())
        self.assertEqual(work_path(reviewed, "releases"), main.resolve() / "_work" / "releases")
        # A made-up tree inside another checkout's folder is its own project; so is a folder without Git.
        made_up = main / "_work" / "test-artifacts" / "fake-repo"
        made_up.mkdir(parents=True)
        self.assertEqual(project_root(made_up), made_up.resolve())
        self.assertEqual(project_root(self.source), self.source.resolve())
        # The worktree stays invisible to the main checkout's status.
        (reviewed / "new.txt").write_text("x")
        self.assertEqual(run_git(main, "status", "--porcelain", "--untracked-files=all"), "")
        if PWSH:
            script = (
                ". $env:CH_PROJECT_LAYOUT; Get-ProjectRoot -SourceRoot $env:CH_A; Get-ProjectRoot -SourceRoot $env:CH_B; "
                "Get-ProjectRoot -SourceRoot $env:CH_C; Test-InProjectWork -Path (Join-Path $env:CH_A '_work/ci') -SourceRoot $env:CH_B"
            )
            result = powershell(
                script,
                CH_PROJECT_LAYOUT=str(ROOT / "scripts/project-layout.ps1"),
                CH_A=str(main),
                CH_B=str(reviewed),
                CH_C=str(made_up),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stdout.splitlines()
            self.assertEqual(
                [Path(x).resolve() for x in lines[:3]], [main.resolve(), main.resolve(), made_up.resolve()]
            )
            self.assertEqual(lines[3], "True")

    @unittest.skipUnless(WINDOWS and PWSH, "directory junctions need Windows and PowerShell 7")
    def test_link_safe_removal_keeps_the_junction_target(self):
        canary = self.base / "canary"
        canary.mkdir()
        (canary / "keep.txt").write_text("must stay")
        tree = self.base / "tree"
        (tree / "deep/inside").mkdir(parents=True)
        (tree / "deep/inside/file.txt").write_text("x")
        script = (
            ". $env:CH_PROJECT_LAYOUT; "
            "New-Item -ItemType Junction -Path (Join-Path $env:CH_TREE 'deep/link') -Target $env:CH_CANARY | Out-Null; "
            "New-Item -ItemType Junction -Path (Join-Path $env:CH_TREE 'top-link') -Target $env:CH_CANARY | Out-Null; "
            "@(Get-ProjectLinks -Root $env:CH_TREE).Count; Remove-ProjectTree -Root $env:CH_TREE; Test-Path -LiteralPath $env:CH_TREE"
        )
        result = powershell(
            script, CH_PROJECT_LAYOUT=str(ROOT / "scripts/project-layout.ps1"), CH_TREE=str(tree), CH_CANARY=str(canary)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["2", "False"])
        self.assertEqual((canary / "keep.txt").read_text(), "must stay")

    @unittest.skipUnless(WINDOWS and PWSH, "directory junctions need Windows and PowerShell 7")
    @unittest.skipUnless(
        (ROOT / "scripts/dev-worktree.ps1").is_file(), "the maintainer's worktree tool is not published"
    )
    def test_dev_worktree_remove_strips_links_before_git_and_keeps_canaries(self):
        main = self.base / "main"
        (main / "scripts").mkdir(parents=True)
        for name in ("dev-worktree.ps1", "project-layout.ps1"):
            shutil.copyfile(ROOT / "scripts" / name, main / "scripts" / name)
        (main / ".gitignore").write_text("/_work/\n/third_party/\n")
        engine = main / "third_party/cycles"
        (engine / "lib/windows_x64").mkdir(parents=True)
        (engine / "lib/windows_x64/openpgl.lib").write_text("library canary")
        (engine / "src.txt").write_text("engine")
        (engine / ".gitignore").write_text("/lib/\n")
        run_git(engine, "init", "-q")
        run_git(engine, "add", "src.txt", ".gitignore")
        run_git(engine, "commit", "-q", "-m", "engine")
        tip = run_git(engine, "rev-parse", "HEAD")
        optix = main / "third_party/optix"
        optix.mkdir()
        (optix / "optix.h").write_text("header canary")
        (main / "source.lock.json").write_text(
            json.dumps({"cycles": {"authoring_tip": tip}, "certification_candidate": {"houdini": "22.0.466"}})
        )
        run_git(main, "init", "-q")
        run_git(main, "add", ".")
        run_git(main, "commit", "-q", "-m", "main")
        root = main / "_work/wt/t1"
        source = root / "s"
        run_git(main, "worktree", "add", "-q", "--detach", str(source))
        run_git(engine, "worktree", "add", "-q", "--detach", str(source / "third_party/cycles"), tip)
        (root / "b").mkdir()
        (root / "b/CMakeCache.txt").write_text("cache")
        script = (
            "New-Item -ItemType Junction -Path $env:CH_L1 -Target $env:CH_T1 | Out-Null; "
            "New-Item -ItemType Junction -Path $env:CH_L2 -Target $env:CH_T2 | Out-Null; "
            "(Get-Item -LiteralPath $env:CH_L1).Attributes"
        )
        links = dict(
            CH_L1=str(source / "third_party/cycles/lib/windows_x64"),
            CH_T1=str(engine / "lib/windows_x64"),
            CH_L2=str(source / "third_party/optix"),
            CH_T2=str(optix),
        )
        (source / "third_party/cycles/lib").mkdir(parents=True)
        self.assertEqual(powershell(script, **links).returncode, 0)
        command = [
            PWSH,
            "-NoProfile",
            "-File",
            str(main / "scripts/dev-worktree.ps1"),
            "-Name",
            "t1",
            "-Action",
            "remove",
        ]
        (source / "dirty.txt").write_text("unsaved")
        refused = subprocess.run(command, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("has changes", refused.stdout + refused.stderr)
        self.assertTrue(source.is_dir())
        result = subprocess.run(command + ["-Discard"], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(root.exists())
        self.assertEqual((engine / "lib/windows_x64/openpgl.lib").read_text(), "library canary")
        self.assertEqual((optix / "optix.h").read_text(), "header canary")
        self.assertEqual((engine / "src.txt").read_text(), "engine")
        self.assertEqual(len(run_git(main, "worktree", "list").splitlines()), 1)
        self.assertEqual(len(run_git(engine, "worktree", "list").splitlines()), 1)
        self.assertTrue(run_git(main, "rev-parse", "refs/archive/wt/t1"))

    def test_explicit_external_and_legacy_cache_ownership(self):
        for build in (
            default_build_directory(self.source),
            self.source / "build/legacy-cache",
            self.base / "ci",
        ):
            self.cache(build)
            self.assertEqual(validate_build_source(self.source, build), build.resolve())

    def test_wrong_source_and_relocated_cache_rejected(self):
        build = default_build_directory(self.source)
        self.cache(build, source=self.base / "another-checkout")
        with self.assertRaisesRegex(RuntimeError, "different source"):
            validate_build_source(self.source, build)
        self.cache(build, cached_build=self.base / "original-location")
        with self.assertRaisesRegex(RuntimeError, "relocated"):
            validate_build_source(self.source, build)
        with self.assertRaisesRegex(RuntimeError, "contain the source"):
            validate_build_source(self.source, self.base)

    def test_staging_cli_accepts_external_and_legacy_and_contains_host(self):
        for build in (default_build_directory(self.source), self.source / "build/legacy-cache"):
            self.cache(build)
            host = build / "hosts/test"
            command = [
                str(ISOLATED_PYTHON),
                "-I",
                str(ROOT / "scripts/runtime-assets.py"),
                "frozen-json",
                "--source-root",
                str(self.source),
                "--build-directory",
                str(build),
                "--host-root",
                str(host),
                "--path",
                "metadata.json",
                "--allow-create",
            ]
            result = subprocess.run(command, input='{"fixture":true}', text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((host / "metadata.json").read_text()), dict(fixture=True))
            command[command.index("--host-root") + 1] = str(self.base / "unowned-host")
            result = subprocess.run(command, input="{}", text=True, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Native host root must stay inside", result.stderr)
            self.assertFalse((self.base / "unowned-host").exists())

    def test_installed_ci_runner_isolated_without_sibling_modules(self):
        runner = self.base / "installed runner"
        runner.mkdir()
        installed = runner / "workstation-ci.py"
        shutil.copyfile(ROOT / "scripts/workstation-ci.py", installed)
        result = subprocess.run(
            [str(ISOLATED_PYTHON), "-I", str(installed), "--help"],
            cwd=runner,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        scripts = self.source / "scripts"
        scripts.mkdir()
        helper = scripts / "build_layout.py"
        shutil.copyfile(ROOT / "scripts/build_layout.py", helper)
        probe = (
            "import runpy,sys;from pathlib import Path;"
            "runner=runpy.run_path(sys.argv[1]);"
            "print(runner['checkout_build_directory'](Path(sys.argv[2]),'22.0.466'))"
        )
        command = [str(ISOLATED_PYTHON), "-I", "-c", probe, str(installed), str(self.source)]
        result = subprocess.run(command, cwd=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), default_build_directory(self.source))
        helper.unlink()
        result = subprocess.run(command, cwd=runner, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.source / "build/windows-h22.0.466-release")
        self.assertEqual([path.name for path in runner.iterdir()], ["workstation-ci.py"])

    def test_pure_script_imports_under_isolated_python(self):
        code = (
            "import importlib.util,runpy,sys;"
            "path=sys.argv[1];runpy.run_path(path);"
            "spec=importlib.util.spec_from_file_location('isolated_script',path);"
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))"
        )
        for script in ("runtime-assets.py", "source-history.py", "engine-fork.py", "export-engine-patches.py"):
            result = subprocess.run(
                [str(ISOLATED_PYTHON), "-I", "-c", code, str(ROOT / "scripts" / script)],
                cwd=self.base,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, (script, result.stderr))

    def test_production_features_and_presets_stay_enabled(self):
        presets = json.loads((self.source / "CMakePresets.json").read_text())
        base = next(value for value in presets["configurePresets"] if value["name"] == "houdini-windows")
        self.assertEqual(base["cacheVariables"]["CH_IMAGE_IRIS"], "TRUE")
        self.assertEqual(base["cacheVariables"]["CH_DLSS"], "TRUE")
        with self.assertRaises(ValueError):
            default_build_directory(self.source, "22.0")

    def test_powershell_matches_presets(self):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("PowerShell 7 is unavailable")
        command = (
            ". $env:CH_LAYOUT_HELPER; "
            "Get-DefaultBuildDirectory -SourceRoot $env:CH_LAYOUT_SOURCE -HoudiniVersion 22.0.466; "
            "Get-DefaultBuildDirectory -SourceRoot $env:CH_LAYOUT_SOURCE -HoudiniVersion 22.0.999 -Preset solaris"
        )
        import os

        result = subprocess.run(
            [pwsh, "-NoProfile", "-Command", command],
            env=dict(
                os.environ, CH_LAYOUT_HELPER=str(ROOT / "scripts/build-layout.ps1"), CH_LAYOUT_SOURCE=str(self.source)
            ),
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        self.assertEqual(
            [Path(value) for value in result.stdout.splitlines()],
            [default_build_directory(self.source), default_build_directory(self.source, "22.0.999", "solaris")],
        )


if __name__ == "__main__":
    unittest.main()
