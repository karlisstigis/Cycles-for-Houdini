# SPDX-License-Identifier: Apache-2.0
"""Run a selected native fixture after checking its actual loaded runtime."""

import runpy
import sys
import json
import os
from pathlib import Path
from native_runtime_identity import verify
from support.devices import resolve_device_argument
from support.paths import configure_test_environment

configure_test_environment()

verify(sys.argv[1])
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.argv = sys.argv[1:]
if "CYCLES_HOUDINI_TEST_IDENTITY" in os.environ:
    import cycles_houdini as ch

    available = ch.devices()
    optix = sorted(item["id"] for item in available if item["backend"] == "OPTIX")
    # Native test defaults must also work on a clean CI GPU, rather than rely
    # on the artist application's workstation-specific RTX 4090 preference.
    os.environ["CYCLES_HOUDINI_DEVICE"] = os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE") or (
        optix[0] if optix else "CPU"
    )
if "--device" in sys.argv:
    import cycles_houdini as ch

    backend = sys.argv[sys.argv.index("--device") + 1]
    requested_id = os.environ.get("CYCLES_HOUDINI_TEST_" + backend + "_DEVICE")
    arguments, selected = resolve_device_argument(sys.argv[0], sys.argv[1:], ch.devices(), requested_id)
    sys.argv[1:] = arguments
    identity_path = os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY_REPORT")
    if selected is not None and identity_path and Path(identity_path).is_file():
        path = Path(identity_path)
        identity = json.loads(path.read_text())
        identity["selected_device"] = selected
        path.write_text(json.dumps(identity, indent=2))
runpy.run_path(sys.argv[0], run_name="__main__")
