# SPDX-License-Identifier: Apache-2.0
"""Preflight exact GPU identities and coupled fixtures' explicit default GPU."""

import argparse
import json
from pathlib import Path
import cycles_houdini as ch
from native_test_runtime import runtime_context

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", required=True)
parser.add_argument("--require-default", action="store_true")
parser.add_argument("--report", type=Path, required=True)
parser.add_argument("--manifest-sha256", required=True)
args = parser.parse_args()
context = runtime_context()
assert context["manifest_sha256"].lower() == args.manifest_sha256.lower(), (
    "Build manifest changed after matrix generation"
)
available = ch.devices()
selected = [device for device in available if device["id"] == args.device]
assert len(selected) == 1 and selected[0]["backend"] in ("CPU", "CUDA", "OPTIX", "HIP", "ONEAPI", "METAL"), (
    args.device,
    available,
)
default = ch.default_device()
if args.require_default:
    assert default == args.device, ("Coupled primary fixtures require their actual default GPU", default, args.device)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(
    json.dumps(
        dict(
            result="passed",
            selected=selected[0],
            default_device=default,
            require_default=args.require_default,
            manifest_sha256=context["manifest_sha256"],
            scope="Identity preflight only; no rendering or backend certification",
        ),
        indent=2,
    )
)
print(args.report)
