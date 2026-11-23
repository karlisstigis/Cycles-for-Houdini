# SPDX-License-Identifier: Apache-2.0
"""The entry documents (README, CONTRIBUTING, guides) name only paths, scripts and options that exist, and every
relative link in every Markdown file points to a file and heading that exist."""

import runpy
import shutil
import subprocess

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
CHECK = runpy.run_path(str(ROOT / "scripts/check-doc-paths.py"))


def test_entry_documents_name_existing_paths_and_options():
    problems = CHECK["Checker"](ROOT).run()
    assert problems == []


def test_every_markdown_link_resolves():
    checker = CHECK["Checker"](ROOT)
    assert checker.run_all_links()
    assert checker.problems == []


def repository(tmp_path, files, use_git):
    root = tmp_path / "repo"
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    if use_git:
        if shutil.which("git") is None:
            pytest.skip("Git-specific fixture")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


@pytest.mark.parametrize("use_git", [False, True])
def test_checker_reports_missing_paths_options_and_anchors(tmp_path, use_git):
    script = "param([string]$BuildDirectory)\n"
    readme = "\n".join(
        [
            "# Title",
            "Run `./scripts/build.ps1 -BuildDirectory x` and `./scripts/build.ps1 -Jobs 4`.",
            "See `docs/missing.md`, [ok](scripts/build.ps1), [gone](docs/gone.md) and [bad](#nowhere).",
            "Skipped: `<build>/bin`, [PENDING-NAME: `launchers/x.cmd`], `third_party/cycles`.",
            "Double-click `START HERE - Build Cycles for Houdini.cmd` and `9 - Missing step.cmd`.",
        ]
    )
    root = repository(
        tmp_path,
        {
            "scripts/build.ps1": script,
            "README.md": readme + "\n",
            "START HERE - Build Cycles for Houdini.cmd": "@echo off\n",
        },
        use_git,
    )
    checker = CHECK["Checker"](root)
    for kind, text in CHECK["spans"](readme):
        (checker.code if kind == "code" else checker.link)("README.md", text)
    assert checker.problems == [
        "README.md: scripts/build.ps1 has no parameter -Jobs",
        "README.md: docs/missing.md does not exist",
        "README.md: no tracked file is named 9 - Missing step.cmd",
        "README.md: link docs/gone.md points to a missing file",
        "README.md: link #nowhere names a missing heading",
    ]


@pytest.mark.parametrize("use_git", [False, True])
def test_all_links_mode_reports_missing_files_and_headings(tmp_path, use_git):
    root = repository(
        tmp_path,
        {
            "docs/a.md": "# A\n\nSee [b](b.md), [c](sub/c.md#second-part), [gone](gone.md) and [bad](sub/c.md#nowhere).\n",
            "docs/b.md": "# B\n\n[Back](a.md#a) and [code](`skipped.md`) `[not a link](x.md)`\n",
            "docs/sub/c.md": "# C\n\n## Second part\n\n[Up](../../README.md)\n",
        },
        use_git,
    )
    checker = CHECK["Checker"](root)
    assert checker.run_all_links() == ["docs/a.md", "docs/b.md", "docs/sub/c.md"]
    assert checker.problems == [
        "docs/a.md: link gone.md points to a missing file",
        "docs/a.md: link sub/c.md#nowhere names a missing heading",
        "docs/sub/c.md: link ../../README.md points to a missing file",
    ]


def test_zip_doc_inventory_does_not_run_git_or_scan_generated_folders(tmp_path, monkeypatch):
    vendor = "include/third_party/pybind11"
    root = repository(
        tmp_path,
        {
            "README.md": f"# Top\n\n[Header]({vendor}/header.h)",
            "_work/a.md": "[bad](gone.md)",
            "third_party/y.md": "[bad](none.md)",
            "artifacts/run.md": "[bad](gone.md)",
            "Installers/README.md": "[bad](none.md)",
            f"{vendor}/header.h": "// Vendored source fixture.",
        },
        False,
    )
    monkeypatch.setenv("PATH", "")
    checker = CHECK["Checker"](root)
    checker.code("README.md", vendor + "/")
    assert checker.run_all_links() == ["README.md"]
    assert checker.problems == []


def test_zip_doc_inventory_checks_links_in_nested_third_party_sources(tmp_path, monkeypatch):
    nested = "include/third_party/pybind11/README.md"
    root = repository(tmp_path, {nested: "# Vendor\n\n[Missing](missing.md)"}, False)
    monkeypatch.setenv("PATH", "")
    checker = CHECK["Checker"](root)
    assert checker.run_all_links() == [nested]
    assert checker.problems == [f"{nested}: link missing.md points to a missing file"]
