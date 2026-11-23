# SPDX-License-Identifier: Apache-2.0
"""Opt-in native execution; collection never loads hou or the native extension."""

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "houdini/python3.13libs"))
# The Blender component (GPL-3.0-or-later, package cycles_blender): tests that drive the exporter
# import it; tests/pytest_cases/test_blender_component_boundary.py keeps such tests free of hou.
sys.path.insert(0, str(ROOT / "blender"))
sys.path.insert(0, str(ROOT / "tests"))


def pytest_addoption(parser):
    parser.addoption("--native", action="store_true", help="Run verified native hython cases")
    parser.addoption("--artifacts", type=Path, help="Output root for migrated tests")
    parser.addoption(
        "--catalogue",
        action="store_true",
        help="Discover incremental headless catalogue recipes; execute with --native",
    )
    parser.addoption("--build-directory", type=Path, help="Verified build for catalogue recipes")
    parser.addoption("--houdini-root", type=Path, help="Exact Houdini SDK for catalogue recipes")
    parser.addoption("--test-dependencies", type=Path, help="Pinned dependencies for the existing native runner")


def pytest_ignore_collect(collection_path, config):
    if collection_path.name in ("test_catalogue_native.py", "test_dependent_recipes.py") and not config.getoption(
        "--catalogue"
    ):
        return True
    if collection_path.is_file() and collection_path.suffix == ".py":
        return collection_path.parent != ROOT / "tests/pytest_cases"
    return None


def pytest_configure(config):
    from support.paths import configure_test_environment, pytest_artifacts_root

    artifacts = pytest_artifacts_root(config.getoption("--artifacts"))
    os.environ["CYCLES_HOUDINI_TEST_ARTIFACTS"] = str(artifacts)
    configure_test_environment()


def pytest_report_header(config):
    return "Test artifacts: " + os.environ["CYCLES_HOUDINI_TEST_ARTIFACTS"]


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--native"):
        for item in items:
            if "hython" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Use --native with a verified hython runtime"))
