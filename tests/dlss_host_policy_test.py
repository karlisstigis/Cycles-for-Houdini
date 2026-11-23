# SPDX-License-Identifier: Apache-2.0
"""Execute host policy functions with authored values; no Houdini or GPU required."""

import ast
from support.paths import product_source
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
source = product_source("native_scene.py")
tree = ast.parse(source.read_text(encoding="utf-8"))
functions = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in ("interactive_settings", "denoising_configuration")
]
values = dict(
    viewer_interactive=True,
    use_denoise=True,
    denoiser="oidn",
    denoise_use_gpu=True,
    denoise_quality="high",
    denoise_prefilter="accurate",
    denoise_start_sample=16,
    denoise_passes="albedo_normal",
)
values.update({"ipr_" + name: value for name, value in tuple(values.items()) if name != "viewer_interactive"})
values.update(ipr_denoiser="dlss", ipr_dlss_quality="quality")
namespace = dict(hou=SimpleNamespace(time=lambda: 0), rop_value=lambda rop, name, t: values[name])
exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
resolve = namespace["denoising_configuration"]
for quality, (engine, factor) in {
    "native": ("high", 1.0),
    "quality": ("high", 1.5),
    "balanced": ("balanced", 1.0 / 0.58),
    "performance": ("fast", 2.0),
    "ultra_performance": ("fast", 3.0),
}.items():
    values["ipr_dlss_quality"] = quality
    result = resolve(object(), 0)
    assert (result["method"], result["quality"], result["upscale_factor"], result["start_sample"]) == (
        "dlss",
        engine,
        factor,
        0,
    )
    assert resolve(object(), 0, interactive=False)["method"] == "oidn"
values["ipr_dlss_quality"] = "invalid"
try:
    resolve(object(), 0)
except ValueError:
    pass
else:
    raise AssertionError("Unknown DLSS quality accepted")
values.update(ipr_denoiser="auto", ipr_dlss_quality="quality")
result = resolve(object(), 0)
assert result["method"] == "auto" and result["start_sample"] == 16 and "upscale_factor" not in result
values["denoiser"] = "dlss"
try:
    resolve(object(), 0, interactive=False)
except ValueError:
    pass
else:
    raise AssertionError("DLSS leaked into final settings")
print("Passed: DLSS quality/internal scale, IPR-only routing, final isolation and unchanged automatic defaults")
