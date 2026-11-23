# SPDX-License-Identifier: Apache-2.0
"""Artifact routing and implementation paths for incremental test migration."""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "houdini/python3.13libs/cycles_houdini"
# The scripts that run inside Blender (GPL-3.0-or-later; package cycles_blender, entry scripts).
BLENDER = ROOT / "blender"


def artifacts_root():
    explicit = os.environ.get("CYCLES_HOUDINI_TEST_ARTIFACTS")
    # Create the base as well: legacy TemporaryDirectory(dir=...) callers need
    # it to exist even when no report has been written in the selected run yet.
    base = (
        Path(explicit)
        if explicit
        else Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]) / "artifacts"
        if os.environ.get("CYCLES_HOUDINI_SOURCE_ROOT")
        and Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"]).resolve() != ROOT.resolve()
        else ROOT / "_work/test-artifacts"
    )
    base = base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def pytest_artifacts_root(explicit=None):
    """Fresh run folders in <checkout>/_work/test-runs/pytest by default (ignored by Git, never scanned)."""
    selected = explicit if explicit is not None else os.environ.get("CYCLES_HOUDINI_TEST_ARTIFACTS")
    if selected:
        return Path(selected).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    return ROOT / "_work/test-runs/pytest" / stamp


def artifact_path(*parts):
    base = artifacts_root()
    path = base.joinpath(*parts)
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Artifact path leaves the selected artifact root")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def product_source(name):
    """Resolve old source names for AST contracts, without importing Houdini."""
    if name == "__init__.py":
        return PACKAGE / "__init__.py"
    if name == "materials.py":
        return PACKAGE / "materials/__init__.py"
    aliases = json.loads((PACKAGE / "_module_aliases.json").read_text())
    candidates = [canonical for canonical, legacy in aliases.items() if legacy == Path(name).stem]
    if len(candidates) != 1:
        raise ValueError("No unique implementation source for " + name)
    return PACKAGE.joinpath(*candidates[0].split(".")).with_suffix(".py")


def configure_test_environment():
    """Keep disposable Python/host outputs under the selected evidence root."""
    directory = artifacts_root() / "temp"
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = os.environ["TEMP"] = os.environ["TMPDIR"] = str(directory)
    os.environ.setdefault("CYCLES_HOUDINI_WORKER_LOG_DIR", str(artifacts_root() / "workers"))
    tempfile.tempdir = str(directory)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return directory


def artifact_label(value, *, limit=48):
    """Stable single filename component for exact device IDs on Windows."""
    if not isinstance(limit, int) or limit < 10:
        raise ValueError("Artifact label limit must be at least 10")
    original = str(value)
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", original).strip(" .") or "case"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *("COM" + str(i) for i in range(1, 10)),
        *("LPT" + str(i) for i in range(1, 10)),
    }
    if label.split(".")[0].upper() in reserved:
        label = "_" + label
    if len(label) > limit:
        label = label[: limit - 9] + "-" + hashlib.sha256(original.encode()).hexdigest()[:8]
    return label
