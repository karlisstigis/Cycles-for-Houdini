# SPDX-License-Identifier: Apache-2.0
"""Large portable input: full decode, cold TX generation and warm TX reuse.

Run with scripts/run-hython.ps1. Set CYCLES_HOUDINI_TEXTURE_BENCHMARK_SIZE
and CYCLES_HOUDINI_TEXTURE_BENCHMARK_REPEATS to change the 4096/3 defaults.
Times include scene application and rendering; filesystem caches are not flushed.
"""

from array import array
import json
import os
from pathlib import Path
import time

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from support.paths import artifacts_root


folder = artifacts_root() / "portable-texture-benchmark" / str(time.time_ns())
folder.mkdir(parents=True)
size = int(os.getenv("CYCLES_HOUDINI_TEXTURE_BENCHMARK_SIZE", "4096"))
repeats = int(os.getenv("CYCLES_HOUDINI_TEXTURE_BENCHMARK_REPEATS", "3"))
assert 64 <= size <= 16384 and 1 <= repeats <= 20
builtin = OCIO.Config.CreateFromBuiltinConfig("studio-config-latest")
config = OCIO.Config()
config.setVersion(2, 5)
reference = OCIO.ColorSpace(name="Benchmark Reference")
config.addColorSpace(reference)
working = OCIO.ColorSpace(name="Benchmark Working")
working.setTransform(
    builtin.getProcessor("ACEScg", "ACES2065-1").createGroupTransform(), OCIO.COLORSPACE_DIR_TO_REFERENCE
)
config.addColorSpace(working)
raw = OCIO.ColorSpace(name="Benchmark Raw", isData=True)
config.addColorSpace(raw)
config.setRole(OCIO.ROLE_SCENE_LINEAR, working.getName())
config.setRole(OCIO.ROLE_DEFAULT, working.getName())
config.setRole(OCIO.ROLE_DATA, raw.getName())
config.setRole(OCIO.ROLE_COMPOSITING_LOG, reference.getName())
config.setRole(OCIO.ROLE_COLOR_TIMING, reference.getName())
config.setRole("aces_interchange", reference.getName())
config.addDisplayView("Benchmark Display", "Raw", working.getName(), "")
config.validate()
config_path = folder / "benchmark.ocio"
config_path.write_text(config.serialize(), encoding="utf-8")
hou.putenv("OCIO", str(config_path))
hou.putenv("HOUDINI_OCIO_FILENAME_COLORSPACE", "0")
hou.Color.reloadOCIO()
ch.synchronize_color_configuration()
assert OCIO.GetCurrentConfig().getColorSpace("Benchmark Working")

# Smooth gradients plus deterministic fine variation defeat constant-color TX
# elimination and make disk costs representative of nonconstant float textures.
rng = np.random.default_rng(426)
pixels = rng.random((size, size, 4), dtype=np.float32) * 0.001
pixels[:, :, :3] += np.linspace(0.125, 1.5, size, dtype=np.float32)[None, :, None]
pixels[:, :, 3] = 0.375
path = folder / "portable.exr"
spec = OIIO.ImageSpec(size, size, 4, OIIO.FLOAT)
spec.attribute("colorInteropID", "lin_ap0_scene")
spec.attribute("compression", "zip")
writer = OIIO.ImageOutput.create(str(path))
assert writer.open(str(path), spec) and writer.write_image(pixels) and writer.close()
del pixels

geo = hou.node("/obj").createNode("geo", "benchmark_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parmTuple("size").set((8, 8))
uv = geo.createNode("attribwrangle")
uv.setInput(0, grid)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "benchmark_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
request = ch.geometry_request(geo, uv, material="surface")
material = dict(
    key="surface",
    kind="graph",
    graph=dict(
        schema_version=1,
        nodes=[
            dict(
                id="file",
                definition="cycles/image_texture/1",
                parameters=dict(
                    filename=str(path), colorspace="auto", interpolation="closest", alpha_type="channel_packed"
                ),
            ),
            dict(id="light", definition="cycles/emission/1", parameters=dict(strength=1.0)),
        ],
        links=[{"from": dict(node="file", socket="color"), "to": dict(node="light", socket="color")}],
        surface=dict(node="light", socket="emission"),
    ),
)
cases = []
baseline = None
for device in ("CPU", ch.default_device()):
    for derivative in folder.rglob("*.tx"):
        derivative.unlink()
    for route, cache, generate, count in (
        ("uncached", False, False, repeats),
        ("cold_tx", True, True, repeats),
        ("warm_tx", True, False, repeats),
    ):
        # Cold generation is measured separately for each backend. Delete only
        # this invocation's owned derivatives, never shared asset caches.
        for trial in range(count):
            if route == "cold_tx":
                for derivative in folder.rglob("*.tx"):
                    derivative.unlink()
            with ch.Session(
                device=device, width=32, height=32, samples=1, use_texture_cache=cache, auto_texture_cache=generate
            ) as session:
                started = time.perf_counter()
                frame = session.wait(
                    session.apply(
                        dict(geometry=[request], materials=[material], camera_path=camera.path(), background=(0, 0, 0))
                    )
                )
                seconds = time.perf_counter() - started
                assert frame["working_space"] == "Benchmark Working", frame["working_space"]
                inputs = session.texture_inputs()
                if "portable_decode_bytes" in inputs[0]:
                    assert inputs[0]["portable_decode_bytes"] == size * size * 16, inputs
                    assert inputs[0]["portable_cache_format"].startswith("float32 RGBA OpenEXR"), inputs
                actual = array("f")
                actual.frombytes(frame["planes"]["combined"]["pixels"])
                actual = np.asarray(actual).reshape((32, 32, 4))
                assert np.isfinite(actual).all()
                if baseline is None:
                    baseline = actual.copy()
                error = float(np.max(np.abs(actual - baseline)))
                # TX deliberately filters mip levels; a smooth ramp allows a
                # small sampling difference, unlike the independent texel tests.
                assert error < 0.02, (route, device, error)
                derivatives = list(folder.rglob("*.tx"))
                if cache:
                    assert len(derivatives) == 1, derivatives
                cases.append(
                    dict(
                        device=device,
                        route=route,
                        trial=trial,
                        seconds=seconds,
                        source_bytes=path.stat().st_size,
                        tx_bytes=sum(file.stat().st_size for file in derivatives),
                        max_pixel_error=error,
                        inputs=inputs,
                        working_space=frame["working_space"],
                        device_buffer_bytes=session.status()["device_allocation_bytes"],
                    )
                )
                print(json.dumps(cases[-1]), flush=True)
report = dict(
    result="passed",
    size=size,
    repeats=repeats,
    cases=cases,
    scope="New sessions; scene apply-to-frame times; OS file caches retained; float32 RGBA TX with mipmaps.",
)
(folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("PORTABLE_BENCHMARK_REPORT=" + str(folder / "report.json"), flush=True)
