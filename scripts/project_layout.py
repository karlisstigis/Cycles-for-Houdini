# SPDX-License-Identifier: Apache-2.0
"""One folder for everything (docs/dev/README.md, "Working folders"), the Python side of scripts/project-layout.ps1.

What the developer route makes goes inside the project folder, in one working folder that Git ignores:
<project>/_work. <project> is the main working tree of the repository a checkout belongs to (the checkout itself,
or for a linked worktree the main checkout it was made from); a folder that is not the top of its own Git working
tree is its own project."""

from pathlib import Path
import subprocess

WORK = "_work"


def project_root(source_root):
    source = Path(source_root).resolve()
    try:

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(source), "rev-parse", *args], capture_output=True, text=True, timeout=60
            )

        top = git("--show-toplevel")
        common = git("--path-format=absolute", "--git-common-dir")
    except (OSError, subprocess.SubprocessError):
        return source
    if top.returncode or common.returncode or not top.stdout.strip() or not common.stdout.strip():
        return source
    if Path(top.stdout.strip()).resolve() != source:
        return source
    common_dir = Path(common.stdout.strip()).resolve()
    return common_dir.parent if common_dir.name == ".git" else source


def work_path(source_root, name):
    return project_root(source_root) / WORK / name


def in_project_work(path, source_root):
    return Path(path).resolve().is_relative_to(project_root(source_root) / WORK)
