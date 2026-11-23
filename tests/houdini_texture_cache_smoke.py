# SPDX-License-Identifier: Apache-2.0
"""Exercise upstream tiled image caching with independent color expectations."""

from support.paths import artifacts_root
from array import array
import json
import os
from pathlib import Path
import time
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage3/textures/cache"
destination.mkdir(parents=True, exist_ok=True)
hou.hipFile.load(str(artifacts_root() / "stage3/textures/native-textures.hiplc"), suppress_save_prompt=True)
rop = hou.node("/out/native_texture_cycles")
builder = hou.node("/mat/native_texture_surface")
texture = builder.node("file_image")
rop.parm("samples").set(8)
from cycles_houdini import native_scene


def write(path, color):
    output = OIIO.ImageOutput.create(str(path))
    spec = OIIO.ImageSpec(256, 256, 3, OIIO.FLOAT)
    assert output.open(str(path), spec), output.geterror()
    assert output.write_image(np.broadcast_to(np.asarray(color, dtype=np.float32), (256, 256, 3)).copy()), (
        output.geterror()
    )
    assert output.close(), output.geterror()


def color(frame):
    data = array("f")
    data.frombytes(frame["planes"]["combined"]["pixels"])
    return np.asarray(data)[(32 * 64 + 32) * 4 : (32 * 64 + 32) * 4 + 3]


reports = []
for device in ("CPU", ch.default_device()):
    for cache, generate in ((False, False), (True, True), (True, False)):
        label = ("cpu" if device == "CPU" else "optix") + "-" + str(int(cache)) + "-" + str(int(generate))
        folder = destination / (label + "-" + str(time.time_ns()))
        folder.mkdir()
        path = folder / "color.exr"
        write(path, (0.2, 0.4, 0.8))
        texture.parm("filename").set(str(path))
        with ch.Session(
            device=device,
            width=64,
            height=64,
            samples=8,
            collect_diagnostics=True,
            use_texture_cache=cache,
            auto_texture_cache=generate,
        ) as session:
            started = time.perf_counter()
            frame = session.wait(session.apply(native_scene.capture(rop)))
            first_seconds = time.perf_counter() - started
            measured = color(frame)
            assert np.allclose(measured, (0.2, 0.4, 0.8), atol=0.001, rtol=0), measured
            files = list(folder.rglob("*.tx"))
            if generate:
                assert files, "Automatic caching produced no tx artifact"
            else:
                assert not files, "Caching without generation wrote a derivative"
            conversions = session.status()["geometry_conversions"]
            builds = session.diagnostics()["material_builds"]
            started = time.perf_counter()
            session.wait(session.apply(dict(camera_path=rop.evalParm("camera"))))
            warm_seconds = time.perf_counter() - started
            assert (
                session.status()["geometry_conversions"] == conversions
                and session.diagnostics()["material_builds"] == builds
            )
            assert not session.textures_changed()
            write(path, (0.7, 0.3, 1.2))
            assert session.textures_changed()
            frame = session.wait(session.apply({}))
            updated = color(frame)
            assert np.allclose(updated, (0.7, 0.3, 1.2), atol=0.001, rtol=0), updated
            if generate:
                new_files = list(folder.rglob("*.tx"))
                assert len(new_files) > len(files), "Revision did not isolate the derived cache"
            else:
                new_files = []
            reports.append(
                dict(
                    device=device,
                    cache=cache,
                    automatic_generation=generate,
                    result="passed",
                    initial=measured.tolist(),
                    updated=updated.tolist(),
                    first_seconds=first_seconds,
                    warm_seconds=warm_seconds,
                    tx_files=[dict(path=str(file), bytes=file.stat().st_size) for file in new_files],
                    device_buffer_bytes=session.status()["device_allocation_bytes"],
                )
            )
        assert session.status()["device_allocation_bytes"] == 0
report = dict(
    result="passed",
    cases=reports,
    scope="Functional cache routes and revision isolation on small textures. Timing is diagnostic; large-scene cache performance and disk-retention budgets remain separate gates.",
)
(destination / "houdini-texture-cache-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
