# SPDX-License-Identifier: Apache-2.0
"""Verify matching SDK headers/import libraries/runtime without executing OIDN."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sdk_dependencies", ROOT / "houdini/python3.13libs/cycles_houdini/core/sdk_dependencies.py"
)
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)


def inspect(hfs, source=ROOT, cache=None):
    policy = json.loads((Path(source) / "source.lock.json").read_text())["sdk_dependencies"]["openimagedenoise"]
    result = sdk.inspect_sdk(hfs, policy)
    if cache:
        sdk.validate_cache(cache, hfs)
    return dict(openimagedenoise=result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--houdini-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--cache", type=Path)
    parser.add_argument(
        "--expected", type=Path, help="Frozen staged SDK identity to verify before running a native tool"
    )
    args = parser.parse_args()
    try:
        result = inspect(args.houdini_root, args.source_root, args.cache)
        if args.expected:
            sdk.require(
                result == json.loads(args.expected.read_text(encoding="utf-8-sig")),
                "the active SDK differs from the staged dependency identity",
            )
        print(json.dumps(result))
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(json.dumps(dict(result="failed", error=str(error))), file=sys.stderr)
        raise SystemExit(1)
