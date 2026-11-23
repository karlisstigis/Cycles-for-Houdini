# SPDX-License-Identifier: Apache-2.0
"""Resolve build defaults from the same presets used by direct CMake builds.

The developer build cache is <project>/_work/dev/windows-h<build>-<variant>, inside the project folder
(docs/dev/README.md, "Working folders")."""

import json
from pathlib import Path
import re


def default_build_directory(source_root, houdini_version=None, preset="normal"):
    source = Path(source_root).resolve()
    presets = json.loads((source / "CMakePresets.json").read_text(encoding="utf-8-sig"))
    selected = [value for value in presets["configurePresets"] if value["name"] == preset]
    if len(selected) != 1 or not selected[0].get("binaryDir"):
        raise ValueError(f"Missing build directory for preset {preset}")
    directory = selected[0]["binaryDir"]
    for macro, value in (("sourceDir", source), ("sourceParentDir", source.parent), ("sourceDirName", source.name)):
        directory = directory.replace("${" + macro + "}", str(value))
    if "${" in directory:
        raise ValueError(f"Unsupported build-directory macro in preset {preset}")
    if houdini_version:
        if not re.fullmatch(r"\d+\.\d+\.\d+", houdini_version):
            raise ValueError("An exact Houdini build version is required")
        directory = re.sub(r"(?<=windows-h)\d+\.\d+\.\d+(?=-)", houdini_version, directory)
    return Path(directory).resolve()


def earlier_build_directory(source_root, build_directory):
    """The same cache's place before the one-folder layout (<parent>/<folder>-build/<name>), if it exists.

    Only named to the user, never used and never deleted."""
    source = Path(source_root).resolve()
    old = source.parent / (source.name + "-build") / Path(build_directory).name
    return old if old.is_dir() else None


def validate_build_source(source_root, build_directory):
    """An explicit external or legacy cache must belong to this source checkout."""
    source, build = Path(source_root).resolve(), Path(build_directory).resolve()
    if source.is_relative_to(build):
        raise RuntimeError("Build directory must not contain the source checkout")
    cache = {}
    for line in (build / "CMakeCache.txt").read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"([^/#][^:]*):[^=]+=(.*)$", line)
        if match:
            cache[match[1]] = match[2]
    home = cache.get("CMAKE_HOME_DIRECTORY")
    if not home or Path(home).resolve() != source:
        raise RuntimeError("Build cache belongs to a different source checkout")
    cached_build = cache.get("CMAKE_CACHEFILE_DIR")
    if not cached_build or Path(cached_build).resolve() != build:
        raise RuntimeError("Build cache was relocated; configure a fresh external build directory")
    return build
