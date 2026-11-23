# SPDX-License-Identifier: Apache-2.0
"""Run with Houdini Python and the ch_python_bindings_contracts output directory."""

import importlib.util
import json
from pathlib import Path
import sys

# Houdini may pre-load the artist DSO during startup. Importing by name or
# altering sys.path would silently exercise that module instead of this probe.
module_path = (Path(sys.argv[1]) / "_cycles_houdini.pyd").resolve(strict=True)
spec = importlib.util.spec_from_file_location("_cycles_houdini", module_path)
assert spec is not None and spec.loader is not None
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
assert Path(native.__file__).resolve() == module_path, native.__file__
assert hasattr(native, "legacy_failure"), "Expected the isolated binding contract probe"

status = native.status(0)
assert type(status) is dict
assert type(status["generation"]) is int and status["generation"] == 2**63 + 17
assert status["progress"] == 0.375 and status["message"] == "Unicode: \N{SNOWMAN}"
assert status["render_device_ids"] == ["CPU", "OPTIX"]
assert status["motion_warnings"] == ["warning"]
assert type(status["color_update_pending"]) is bool
assert type(status["current_sample"]) is int
assert set(status["source_diagnostics"][0]) == {
    "code",
    "source",
    "resource",
    "message",
    "action",
    "severity",
    "attribute",
}
caps = native.capabilities()
assert type(caps) is dict and caps["image_iris"] is True and caps["bdpt_prototype"] is False
assert caps["surface_graph_definitions"] == ["surface"]
assert caps["material_ramp_max_keys"] == 32
for call, kind, message in [
    (lambda: native.status(-1), RuntimeError, "native argument"),
    (lambda: native.status(-2), native.SceneUpdateRejected, "candidate rejected"),
    (native.legacy_failure, RuntimeError, "legacy argument"),
    (lambda: native.status("wrong"), TypeError, None),
]:
    try:
        call()
    except Exception as error:
        assert type(error) is kind, (type(error), kind)
        if message:
            assert str(error) == message
    else:
        raise AssertionError("exception not raised")
for _ in range(10000):
    native.status(0)
    native.capabilities()
if "--json" in sys.argv:
    print(json.dumps({"status": status, "capabilities": caps}, sort_keys=True))
else:
    print("Python binding contracts passed on", sys.version)
