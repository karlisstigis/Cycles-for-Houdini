# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from geometry_binding_recovery_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
events = []
try:
    for case in CASES:
        f.configure(case)
        baseline = None
        for broken in (False, True, False):
            f.edit(broken)
            frame = entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
            status = entry.session.status()
            pixels = f.check(frame, status, broken, baseline)
            if baseline is None:
                baseline = pixels
            conversions = status["geometry_conversions"]
            entry.session.wait(entry.apply(dict(camera_path=f.rop.evalParm("camera")), hou.time()))
            assert entry.session.status()["geometry_conversions"] == conversions
            assert entry.session.status()["source_diagnostics"] == status["source_diagnostics"]
            events.append(dict(case=case, broken=broken, diagnostics=status["source_diagnostics"]))
    # Diagnostic variants must retain source Cryptomatte identity across kinds.
    f.configure("tangent_color")
    f.edit(True)
    update = native_scene.capture(f.rop)
    update["cryptomatte"] = dict(object=True, material=True, asset=False, levels=2)
    frame = entry.session.wait(entry.apply(update, hou.time()))
    f.check(frame, entry.session.status(), True)
    import numpy as np

    metadata = entry.session.output_metadata()
    for key, value in metadata.items():
        if key.endswith("/name") and value == "CryptoMaterial":
            manifest = json.loads(metadata[key[:-4] + "manifest"])
            assert f.material.path() in manifest and not any("binding_marker" in name for name in manifest), manifest
            p = frame["planes"]["CryptoMaterial00"]
            ids = np.frombuffer(p["pixels"], np.float32).reshape(128, 192, 4)
            assert ids[80, 38, 0] == ids[80, 96, 0] == ids[80, 154, 0], ids[80, [38, 96, 154]]
            break
    else:
        raise AssertionError("Missing material Cryptomatte metadata")
    events.append(dict(check="Mesh, point and curve variants retain one source Cryptomatte identity"))
    # Convenience sparse materials also recover on meshes, not just strands.
    f.configure("volume_only_field")
    with ch.Session(
        device=device, width=192, height=128, samples=64, execution="worker", passes=["combined", "position", "normal"]
    ) as legacy_session:
        neighbor = hou.node("/obj/unchanged")
        geometry = [
            ch.geometry_request(f.obj, f.stash, material="legacy"),
            ch.geometry_request(neighbor, neighbor.displayNode(), material="unaffected"),
        ]
        for legacy_kind in ("volume_absorption", "principled_volume"):
            frame = legacy_session.wait(
                legacy_session.apply(
                    dict(
                        geometry=geometry,
                        camera_path=f.rop.evalParm("camera"),
                        background=(1, 1, 1),
                        materials=[
                            dict(key="legacy", kind=legacy_kind),
                            dict(key="unaffected", kind="emission", color=(0.1, 0.7, 0.2), strength=1.0),
                        ],
                    )
                )
            )
            f.check(frame, legacy_session.status(), True, legacy=True)
            frame = legacy_session.wait(
                legacy_session.apply(
                    dict(materials=[dict(key="legacy", kind="emission", color=(0.2, 0.6, 0.3), strength=1.0)])
                )
            )
            f.check(frame, legacy_session.status(), False)
            events.append(dict(check="Legacy " + legacy_kind + " preserves all mixed components and repairs"))
    f.configure("combined_volume")
    for n, broken in enumerate((False, True, False, True, False), 1):
        hou.setFrame(n)
        f.edit(broken)
        entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))
        path = f.dest / f"recovery.{n:04d}.exr"
        entry.session.write_exr(path)
        reader = oiio.ImageInput.open(str(path))
        assert reader
        warnings = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        reader.close()
        assert bool(warnings) == broken
    with ch.Session(
        device=device,
        width=192,
        height=128,
        samples=64,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal"],
    ) as final:
        for case in CASES:
            f.configure(case)
            for broken in (True, False):
                f.edit(broken)
                frame = final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time()))
                f.check(frame, final.status(), broken)
    f.configure("true_displacement")
    hou.hipFile.save(str(f.dest / "geometry-binding-recovery.hiplc"))
finally:
    registry.close_all()
report = dict(result="passed", device=device, events=events, sequence_frames=5)
path = artifacts_root() / "stage3/materials/recovery/houdini-geometry-binding-recovery-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
