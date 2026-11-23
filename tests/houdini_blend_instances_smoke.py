# SPDX-License-Identifier: Apache-2.0
"""Compare ordinary Houdini point-instance renders to native Blender reference EXRs."""

import argparse
import json
import os
from pathlib import Path
import subprocess

import hou
import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio

import cycles_houdini as ch
from cycles_houdini import blender_assets, native_scene
from cycles_houdini.device_selection import set_devices
from support.images import ssim
from support.devices import device_pci_bus_id, match_reference_optix_device


def pixels(path):
    reader = oiio.ImageInput.open(str(path))
    assert reader, path
    try:
        channels = reader.spec().channelnames
        return reader.read_image(format=oiio.FLOAT)[:, :, [channels.index(c) for c in "RGBA"]]
    finally:
        reader.close()


parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True, type=Path)
parser.add_argument("--device", required=True, choices=("CPU", "OPTIX"))
parser.add_argument("--packed-reference", action="store_true", help="Render the serialized Blender reference SOP")
parser.add_argument(
    "--generate-reference",
    action="store_true",
    help="Create the fixture and render its reference images in background Blender",
)
args = parser.parse_args()
dest = args.out.resolve()
requested_optix = os.environ.get("CYCLES_HOUDINI_TEST_OPTIX_DEVICE") if args.device == "OPTIX" else None
if args.device == "OPTIX":
    devices = [item for item in ch.devices() if item["backend"] == "OPTIX"]
    if requested_optix:
        matches = [item for item in devices if item["id"] == requested_optix]
        assert len(matches) == 1, ("Selected test OptiX device is unavailable", requested_optix, devices)
        selected_device = matches[0]
    else:
        selected_device = next((item for item in devices if "4090" in item["name"]), devices[0])
else:
    selected_device = next(item for item in ch.devices() if item["id"] == "CPU")
device = selected_device["id"]
selected_pci = device_pci_bus_id(selected_device.get("physical_id", "")) or device_pci_bus_id(device)
reference_arguments = []
if requested_optix:
    reference_arguments += ["--optix-device-name", selected_device["name"], "--host-device-id", device]
    if selected_pci:
        reference_arguments += ["--optix-device-pci", selected_pci]
if args.generate_reference:
    dest.mkdir(parents=True, exist_ok=True)
    generator = Path(__file__).with_name("blend_instances_reference.py")
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "OCIO")}
    with (dest / "blender-reference.log").open("w") as log:
        subprocess.run(
            [
                str(blender_assets._blender()),
                "--background",
                "--factory-startup",
                "--disable-autoexec",
                "--python-exit-code",
                "1",
                "--python",
                str(generator),
                "--",
                "--device",
                args.device,
                "--out",
                str(dest),
                *reference_arguments,
            ],
            env=env,
            check=True,
            timeout=120,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
config = ocio.Config.CreateFromStream(ocio.GetCurrentConfig().serialize())
config.setWorkingDir(ocio.GetCurrentConfig().getWorkingDir())
config.setRole(ocio.ROLE_SCENE_LINEAR, "Linear Rec.709 (sRGB)")
profile = dest / ("rec709-" + args.device + ".ocio")
profile.write_text(config.serialize())
hou.putenv("OCIO", str(profile))
hou.Color.reloadOCIO()
ch.synchronize_color_configuration()
hou.setFrame(32)
root = hou.node("/obj")
geo = root.createNode("geo", "blend_points", run_init_scripts=False)
sop = geo.createNode("attribwrangle", "point_reference")
sop.parm("class").set(0)
sop.setDisplayFlag(True)
sop.setRenderFlag(True)
cam = root.createNode("cam")
cam.setParms(dict(tz=5, projection=1, orthowidth=4))
rop = hou.node("/out").createNode("cycles_ipr", "blend_instances")
set_devices(rop, device)
rop.setParms(
    dict(
        source_mode="objects",
        objectroot="/obj",
        vobject="blend_points",
        lights="",
        camera=cam.path(),
        resx=128,
        resy=128,
        samples=128,
        ipr_samples=128,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        transparent_background=1,
        pixel_filter="box",
        filter_width=1,
        film_exposure=1,
        use_material_assignments=1,
        allowmotionblur=0,
        render_threads=4,
    )
)
rop.parmTuple("background").set((0, 0, 0))
variant = "-packed" if args.packed_reference else ""
example = dest / f"example-{args.device}{variant}.hip"
hou.hipFile.setName(str(example))
asset = "$HIP/animated panels.blend"
reference_metadata_path = dest / ("blender-" + args.device + ".json")
reference_metadata = json.loads(reference_metadata_path.read_text()) if reference_metadata_path.is_file() else {}
if requested_optix:
    assert reference_metadata.get("device_id") and reference_metadata.get("device_name"), (
        "Regenerate references to verify the selected GPU"
    )
if reference_metadata.get("device_id"):
    assert reference_metadata["backend"] == args.device, reference_metadata
    if args.device == "OPTIX":
        verified_reference = match_reference_optix_device(
            [
                dict(
                    id=reference_metadata["device_id"],
                    name=reference_metadata["device_name"],
                    backend=reference_metadata["backend"],
                )
            ],
            name=selected_device["name"],
            pci=selected_pci,
        )
        assert verified_reference["id"] == reference_metadata["device_id"]
    else:
        assert reference_metadata["device_id"] == "CPU", reference_metadata
report = dict(
    device=device,
    entry_point="packed" if args.packed_reference else "point",
    backend=args.device,
    host=ch.__file__,
    frames=[],
    verified_houdini_device_id=device,
    houdini_device=selected_device,
    verified_reference_device_id=reference_metadata.get("device_id"),
    reference_device=reference_metadata,
)
if args.packed_reference:
    packed = geo.createNode("cycles::blender_reference", "packed_blender")
    packed.parm("file").set(asset)
    packed.parm("frame").deleteAllKeyframes()
    on_disk = geo.createNode("file", "reopened_geometry")
    on_disk.setDisplayFlag(True)
    on_disk.setRenderFlag(True)


def freeze_packed(form, name, frame):
    packed.setParms(dict(target_kind=form, target=name, frame=frame))
    geometry = packed.geometry()
    assert len(geometry.prims()) == 1, (packed.errors(), geometry.prims())
    path = dest / f"{args.device}-{form}-{frame:g}.bgeo.sc"
    geometry.saveToFile(str(path))
    on_disk.parm("file").set(str(path))
    prim = on_disk.geometry().prim(0)
    assert prim.intrinsicValue("cycles_blender_frame") == frame
    assert prim.intrinsicValue("cycles_blender_reference") == asset + "#" + form + "=" + name


with ch.Session(
    device=device, width=128, height=128, samples=128, execution="worker", threads=4, collect_diagnostics=True
) as session:
    for form, name in (("collection", "Panel Set"), ("object", "Hero Panel")):
        previous = None
        for frame in (1, 7.5):
            stem = f"{args.device}-{form}-{frame:g}"
            token = json.dumps(asset + "#" + form + "=" + name)
            if args.packed_reference:
                freeze_packed(form, name, frame)
            else:
                sop.parm("snippet").set(
                    'int p=addpoint(0, {0,0,0}); setpointattrib(0,"instance",p,'
                    + token
                    + '); setpointattrib(0,"index",p,'
                    + repr(float(frame))
                    + ");"
                )
            session.wait(session.apply(native_scene.capture(rop)))
            status = session.status()
            assert not status["error"], status
            assert status["render_device_ids"] == [device], status
            problems = status["source_diagnostics"]
            print("source diagnostics:", problems, flush=True)
            assert not any("unavailable" in d["code"] for d in problems), problems
            target = dest / ("houdini-" + stem + variant + ".exr")
            session.write_exr(target)
            actual = pixels(target)
            ref = pixels(dest / ("blender-" + stem + ".exr"))
            assert np.isfinite(actual).all() and np.isfinite(ref).all()
            # Measure the foreground so a matching transparent background cannot hide a mismatch.
            mask = (ref[:, :, 3] > 0.01) | (actual[:, :, 3] > 0.01)
            assert mask.sum() > 1500
            error = actual[mask] - ref[mask]
            rgb = float(np.sqrt(np.mean(error[:, :3] ** 2)))
            alpha = float(np.sqrt(np.mean(error[:, 3] ** 2)))
            # Keep spatial neighborhoods: SSIM on a flattened foreground mask
            # would compare unrelated pixels. Crop to the union foreground bounds
            # so transparent background cannot dominate the structure score.
            ys, xs = np.nonzero(mask)
            crop = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
            rgb_ssim = ssim(actual[crop][:, :, :3], ref[crop][:, :, :3], data_range=1, channel_axis=-1)
            alpha_ssim = ssim(actual[crop][:, :, 3], ref[crop][:, :, 3], data_range=1)
            row = dict(
                form=form,
                frame=frame,
                foreground_pixels=int(mask.sum()),
                rgb_rmse=rgb,
                alpha_rmse=alpha,
                rgb_ssim=rgb_ssim,
                alpha_ssim=alpha_ssim,
                ssim_minimum=0.97,
                ssim_data_range=1,
                ssim_crop=[int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)],
                diagnostics=problems,
                render_device_ids=status["render_device_ids"],
            )
            if args.packed_reference:
                prim = on_disk.geometry().prim(0)
                assert prim.intrinsicValue("cycles_blender_bounds_known"), problems
                bounds = json.loads(prim.intrinsicValue("cycles_blender_bounds"))
                assert bounds["frame"] == frame
                row["local_bounds"] = bounds["bounds"]
                if previous is not None:
                    assert not np.allclose(report["frames"][-1]["local_bounds"], row["local_bounds"])
            print(json.dumps(row), flush=True)
            assert rgb < 0.018 and alpha < 0.018, row
            # Unit-strength emission textures and coverage alpha have authored
            # span 1 in scene-linear Rec.709. The existing 0.018 RMSE permits
            # sampling differences at silhouettes; 0.97 SSIM additionally rejects
            # displaced texture/geometry structure. Neither threshold is fitted
            # dynamically to a result, and RGB never includes opaque alpha.
            assert rgb_ssim >= 0.97 and alpha_ssim >= 0.97, row
            if previous is not None:
                assert np.abs(previous - actual).sum() > 500, "Animation did not change the rendered image"
            previous = actual
            report["frames"].append(row)
    # Check two time samples in one scene, against translated independent
    # Blender renders. Integer pixel shifts avoid reference resampling.
    token = json.dumps(asset + "#object=Hero Panel")
    points = (
        f'int b=addpoint(0, {{1.375,0,0}}); setpointattrib(0,"instance",b,{token}); setpointattrib(0,"index",b,7.5);'
    )
    if args.packed_reference:
        freeze_packed("object", "Hero Panel", 1.0)
        xform = geo.createNode("xform")
        xform.setInput(0, on_disk)
        xform.parm("tx").set(-0.5)
        mixed_sop = geo.createNode("merge")
        mixed_sop.setNextInput(xform)
        mixed_sop.setNextInput(sop)
        mixed_sop.setDisplayFlag(True)
        mixed_sop.setRenderFlag(True)
        sop.parm("snippet").set(points)
    else:
        sop.parm("snippet").set(
            "int a=addpoint(0, {-.5,0,0}); "
            f'setpointattrib(0,"instance",a,{token}); setpointattrib(0,"index",a,1.0); ' + points
        )
    session.wait(session.apply(native_scene.capture(rop)))
    target = dest / ("two-offsets-" + args.device + variant + ".exr")
    session.write_exr(target)
    previous = pixels(target)
    left = pixels(dest / f"blender-{args.device}-object-1.exr")
    right = pixels(dest / f"blender-{args.device}-object-7.5.exr")
    combined = np.zeros_like(left)
    combined[:, :-16] += left[:, 16:]
    combined[:, 44:] += right[:, :-44]
    mask = (combined[:, :, 3] > 0.01) | (previous[:, :, 3] > 0.01)
    mixed = float(np.sqrt(np.mean((combined[mask] - previous[mask]) ** 2)))
    assert mixed < 0.018, mixed
    report["two_simultaneous_frames_rgba_rmse"] = mixed
    hou.hipFile.save(str(example))
    assert not any(n.userData("cycles_external_blend") for n in root.children())
    import cycles_houdini_offline

    assert not any("/cycles_blend_prototypes/" in record["path"] for record in cycles_houdini_offline.inspect())
    session.wait(session.apply(native_scene.capture(rop)))
    assert not session.status()["error"]
    repair = dest / ("after-save-" + args.device + variant + ".exr")
    session.write_exr(repair)
    assert np.allclose(pixels(repair), previous, atol=0.012), "Saving HIP broke Blender reference restoration"
if args.packed_reference:
    from unittest.mock import patch

    # Scene load, SOP cook and viewport bounds must work on a machine without Blender.
    with patch.object(
        blender_assets, "_blender", side_effect=AssertionError("Blender started on scene open")
    ) as loader:
        hou.hipFile.load(str(example), suppress_save_prompt=True)
        geo = hou.node("/obj/blend_points")
        box = geo.renderNode().geometry().boundingBox()
        assert box.isValid(), box
        loader.assert_not_called()
    rop = hou.node("/out/blend_instances")
    with ch.Session(device=device, width=128, height=128, samples=128, execution="worker", threads=4) as session:
        session.wait(session.apply(native_scene.capture(rop)))
        assert not session.status()["error"], session.status()
        reopened = dest / ("after-reopen-" + args.device + variant + ".exr")
        session.write_exr(reopened)
        assert np.allclose(pixels(reopened), previous, atol=0.012)
        # A mirrored packed primitive must mirror the texture together with the mesh.
        xform = geo.renderNode().input(0)
        xform.setDisplayFlag(True)
        xform.setRenderFlag(True)
        xform.setParms(dict(tx=0, sx=-1))
        session.wait(session.apply(native_scene.capture(rop)))
        mirrored = dest / ("negative-scale-" + args.device + variant + ".exr")
        session.write_exr(mirrored)
        actual = pixels(mirrored)
        reference = left[:, ::-1]
        mask = (actual[:, :, 3] > 0.01) | (reference[:, :, 3] > 0.01)
        rms = float(np.sqrt(np.mean((actual[mask] - reference[mask]) ** 2)))
        assert rms < 0.018, rms
        report["negative_scale_foreground_rmse"] = rms
        report["hip_reopened"] = True

        from cycles_houdini import blend_worker

        # Two serialized primitives and 32 shortcut points request one cold subframe.
        packed = geo.node("packed_blender")
        packed.setParms(dict(target_kind="object", target="Hero Panel", frame=3.25))
        points = geo.node("point_reference")
        points.parm("snippet").set(
            "for (int i=0; i<32; i++) {int p=addpoint(0,set(i,3,0)); "
            f'setpointattrib(0,"instance",p,{token}); setpointattrib(0,"index",p,3.25);' + "}"
        )
        copies = geo.createNode("merge")
        copies.setNextInput(packed)
        copies.setNextInput(packed)
        copies.setNextInput(points)
        copies.setDisplayFlag(True)
        copies.setRenderFlag(True)
        with patch.object(blend_worker, "evaluate_many", wraps=blend_worker.evaluate_many) as evaluate:
            capture = native_scene.capture(rop)
            assert not any("unavailable" in issue["code"] for issue in capture["source_diagnostics"])
            frames = [req["frame"] for call in evaluate.call_args_list for req in call.args[2]]
            assert frames == [3.25], frames
            session.wait(session.apply(capture))
            session.wait(session.apply(native_scene.capture(rop)))
            assert evaluate.call_count == 1
            report["shared_frame_evaluations"] = frames

        missing = geo.createNode("cycles::blender_reference", "missing_blender_asset")
        missing.parm("frame").deleteAllKeyframes()
        missing.setParms(dict(file="$HIP/missing.blend", target="Missing", frame=1))
        copies.setNextInput(missing)
        capture = native_scene.capture(rop)
        diagnostics = capture["source_diagnostics"]
        assert any("missing.blend" in issue["message"] for issue in diagnostics), diagnostics
        session.wait(session.apply(capture))
        assert not session.status()["error"], session.status()
        report["missing_reference_reported"] = True
report["result"] = "passed"
(dest / ("comparison-" + args.device + variant + ".json")).write_text(json.dumps(report, indent=2))
