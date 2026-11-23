# SPDX-License-Identifier: Apache-2.0
"""Independent USD fixture rendered by the pinned official Hydra delegate in husk."""

from support.paths import artifacts_root

import json
import os
from pathlib import Path
import subprocess
import time

import hou
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdRender, UsdShade


root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage0"
destination.mkdir(parents=True, exist_ok=True)
scene_file = destination / "hydra-smoke.usda"
image_file = destination / "hydra-smoke.exr"
image_file.unlink(missing_ok=True)
stage = Usd.Stage.CreateNew(str(scene_file))
UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
UsdGeom.SetStageMetersPerUnit(stage, 1)
mesh = UsdGeom.Mesh.Define(stage, "/world/triangle")
mesh.CreatePointsAttr([Gf.Vec3f(-1, -0.7, 0), Gf.Vec3f(1, -0.7, 0), Gf.Vec3f(0, 0.8, 0)])
mesh.CreateFaceVertexCountsAttr([3])
mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
material = UsdShade.Material.Define(stage, "/materials/red")
shader = UsdShade.Shader.Define(stage, "/materials/red/surface")
shader.CreateIdAttr("UsdPreviewSurface")
shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.8, 0.05, 0.025))
shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.8, 0.05, 0.025))
shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)
material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
camera = UsdGeom.Camera.Define(stage, "/world/camera")
camera.AddTranslateOp().Set(Gf.Vec3d(0, 0, 5))
camera.CreateHorizontalApertureAttr(36)
camera.CreateVerticalApertureAttr(20.25)
camera.CreateFocalLengthAttr(50)
render_var = UsdRender.Var.Define(stage, "/Render/Vars/color")
render_var.CreateDataTypeAttr("color4f")
render_var.CreateSourceNameAttr("color")
render_var.CreateSourceTypeAttr("raw")
render_var.GetPrim().CreateAttribute("driver:parameters:aov:husk:name", Sdf.ValueTypeNames.String).Set("color")
render_var.GetPrim().CreateAttribute("driver:parameters:aov:husk:format", Sdf.ValueTypeNames.Token).Set("color4f")
product = UsdRender.Product.Define(stage, "/Render/Products/beauty")
product.CreateProductTypeAttr("raster")
product.CreateProductNameAttr(str(image_file))
product.CreateOrderedVarsRel().SetTargets([render_var.GetPath()])
settings = UsdRender.Settings.Define(stage, "/Render/Settings")
settings.CreateResolutionAttr(Gf.Vec2i(320, 180))
settings.CreateCameraRel().SetTargets([camera.GetPath()])
settings.CreateProductsRel().SetTargets([product.GetPath()])
settings.GetPrim().CreateAttribute("cycles:samples", Sdf.ValueTypeNames.Int).Set(16)
settings.GetPrim().CreateAttribute("cycles:device", Sdf.ValueTypeNames.String).Set("CPU")
stage.SetMetadata("renderSettingsPrimPath", str(settings.GetPath()))
stage.GetRootLayer().Save()
husk = Path(hou.getenv("HFS")) / "bin" / "husk.exe"
environment = os.environ.copy()
environment["CYCLES_DEVICE"] = "CPU"
started = time.monotonic()
result = subprocess.run(
    [
        str(husk),
        "--indie",
        "-R",
        "HdCyclesPlugin",
        "--headlight",
        "none",
        "-s",
        str(settings.GetPath()),
        str(scene_file),
    ],
    env=environment,
    capture_output=True,
    text=True,
    timeout=120,
)
(destination / "hydra-smoke.log").write_text(result.stdout + result.stderr, encoding="utf-8")
report = dict(
    renderer="HdCyclesPlugin",
    device="CPU",
    returncode=result.returncode,
    elapsed_seconds=time.monotonic() - started,
    output_exists=image_file.is_file(),
)
(destination / "hydra-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(result.stdout)
print(result.stderr)
print(json.dumps(report))
assert result.returncode == 0 and image_file.is_file(), "husk smoke failed; see hydra-smoke.log"
# The byte-level EXR inspection is separate from the delegate's scene conversion.
import OpenImageIO as OIIO

image = OIIO.ImageInput.open(str(image_file))
assert image is not None
spec = image.spec()
pixels = image.read_image(format=OIIO.FLOAT)
assert (spec.width, spec.height) == (320, 180)
centre = pixels[90, 160]
assert centre[0] > centre[1] * 2 and centre[0] > 0.1
image.close()
report["expected_red_centre"] = list(map(float, centre))
(destination / "hydra-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
