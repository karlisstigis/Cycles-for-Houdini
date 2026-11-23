# SPDX-License-Identifier: Apache-2.0
"""Regenerate the public current/adaptive showcase from current definitions."""

from support.paths import artifacts_root
import argparse, hashlib, json, os, runpy, traceback
from pathlib import Path
import hou, numpy as np
from cycles_houdini import device_selection, native_scene, registry

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(__file__).resolve().parents[1]
# The generators (examples/sop-fx-courtyard) default to SOURCE_ROOT/artifacts
# when CYCLES_HOUDINI_TEST_ARTIFACTS is unset (a standalone hython run), while
# the tests that load the showcase read artifacts_root(). Point both at one root.
os.environ.setdefault("CYCLES_HOUDINI_TEST_ARTIFACTS", str(artifacts_root()))
folder = artifacts_root() / "examples/sop-fx-courtyard"
destination = artifacts_root() / "current-schema-cleanup/showcase"
destination.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, checks=[])
artist = folder / "sop-fx-courtyard-artist-fixed.hiplc"
artist_hash = hashlib.sha256(artist.read_bytes()).hexdigest() if artist.exists() else None
try:
    hou.hipFile.clear(suppress_save_prompt=True)
    rop, generated = runpy.run_path(str(root / "examples/sop-fx-courtyard/build_scene.py"))["build"]()
    current = folder / "sop-fx-courtyard-current.hiplc"
    hou.hipFile.load(str(current), suppress_save_prompt=True)
    palette = hou.node("/mat/courtyard_noise_displaced_stone/stone_palette")
    assert palette is not None and len(palette.parmTuple("ramp1c")) == 3 and palette.parm("ramp1ca") is None
    assert palette.parm("alpha_ramp").evalAsRamp().values() == (1.0, 1.0)
    assert hou.node("/out/cycles_sop_courtyard").parm("device") is None
    report["checks"].append(
        "Regenerated current showcase reloads without schema warnings; inline Color/Alpha ramps and current ROP definitions are authored"
    )
    upgrade = runpy.run_path(str(root / "examples/sop-fx-courtyard/adaptive_scene.py"))["upgrade"]()
    adaptive = folder / "sop-fx-courtyard-adaptive.hiplc"
    hou.hipFile.save(str(adaptive))
    upgrade.update(source=str(current), example=str(adaptive))
    (folder / "adaptive-scene.json").write_text(json.dumps(upgrade, indent=2))
    for path in (current, adaptive):
        registry.close_all()
        hou.hipFile.load(str(path), suppress_save_prompt=True)
        rop = hou.node("/out/cycles_sop_courtyard")
        device_selection.set_devices(rop, device)
        rop.setParms(
            dict(resx=128, resy=72, samples=1, ipr_samples=1, use_denoise=0, ipr_use_denoise=0, render_threads=2)
        )
        if path == adaptive:
            turbine = hou.node("/obj/courtyard_turbine")
            assert turbine.evalParm("cycles_subdivision_override") and turbine.evalParm("cycles_subdivision_adaptive")
            assert turbine.evalParm("cycles_subdivision_pixel_size") == 1
            assert rop.evalParm("ipr_dicing_rate") == 8 and rop.evalParm("dicing_rate") == 2
            assert rop.evalParm("max_subdivisions") == 6
            assert rop.evalParm("limit_subdivision") and rop.evalParm("subdivision_limit") == 1000000
        owner = registry.for_rop(rop, native_scene.configuration(rop))
        frame = owner.session.wait(owner.apply(native_scene.capture(rop), hou.time()), timeout=180)
        pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32)
        assert frame["complete"] and np.isfinite(pixels).all() and np.count_nonzero(pixels[::4] > 0.01) > 100
        owner.session.write_exr(destination / (path.stem + "-" + ("cpu" if device == "CPU" else "optix") + ".exr"))
        report["checks"].append(
            dict(
                name="Current showcase renders after warning-free reload",
                scene=path.name,
                status=owner.session.status(),
            )
        )
        registry.close_all()
        closed = owner.session.status()
        assert closed["state"] == "closed" and closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
    assert artist_hash == (hashlib.sha256(artist.read_bytes()).hexdigest() if artist.exists() else None)
    report.update(result="passed", generated=generated, adaptive=upgrade, artist_file_preserved=True)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (destination / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(dict(result="passed", checks=len(report["checks"]))))
