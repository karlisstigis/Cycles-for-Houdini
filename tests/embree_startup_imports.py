# SPDX-License-Identifier: Apache-2.0
"""Offline Windows binary regression. Does not import hou or load a target DLL."""

import argparse
from collections import deque
from functools import cache
import importlib.util
import json
from pathlib import Path
import re


spec = importlib.util.spec_from_file_location(
    "release_package", Path(__file__).resolve().parents[1] / "scripts/release-package.py"
)
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)
tables = cache(package.pe_import_tables)
intel_runtime = re.compile(r"embree\d+|sycl\d+|ur_(?:win_proxy_loader|loader|adapter_.+)|ze_loader")


def ordinary_closure(root, providers):
    pending = deque([root])
    paths = {root.name.lower(): [root.name.lower()]}
    outside = set()
    while pending:
        consumer = pending.popleft()
        ordinary, _ = tables(consumer)
        for name in sorted(ordinary):
            if name in paths:
                continue
            paths[name] = paths[consumer.name.lower()] + [name]
            if name in providers:
                pending.append(providers[name])
            else:
                outside.add(name)
    return dict(
        resolved_count=len(paths) - len(outside),
        intel_paths={name: route for name, route in sorted(paths.items()) if intel_runtime.fullmatch(Path(name).stem)},
        outside_scanned_dirs=sorted(outside),
    )


def check(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--runtime-bin", type=Path, required=True)
    parser.add_argument("--hfs", type=Path, required=True)
    parser.add_argument("--previous-plugin", type=Path)
    args = parser.parse_args()
    providers = {}
    scope = (
        args.plugin.parent,
        args.worker.parent,
        args.runtime_bin,
        args.hfs / "bin",
        args.hfs / "python313",
        args.hfs / "python313/DLLs",
    )
    for directory in scope:
        check(directory.is_dir(), f"Missing provider directory: {directory}")
        for dll in directory.glob("*.dll"):
            providers.setdefault(dll.name.lower(), dll)

    def inspect(path):
        ordinary, delayed = tables(path)
        return dict(
            path=str(path.resolve()),
            sha256=package.file_hash(path),
            ordinary=sorted(ordinary),
            delayed=sorted(delayed),
            closure=ordinary_closure(path, providers),
        )

    plugin = inspect(args.plugin)
    worker = inspect(args.worker)
    houdini = inspect(args.hfs / "bin/houdini.exe")
    check("embree4.dll" in plugin["delayed"], "The DSO should retain Embree as a delay import")
    check(not plugin["closure"]["intel_paths"], f"Eager Intel runtime from the DSO: {plugin['closure']['intel_paths']}")
    check(
        not houdini["closure"]["intel_paths"], f"Eager Intel runtime from Houdini: {houdini['closure']['intel_paths']}"
    )
    check("embree4.dll" in worker["ordinary"], "The CPU worker must still link Embree normally")
    check(
        providers.get("embree4.dll", Path()).parent == args.runtime_bin,
        "The CPU worker must resolve Embree from the package runtime",
    )
    workers_runtime = worker["closure"]["intel_paths"]
    check(all(name in providers for name in workers_runtime), "Incomplete worker runtime")
    result = dict(
        method="Static PE inspection only",
        provider_directories=[str(p.resolve()) for p in scope],
        plugin=plugin,
        worker=worker,
        houdini=houdini,
    )
    if args.previous_plugin:
        previous = inspect(args.previous_plugin)
        check("embree4.dll" in previous["ordinary"], "Expected an eager Embree import in the control DSO")
        check(
            set(previous["ordinary"]) - {"embree4.dll"} == set(plugin["ordinary"]),
            "Other ordinary direct imports changed; review the comparison",
        )
        check(
            set(previous["delayed"]) | {"embree4.dll"} == set(plugin["delayed"]),
            "Other delay imports changed; review the comparison",
        )
        result["previous_plugin"] = previous
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
