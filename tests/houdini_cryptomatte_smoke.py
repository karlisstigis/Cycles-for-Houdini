# SPDX-License-Identifier: Apache-2.0
"""Independent hashes, matte extraction, native naming/update and EXR regressions."""

from support.paths import artifacts_root
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import os
from pathlib import Path
import struct
import sys
import traceback
import hou
import numpy as np
import OpenImageIO as OIIO
import cycles_houdini as ch
from cycles_houdini import materials, native_scene, object_properties
from cycles_houdini.passes import catalogue

destination = artifacts_root() / "stage3/passes/cryptomatte"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-cryptomatte-smoke.json"
device = sys.argv[sys.argv.index("--device") + 1] if "--device" in sys.argv else "CPU"
report = dict(result="running", device=device, execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), checks=[])


def murmur32(name):
    # Independent MurmurHash3 x86_32, seed zero; operates on UTF-8, not Python chars.
    data = name.encode("utf-8")
    h = 0
    rotate = lambda value, n: ((value << n) | (value >> (32 - n))) & 0xFFFFFFFF

    def block(k):
        return (rotate(k * 0xCC9E2D51 & 0xFFFFFFFF, 15) * 0x1B873593) & 0xFFFFFFFF

    full = len(data) // 4 * 4
    for offset in range(0, full, 4):
        h ^= block(int.from_bytes(data[offset : offset + 4], "little"))
        h = (rotate(h, 13) * 5 + 0xE6546B64) & 0xFFFFFFFF
    if full != len(data):
        h ^= block(int.from_bytes(data[full:], "little"))
    h ^= len(data)
    h ^= h >> 16
    h = h * 0x85EBCA6B & 0xFFFFFFFF
    h ^= h >> 13
    h = h * 0xC2B2AE35 & 0xFFFFFFFF
    h ^= h >> 16
    return h


def id_bits(name):
    h = murmur32(name)
    if h >> 23 & 255 in (0, 255):
        h ^= 1 << 23
    return h


def plane(frame, name):
    value = frame["planes"][name]
    return np.frombuffer(value["pixels"], np.float32).reshape(frame["height"], frame["width"], value["channels"])


def matte(frame, family, name):
    result = np.zeros((frame["height"], frame["width"]), np.float32)
    for pass_name in sorted(p for p in frame["planes"] if p.startswith(family)):
        pixels = plane(frame, pass_name)
        for offset in (0, 2):
            result += (pixels[:, :, offset].copy().view(np.uint32) == id_bits(name)) * pixels[:, :, offset + 1]
    return result


def manifests(metadata):
    result = {}
    for key, layer in metadata.items():
        if not key.startswith("cryptomatte/") or not key.endswith("/name"):
            continue
        prefix = key[:-4]
        assert key.split("/")[1] == f"{murmur32(layer):08x}"[:7]
        assert metadata[prefix + "hash"] == "MurmurHash3_32" and metadata[prefix + "conversion"] == "uint32_to_float32"
        names = json.loads(metadata[prefix + "manifest"])
        assert all(value == f"{murmur32(name):08x}" for name, value in names.items())
        result[layer] = names
    return result


def assert_exr(filename, frame, metadata):
    image = OIIO.ImageInput.open(str(filename))
    assert image
    spec = image.spec()
    data = np.asarray(image.read_image(format=OIIO.FLOAT))
    assert spec.get_string_attribute("compression") in ("zip", "zips")
    for key, value in metadata.items():
        assert spec.get_string_attribute(key) == value
    for name in frame["planes"]:
        if not name.startswith("Crypto"):
            continue
        indices = [spec.channelnames.index(name + "." + c) for c in "RGBA"]
        assert all(
            str(spec.channelformats[index] if spec.channelformats else spec.format) == "float" for index in indices
        )
        # Comparing raw bits catches ID corruption even when float magnitudes are tiny.
        assert np.array_equal(
            data[:, :, indices].copy().view(np.uint32), np.flipud(plane(frame, name)).copy().view(np.uint32)
        )
    image.close()


def native_cop_masks(filename, frame, names):
    geo = hou.node("/obj").createNode("geo", "crypto_compositor", run_init_scripts=False)
    net = geo.createNode("copnet")
    reader = net.createNode("file", "rendered_mattes")
    pass_fixture.set_parameters(reader, dict(filename=str(filename), colorspace="raw"))
    pass_fixture.parm(reader, "addaovs").pressButton()
    for family, name in names:
        decoder = net.createNode("cryptomatte", family)
        passes = sorted(p for p in frame["planes"] if p.startswith(family))
        for index, pass_name in enumerate(passes):
            decoder.setInput(index, reader, reader.outputNames().index(pass_name))
        pass_fixture.parm(decoder, "selection").set(name)
        layer = decoder.layer(0)
        actual = np.frombuffer(layer.allBufferElements(), np.float32).reshape(frame["height"], frame["width"])
        expected = matte(frame, family, name)
        assert np.allclose(actual, expected, atol=2e-5), (
            family,
            float(np.abs(actual - expected).max()),
            layer.channelCount(),
        )
        layer.close()
        # Viewer selection must be able to discover the EXR manifest too.
        pass_fixture.parm(decoder, "populateselection").pressButton()
        assert name in pass_fixture.parm(decoder, "selection").eval()
        pass_fixture.parm(decoder, "selection").set(name)
    net.layoutChildren()


try:
    assert murmur32("") == 0 and murmur32("foo") == 0xF6A5C420 and murmur32("hello") == 0x248BFA47
    scene = hou.node("/obj").createNode("subnet", "cryptomatte_fixture")
    objects = []
    for name, size, position in (("rear", (3, 2, 0.2), (0, 0, 0)), ("front", (1.2, 1.2, 0.1), (-0.5, 0, 0.5))):
        obj = scene.createNode("geo", name, run_init_scripts=False)
        if name == "front":
            box = obj.createNode("grid")
            pass_fixture.set_parameters(box, dict(type=0, orient=0, rows=2, cols=2))
            box.parmTuple("size").set(size[:2])
        else:
            box = obj.createNode("box")
            box.parmTuple("size").set(size)
        box.setRenderFlag(True)
        obj.parmTuple("t").set(position)
        object_properties.install(obj)
        pass_fixture.parm(obj, "cycles_cryptomatte_asset").set("Courtyard")
        builder = materials.create_material(name="crypto_" + name, shader="emission")
        builder.node("surface_shader").parmTuple("color").set((0.7, 0.2, 0.05))
        pass_fixture.parm(builder.node("surface_shader"), "strength").set(1)
        pass_fixture.parm(obj, "shop_materialpath").set(builder.path())
        objects.append((obj, box, builder))
    rear, front = objects[0][0], objects[1][0]
    builder = objects[1][2]
    transparent = builder.createNode("cycles::transparent_bsdf::1")
    mix = builder.createNode("cycles::mix_closure::1")
    pass_fixture.parm(mix, "fac").set(0.75)
    closure = lambda node: next(
        name for name, type in zip(node.outputNames(), node.outputDataTypes()) if type == "bsdf"
    )
    mix.setNamedInput("closure1", builder.node("surface_shader"), closure(builder.node("surface_shader")))
    mix.setNamedInput("closure2", transparent, closure(transparent))
    builder.node("surface_output").setNamedInput("surface", mix, closure(mix))
    camera = hou.node("/obj").createNode("cam", "crypto_camera")
    pass_fixture.set_parameters(camera, dict(tz=5, projection=1, orthowidth=4))
    rop = hou.node("/out").createNode("cycles_ipr", "crypto_output")
    set_rop_devices(rop, device)
    pass_fixture.set_parameters(
        rop,
        dict(
            source_mode="objects",
            objectroot=scene.path(),
            camera=camera.path(),
            resx=96,
            resy=64,
            samples=256,
            crypto_object=True,
            crypto_material=True,
            crypto_asset=True,
            crypto_levels=6,
        ),
    )
    for descriptor in catalogue():
        parm = pass_fixture.parm(rop, "pass_" + descriptor["id"])
        if parm:
            parm.set(False)
    with ch.Session(
        device=device, width=96, height=64, samples=256, passes=["combined"], collect_diagnostics=True
    ) as session:

        def render():
            return session.wait(session.apply(native_scene.capture(rop)))

        frame = render()
        metadata = session.output_metadata()
        manifest = manifests(metadata)
        assert set(manifest) == {"CryptoObject", "CryptoMaterial", "CryptoAsset"}
        assert rear.path() in manifest["CryptoObject"] and front.path() in manifest["CryptoObject"]
        assert all(builder.path() in manifest["CryptoMaterial"] for _, _, builder in objects)
        assert "Courtyard" in manifest["CryptoAsset"]
        front_mask = matte(frame, "CryptoObject", front.path())
        rear_mask = matte(frame, "CryptoObject", rear.path())
        assert np.allclose(front_mask[29:35, 31:37], 0.25, atol=0.07), front_mask[29:35, 31:37]
        assert np.allclose(rear_mask[29:35, 31:37], 0.75, atol=0.07)
        assert np.allclose(rear_mask[28:36, 60:68], 1, atol=1e-6)
        assert np.allclose(matte(frame, "CryptoAsset", "Courtyard"), front_mask + rear_mask, atol=1e-5)
        assert np.allclose(matte(frame, "CryptoMaterial", objects[1][2].path()), front_mask, atol=1e-5)
        assert np.count_nonzero(front_mask[:3, :3] + rear_mask[:3, :3]) == 0
        converted = session.status()["geometry_conversions"]
        rebuilt = session.status()["displacement_rebuilds"]
        assert len([p for p in frame["planes"] if p.startswith("Crypto")]) == 9
        for value in session.frame_info()["pass_metadata"].values():
            if value.get("cryptomatte"):
                assert value["storage"] == "float32" and not value["color"] and not value["denoise_eligible"]
        filename = destination / "independent-mattes.exr"
        session.write_exr(str(filename))
        assert_exr(filename, frame, metadata)
        native_cop_masks(
            filename,
            frame,
            [("CryptoObject", front.path()), ("CryptoMaterial", objects[1][2].path()), ("CryptoAsset", "Courtyard")],
        )
        report["checks"].append(
            "Independent UTF-8 MurmurHash3 and bit conversion; transparent object/material coverage; grouped assets; float32 ZIP EXR bit-exact IDs and manifests; native Copernicus decoder and manifest selection"
        )
        renamed = 'Front "quoted" \\ courtyard\n雪'
        pass_fixture.parm(front, "cycles_cryptomatte_object").set(renamed)
        pass_fixture.parm(front, "cycles_cryptomatte_asset").set("Foreground")
        changed = render()
        changed_meta = session.output_metadata()
        manifest = manifests(changed_meta)
        assert renamed in manifest["CryptoObject"] and front.path() not in manifest["CryptoObject"]
        assert np.allclose(matte(changed, "CryptoObject", renamed), front_mask, atol=1e-6)
        assert np.allclose(matte(changed, "CryptoAsset", "Foreground"), front_mask, atol=1e-6)
        assert (
            session.status()["geometry_conversions"] == converted
            and session.status()["displacement_rebuilds"] == rebuilt
        )
        # Motion and camera changes preserve names; pass settings preserve geometry.
        generation = session.status()["generation"]
        for invalid in (
            dict(levels=1),
            dict(levels=17),
            dict(levels=True),
            dict(levels=6.5),
            dict(object=1),
            dict(unknown=True),
        ):
            try:
                session.apply(dict(cryptomatte=invalid))
            except (RuntimeError, ValueError):
                pass
            else:
                raise AssertionError("Invalid Cryptomatte settings accepted: " + repr(invalid))
            assert session.status()["generation"] == generation
        for levels in (2, 3, 16):
            pass_fixture.parm(rop, "crypto_levels").set(levels)
            current = render()
            assert len([p for p in current["planes"] if p.startswith("Crypto")]) == 3 * ((levels + 1) // 2)
            assert np.allclose(matte(current, "CryptoObject", renamed), front_mask, atol=1e-6)
        # Exercise both exponent repairs with rendered IDs, rather than only the helper.
        for exponent in (0, 255):
            name = next(
                "exponent_" + str(i) for i in range(10000) if murmur32("exponent_" + str(i)) >> 23 & 255 == exponent
            )
            pass_fixture.parm(front, "cycles_cryptomatte_object").set(name)
            repaired = render()
            assert np.allclose(matte(repaired, "CryptoObject", name), front_mask, atol=1e-6)
        pass_fixture.parm(front, "cycles_cryptomatte_object").set(renamed)
        pass_fixture.parm(rop, "crypto_levels").set(6)
        hou.setFps(24)
        pass_fixture.parm(front, "tx").setExpression("$T*12-.5", hou.exprLanguage.Hscript)
        capture = native_scene.capture(rop)
        capture["motion"] = dict(enabled=True, fps=24.0, shutter_frames=1.0, offset=0.0, samples=3)
        moving = session.wait(session.apply(capture))
        blurred = matte(moving, "CryptoObject", renamed)
        assert np.count_nonzero(blurred[:, 52:]) > 0
        assert abs(blurred.sum() / front_mask.sum() - 1) < 0.08
        assert renamed in manifests(session.output_metadata())["CryptoObject"]
        pass_fixture.parm(front, "tx").deleteAllKeyframes()
        pass_fixture.parm(front, "tx").set(-0.5)
        raw = render()
        denoised = session.wait(session.apply(dict(denoising=dict(enabled=True, method="oidn", use_gpu=False))))
        for name in raw["planes"]:
            if name.startswith("Crypto"):
                assert np.array_equal(plane(raw, name), plane(denoised, name))
        report["checks"].append(
            "Live object/asset naming and levels retain geometry; escaped Unicode manifests and repaired hash exponents; transform-blur coverage; malformed settings preserve generation; ID planes bypass denoising"
        )
        # Progressive preview, native multi-plane delivery and completed-output metadata.
        generation = session.apply(dict(denoising=dict(enabled=False), rendering=dict(samples=1048576)))
        session.set_preview_pass("CryptoObject00")
        import time

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = session.status()
            assert status["state"] != "failed", status
            info = session.frame_info()
            if (
                info
                and info["generation"] == generation
                and info.get("current_scene", True)
                and "CryptoObject00" in info["passes"]
                and not info["complete"]
            ):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("No progressive Cryptomatte preview")
        assert session.output_metadata() == {}, "Manifest transferred on an incomplete progressive update"
        preview = session.display_image(pass_name="CryptoObject00")
        assert (
            np.count_nonzero(
                np.frombuffer(preview["rgba"], np.uint8).reshape(preview["height"], preview["width"], 4)[:, :, :3]
            )
            > 0
        )
        session.open_native_display("null:")
        deadline = time.monotonic() + 30
        expected = set(session.frame_info()["enabled_passes"])
        while time.monotonic() < deadline:
            current = session.frame_info()
            if current and set(current["passes"]) == expected:
                session.present_native()
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Native viewer did not receive every Cryptomatte plane")
        session.close_native_display()
        session.cancel()
        final = render()
        session.write_exr(str(destination / "native-preview-final.exr"))
        assert_exr(destination / "native-preview-final.exr", final, session.output_metadata())
        assert session.status()["geometry_conversions"] == converted
        report["checks"].append(
            "Progressive false-color preview and native image planes; no progressive manifest transport; clean cancellation/restart without geometry recopy"
        )
    assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
    pass_fixture.set_parameters(rop, dict(trange=0, picture=str(destination / "native-batch.exr"), samples=16))
    hip = destination / "crypto-authoring.hiplc"
    hou.hipFile.save(str(hip))
    hou.hipFile.load(str(hip), suppress_save_prompt=True)
    hou.node("/out/crypto_output").render(frame_range=(1, 1), ignore_inputs=True)
    image = OIIO.ImageInput.open(str(destination / "native-batch.exr"))
    assert image
    spec = image.spec()
    batch_meta = {p.name: str(p.value) for p in spec.extra_attribs if p.name.startswith("cryptomatte/")}
    assert (
        renamed in manifests(batch_meta)["CryptoObject"]
        and "/mat/crypto_front" in manifests(batch_meta)["CryptoMaterial"]
    )
    assert "CryptoObject02.R" in spec.channelnames
    image.close()
    report["checks"].append("Native ROP/SOHO batch outputs and stable artist paths/overrides survive HIP reload")
    report["result"] = "passed"
except Exception:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(report_path)
