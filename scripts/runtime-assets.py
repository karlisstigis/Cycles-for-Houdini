# SPDX-License-Identifier: Apache-2.0
"""Validate immutable generated assets in a versioned development host."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import runpy

OUTPUTS = (
    "otls/cycles_ipr.hda",
    "otls/cycles_light.hda",
    "otls/cycles_blender_file.hda",
    "otls/cycles_blender_hierarchy.hda",
)
SOLARIS_OUTPUTS = ("dso/usd_plugins/plugInfo.json", "dso/usd_plugins/hdCycles/resources/plugInfo.json")
MARKER = "runtime-assets.json"


def required_outputs(host, solaris):
    if not solaris and (host / "dso/usd_plugins").exists():
        raise RuntimeError("Solaris plugins must not be staged in a native OBJ/SOP host. Build a fresh host.")
    return OUTPUTS + SOLARIS_OUTPUTS if solaris else OUTPUTS


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(host):
    """Include every frozen runtime file, ignoring interpreter bytecode only."""
    return {
        path.relative_to(host).as_posix(): path
        for path in host.rglob("*")
        if path.is_file()
        and path.name != MARKER
        and "__pycache__" not in path.parts
        and path.suffix not in (".pyc", ".pyo")
    }


def contained(base, relative):
    base = base.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise RuntimeError("Runtime asset path leaves its root: " + str(relative))
    return path


def recipe(root, host, houdini_root, solaris=False):
    root = root.resolve()
    host = host.resolve()
    records = []
    for directory in ("houdini", "src", "cmake", "patches"):
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            records.append(dict(path=path.relative_to(root).as_posix(), sha256=digest(path)))
    for relative in (
        "CMakeLists.txt",
        "CMakePresets.json",
        "source.lock.json",
        "scripts/stage-development.ps1",
        "scripts/runtime-assets.py",
        "scripts/build_layout.py",
        "scripts/build-layout.ps1",
        "scripts/create-light-asset.py",
        "scripts/create-blender-assets.py",
        "scripts/sdk-dependencies.py",
        "scripts/environment-report.py",
        "scripts/engine_source.py",
        "scripts/generate-node-help.py",
        "scripts/generate-property-help.py",
        "scripts/generate-viewer-help-icons.py",
        "scripts/pass-schema.py",
        "scripts/finalize-assets.py",
        "scripts/offline-scripts.py",
        "scripts/rop-layout.py",
        "scripts/rop-layout.json",
        "scripts/soho_pipe.py",
        "scripts/render_hip.py",
    ):
        path = contained(root, relative)
        records.append(dict(path=relative, sha256=digest(path)))
    for relative in ("dso/_cycles_houdini.dll", "bin/ch_worker.exe"):
        path = contained(host, relative)
        records.append(dict(path="@host/" + relative, sha256=digest(path)))
    value = dict(
        schema_version=1,
        solaris=solaris,
        houdini_root=str(houdini_root.resolve()),
        inputs=sorted(records, key=lambda item: item["path"]),
    )
    value["sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def check(root, host, houdini_root, solaris=False):
    required = required_outputs(host, solaris)
    current = recipe(root, host, houdini_root, solaris)
    marker = contained(host, MARKER)
    if marker.exists():
        saved = json.loads(marker.read_text(encoding="utf-8-sig"))
        if saved.get("schema_version") != 1 or saved.get("recipe_sha256") != current["sha256"]:
            raise RuntimeError(
                "Frozen asset inputs changed. Run build.ps1 for a fresh versioned host; existing assets will not be rewritten."
            )
        outputs = saved.get("outputs", [])
        names = {item.get("path") for item in outputs}
        if len(outputs) != len(names) or not set(required).issubset(names):
            raise RuntimeError("Invalid frozen asset inventory. Build a fresh versioned host.")
        if names != set(inventory(host)):
            raise RuntimeError("Frozen runtime files were added or removed. Build a fresh versioned host.")
        for entry in outputs:
            path = contained(host, entry["path"])
            if not path.is_file() or path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
                raise RuntimeError(
                    "Frozen asset changed or is missing: " + entry["path"] + ". Build a fresh versioned host."
                )
        return dict(action="reuse", recipe_sha256=current["sha256"])
    if any(contained(host, name).exists() for name in required):
        raise RuntimeError(
            "Existing assets have no immutable inventory. Run build.ps1 for a fresh versioned host; legacy assets will not be overwritten."
        )
    return dict(action="create", recipe_sha256=current["sha256"])


def seal(root, host, houdini_root, expected, solaris=False):
    required = required_outputs(host, solaris)
    current = recipe(root, host, houdini_root, solaris)
    if current["sha256"] != expected:
        raise RuntimeError("Asset inputs changed during generation. Do not publish this host; build a fresh one.")
    for name in required:
        path = contained(host, name)
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError("Generated asset is missing or empty: " + name)
    outputs = []
    for name in sorted(inventory(host)):
        path = contained(host, name)
        outputs.append(dict(path=name, bytes=path.stat().st_size, sha256=digest(path)))
    # Exclusive creation prevents an existing host inventory being replaced.
    with contained(host, MARKER).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(dict(schema_version=1, recipe_sha256=expected, recipe=current, outputs=outputs), stream, indent=2)
        stream.write("\n")
    return dict(action="sealed", recipe_sha256=expected)


def frozen_json(host, relative, value, allow_create=False):
    path = contained(host, relative)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8-sig")) != value:
            raise RuntimeError("Frozen runtime metadata changed: " + relative + ". Build a fresh versioned host.")
        return dict(action="reuse", path=relative)
    if not allow_create:
        raise RuntimeError("Frozen runtime metadata is missing: " + relative + ". Build a fresh versioned host.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return dict(action="created", path=relative)


def publish_json(build, relative, value, ignore_timestamp=False):
    path = contained(build, relative)
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8-sig"))

        def comparable(item):
            return {key: val for key, val in item.items() if key != "generated_utc"} if ignore_timestamp else item

        if comparable(previous) == comparable(value):
            return dict(action="reuse", path=relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = contained(build, str(path.relative_to(build)) + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    # Both paths are checked inside the build; the replacement is nonrecursive.
    os.replace(temporary, path)
    return dict(action="published", path=relative)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "seal", "frozen-json", "publish-json"))
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--build-directory", type=Path)
    parser.add_argument("--houdini-root", type=Path)
    parser.add_argument("--expected")
    parser.add_argument("--solaris", action="store_true")
    parser.add_argument("--path")
    parser.add_argument("--allow-create", action="store_true")
    parser.add_argument("--ignore-timestamp", action="store_true")
    args = parser.parse_args()
    # Staging also runs under isolated Python; do not depend on sys.path.
    layout = runpy.run_path(str(Path(__file__).with_name("build_layout.py")))
    build = layout["validate_build_source"](
        args.source_root, args.build_directory or layout["default_build_directory"](args.source_root)
    )
    if not args.host_root.resolve().is_relative_to(build):
        raise RuntimeError("Native host root must stay inside this build directory")
    if args.mode == "check":
        result = check(args.source_root, args.host_root, args.houdini_root, args.solaris)
    elif args.mode == "seal":
        result = seal(args.source_root, args.host_root, args.houdini_root, args.expected, args.solaris)
    elif args.mode == "frozen-json":
        result = frozen_json(args.host_root, args.path, json.load(sys.stdin.buffer), args.allow_create)
    else:
        result = publish_json(build, args.path, json.load(sys.stdin.buffer), args.ignore_timestamp)
    print(json.dumps(result))
