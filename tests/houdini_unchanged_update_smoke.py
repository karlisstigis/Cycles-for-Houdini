# SPDX-License-Identifier: Apache-2.0
"""Display attachment retains samples; genuine scene/asset edits still restart."""

from support.paths import artifacts_root
from copy import deepcopy
import json
import os
from pathlib import Path
import time

import hou
import numpy as np
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import cycles_houdini as ch

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage1/unchanged-update"
destination.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "reuse_receiver", run_init_scripts=False)
box = geo.createNode("box")
box.parmTuple("size").set((1, 1, 0.2))
camera = hou.node("/obj").createNode("cam", "reuse_camera")
camera.setParms(dict(tz=5, projection=1, orthowidth=4))
reports = []


def capture():
    return dict(
        geometry=[ch.geometry_request(geo, box, material="surface")],
        camera_path=camera.path(),
        materials=[dict(key="surface", kind="emission", color=(0.3, 0.2, 0.1))],
        lights=[dict(key="key", strength=(100, 100, 100))],
        background=(0.05, 0.05, 0.05),
        transparent_background=False,
        transparent_glass=False,
        transparent_roughness_threshold=0.1,
    )


def centre(frame, plane="combined"):
    p = frame["planes"][plane]
    return np.frombuffer(p["pixels"], np.float32).reshape(frame["height"], frame["width"], p["channels"])[
        frame["height"] // 2, frame["width"] // 2
    ]


def write_texture(path, color):
    out = OIIO.ImageOutput.create(str(path))
    assert out.open(str(path), OIIO.ImageSpec(8, 8, 3, OIIO.FLOAT)), out.geterror()
    assert out.write_image(np.broadcast_to(np.asarray(color, np.float32), (8, 8, 3)).copy()), out.geterror()
    assert out.close(), out.geterror()
    metadata = path.stat()
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000))


for device in ("CPU", ch.default_device()):
    geo.parm("tx").set(0)
    box.parm("sizex").set(1)
    camera.parm("tz").set(5)
    checks = []
    with ch.Session(
        device=device,
        width=64,
        height=64,
        samples=16,
        interactive=False,
        collect_diagnostics=True,
        use_texture_cache=False,
    ) as session:
        first = session.wait(session.apply(capture(), reuse_if_unchanged=True))
        assert np.allclose(centre(first)[:3], (0.3, 0.2, 0.1), atol=1e-5)
        assert abs(centre(first, "depth")[0] - 4.9) < 0.001
        before = session.status()
        info = session.frame_info()
        copies = deepcopy(session.diagnostics()["buffer_copy_requests"])
        assert session.apply(capture(), reuse_if_unchanged=True) == before["generation"]
        assert session.frame_info() == info
        assert session.status()["unchanged_reuses"] == before["unchanged_reuses"] + 1
        assert session.diagnostics()["buffer_copy_requests"] == copies
        checks.append("Complete render, pixels, samples and copy diagnostics retained on matching attachment")
        forced = session.apply(capture())
        assert forced == before["generation"] + 1
        session.wait(forced)
        checks.append("Explicit Render still starts a fresh generation")

        def changed(name, update):
            before = session.status()
            generation = session.apply(update, reuse_if_unchanged=True)
            assert generation == before["generation"] + 1, (name, before, session.status())
            assert session.status()["unchanged_reuses"] == before["unchanged_reuses"]
            session.wait(generation)
            checks.append(name)

        camera.parm("tz").set(6)
        changed("Camera edit restarts and preserves independent depth", capture())
        assert abs(centre(session.frame(), "depth")[0] - 5.9) < 0.001
        geo.parm("tx").set(0.2)
        changed("OBJ transform restarts", capture())
        update = capture()
        update["geometry"][0]["visibility"] = 1
        changed("Object visibility edit restarts", update)
        update = capture()
        update["geometry"][0]["random_id"] = 1234567
        changed("Object identity attribute edit restarts", update)
        box.parm("sizex").set(1.2)
        changed("SOP geometry edit restarts", capture())
        update = capture()
        update["materials"][0]["color"] = (0.7, 0.1, 0.05)
        changed("Material edit restarts with independently specified radiance", update)
        assert np.allclose(centre(session.frame())[:3], (0.7, 0.1, 0.05), atol=1e-5)
        changed("Light edit restarts", dict(lights=[dict(key="key", strength=(200, 100, 100))]))
        changed("Film transparency edit restarts", dict(transparent_background=True))
        changed("Denoising policy edit restarts", dict(denoising=dict(enabled=False, start_sample=8)))
        changed("Crop edit restarts", dict(crop=(0, 0, 32, 64)))
        before = session.status()["generation"]
        assert session.apply(dict(crop=(0, 0, 32, 64)), reuse_if_unchanged=True) == before
        changed("Motion policy edit restarts", dict(motion_blur=True))
        changed("Resource deletion restarts", dict(deleted_lights=["key"]))
        before = session.status()["generation"]
        try:
            session.apply(dict(crop=(-1, 0, 32, 64)), reuse_if_unchanged=True)
        except RuntimeError as error:
            assert "Crop is outside" in str(error), str(error)
        else:
            raise AssertionError("Reuse path bypassed validation")
        assert session.status()["generation"] == before
        checks.append("Invalid transaction is rejected before reuse or scene mutation")

        path = destination / ("cpu-texture.exr" if device == "CPU" else "optix-texture.exr")
        write_texture(path, (0.2, 0.4, 0.8))
        texture = dict(
            key="surface",
            kind="graph",
            graph=dict(
                schema_version=1,
                nodes=[
                    dict(
                        id="image",
                        definition="cycles/image_texture/1",
                        parameters=dict(filename=str(path), colorspace="data", interpolation="closest"),
                    ),
                    dict(id="emission", definition="cycles/emission/1", parameters=dict(strength=1.0)),
                ],
                links=[{"from": dict(node="image", socket="color"), "to": dict(node="emission", socket="color")}],
                surface=dict(node="emission", socket="emission"),
            ),
        )
        before = session.status()["generation"]
        session.wait(session.apply(dict(materials=[texture]), reuse_if_unchanged=True))
        assert session.status()["generation"] > before
        assert any(d["code"] == "material.missing_uv" for d in session.status()["source_diagnostics"])
        checks.append("New attribute requirements resolve to a local fallback before reuse")
        uv = geo.createNode("attribwrangle")
        uv.setInput(0, box)
        uv.parm("snippet").set("v@uv=set(@P.x+.5,@P.y+.5,0);")
        changed(
            "Texture material establishes known linear radiance",
            dict(geometry=[ch.geometry_request(geo, uv, material="surface")], materials=[texture], crop=(0, 0, 64, 64)),
        )
        assert np.allclose(centre(session.frame())[:3], (0.2, 0.4, 0.8), atol=1e-5)
        before = session.status()["generation"]
        assert session.apply(dict(materials=[texture]), reuse_if_unchanged=True) == before
        write_texture(path, (0.8, 0.3, 0.1))
        changed("Changed file contents restart despite identical shader parameters", dict(materials=[texture]))
        assert np.allclose(centre(session.frame())[:3], (0.8, 0.3, 0.1), atol=1e-5)
        session.cancel()
        changed("A cancelled render cannot be reused", dict())
        original = OCIO.GetCurrentConfig()
        original_env = hou.getenv("OCIO")
        working = (
            "ACEScg"
            if original.getColorSpace(OCIO.ROLE_SCENE_LINEAR).getName() != "ACEScg"
            else "Linear Rec.709 (sRGB)"
        )
        config = OCIO.Config.CreateFromStream(original.serialize())
        config.setWorkingDir(original.getWorkingDir())
        assert config.getColorSpace(working), working
        config.setRole(OCIO.ROLE_SCENE_LINEAR, working)
        profile = destination / ("cpu-working.ocio" if device == "CPU" else "optix-working.ocio")
        profile.write_text(config.serialize(), encoding="utf-8")
        try:
            before_color = session.status()
            hou.putenv("OCIO", str(profile))
            hou.Color.reloadOCIO()
            assert ch.synchronize_color_configuration()
            # Observation does not mutate reference sessions: they adopt the
            # new policy on apply, before considering unchanged-scene reuse.
            observed_color = session.status()
            assert observed_color["generation"] == before_color["generation"]
            assert observed_color["color_epoch"] == before_color["color_epoch"]
            changed("A render working-space change cannot reuse the old color epoch", dict())
            after_color = session.status()
            assert after_color["color_epoch"] > before_color["color_epoch"]
            assert not after_color["color_update_pending"]
            assert session.frame()["working_space"] == working
            assert np.allclose(centre(session.frame())[:3], (0.8, 0.3, 0.1), atol=1e-5)
        finally:
            hou.putenv("OCIO", original_env or "")
            hou.Color.reloadOCIO()
            OCIO.SetCurrentConfig(original)
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0

    with ch.Session(
        device=device, width=96, height=64, samples=1048576, interactive=True, collect_diagnostics=True
    ) as session:
        session.apply(capture())
        deadline = time.monotonic() + 30
        while not session.frame_info():
            assert time.monotonic() < deadline, session.status()
            time.sleep(0.01)
        before = session.status()
        assert before["state"] == "rendering", before
        assert session.apply(capture(), reuse_if_unchanged=True) == before["generation"]
        assert session.status()["state"] == "rendering"
        assert session.status()["unchanged_reuses"] == before["unchanged_reuses"] + 1
        checks.append("Live render keeps its generation and continues tracing")
        session.pause(True)
        before = session.status()["generation"]
        assert session.apply(capture(), reuse_if_unchanged=True) == before
        assert session.status()["state"] == "paused"
        checks.append("Attachment preserves Pause")
        session.pause(False)
        session.cancel()
        before = session.status()["generation"]
        assert session.apply(capture(), reuse_if_unchanged=True) == before + 1
        assert session.status()["state"] == "rendering"
        checks.append("Stop followed by Render restarts rather than showing retained pixels")
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, checks=checks))

report = dict(result="passed", cases=reports)
(destination / "houdini-unchanged-update-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
