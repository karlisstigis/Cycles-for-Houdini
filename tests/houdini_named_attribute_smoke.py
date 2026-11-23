# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from subdivision_test_setup import configure_render, set_subdivision
import argparse, json, os, subprocess
from pathlib import Path
import hou
import numpy as np
import OpenImageIO as oiio
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from named_attribute_fixture import Fixture, CASES

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
f = Fixture(device)
entry = registry.for_rop(f.rop, native_scene.configuration(f.rop))
events = []
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
contracts = subprocess.run(
    [
        str(runtime_executable("ch_harness.exe")),
        "--resources",
        os.environ["CYCLES_HOUDINI_RESOURCES"],
        "--device",
        device,
        "--named-attribute-contracts",
    ],
    capture_output=True,
    text=True,
    timeout=60,
)
assert contracts.returncode == 0, (contracts.stdout, contracts.stderr)
contract_report = json.loads(contracts.stdout.strip().splitlines()[-1])
assert contract_report["rejected_inputs"] == 58 and contract_report["sampled_wire_roundtrip"], contract_report


def apply():
    return entry.session.wait(entry.apply(native_scene.capture(f.rop), hou.time()))


def clone(node):
    g = hou.Geometry()
    g.merge(node.geometry())
    return g


try:
    for case in CASES:
        f.configure(case)
        baseline = f.check(apply(), entry.session.status())
        for broken in (True, False):
            f.parm.set(f.field if broken else "")
            f.check(apply(), entry.session.status(), broken, baseline)
            events.append(dict(case=case, broken=broken, diagnostics=entry.session.status()["source_diagnostics"]))
        # A camera-only transaction never reconverts any of these streams.
        before = entry.session.status()["geometry_conversions"]
        entry.session.wait(entry.apply(dict(camera_path=f.camera.path()), hou.time()))
        assert entry.session.status()["geometry_conversions"] == before
        if case == "rest":
            # Independently author the expected field through the established Cd
            # path, including varying point values and interpolated hair keys.
            authored = [clone(node) for node in f.stashes]
            for node, g in zip(f.stashes, authored):
                ref = hou.Geometry()
                ref.merge(g)
                cd = ref.addAttrib(hou.attribType.Point, "Cd", (0.0, 0.0, 0.0))
                for point in ref.points():
                    point.setAttribValue(cd, point.attribValue("rest"))
                node.parm("stash").set(ref)
            f.source.parm("attribute").set("Cd")
            reference = f.planes(apply())
            assert np.allclose(reference["combined"], baseline["combined"], atol=2e-5), (
                "Named gradient differs from independent Cd authoring"
            )
            for node, g in zip(f.stashes, authored):
                node.parm("stash").set(g)
            f.source.parm("attribute").set("rest")
            f.check(apply(), entry.session.status(), False, baseline)
            events.append(dict(check="Varying mesh/point/curve fields match independent Cd authoring"))
    # Requested non-finite/type-incompatible data remains a local source problem.
    f.configure("vector")
    baseline = f.check(apply(), entry.session.status())
    original = clone(f.stashes[0])
    for value in (float("nan"), "bad type"):
        g = hou.Geometry()
        g.merge(original)
        g.findPointAttrib("paint").destroy()
        a = g.addAttrib(hou.attribType.Point, "paint", value)
        if isinstance(value, float):
            for point in g.points():
                point.setAttribValue(a, value)
        f.stashes[0].parm("stash").set(g)
        f.check(apply(), entry.session.status(), True)
        f.stashes[0].parm("stash").set(original)
        f.check(apply(), entry.session.status(), False, baseline)
        events.append(dict(check="Non-finite/type change and repair", value=str(value)))
    # Irrelevant broken fields and disconnected demands do not stop or warn.
    unused = f.material.createNode("cycles::attribute::1", "disconnected")
    unused.parm("attribute").set("not_numeric")
    f.check(apply(), entry.session.status(), False, baseline)
    for name in ("", "x" * 257):
        f.source.parm("attribute").set(name)
        frame = apply()
        status = entry.session.status()
        pixels = f.planes(frame)
        assert status["state"] != "failed" and status["source_diagnostics"], status
        assert all(issue["source"].startswith("/mat/") for issue in status["source_diagnostics"]), status[
            "source_diagnostics"
        ]
        visible = pixels["combined"][:, :, 3] > 0.999
        assert np.allclose(pixels["combined"][visible, :3], (1, 0, 1), atol=2e-5)
    f.source.parm("attribute").set("paint")
    f.check(apply(), entry.session.status(), False, baseline)
    events.append(dict(check="Empty/oversized shader field name recovers and repairs"))
    # Shader-name edits demand a new geometry field even if no SOP data ID changed.
    f.source.parm("attribute").set("new_paint")
    frame = apply()
    status = entry.session.status()
    assert status["state"] != "failed" and status["source_diagnostics"]
    originals = [clone(node) for node in f.stashes]
    for node, g in zip(f.stashes, originals):
        updated = hou.Geometry()
        updated.merge(g)
        updated.addAttrib(hou.attribType.Point, "new_paint", (0.2, 0.6, 0.3))
        node.parm("stash").set(updated)
    f.check(apply(), entry.session.status(), False, baseline)
    for node, g in zip(f.stashes, originals):
        node.parm("stash").set(g)
    f.source.parm("attribute").set("paint")
    f.check(apply(), entry.session.status(), False, baseline)
    events.append(dict(check="Missing new field repaired without changing the assignment"))
    # A vertex-owned field renders correctly. HOM may move an existing same-name
    # point field, so restore the original owned geometry after this fixture edit.
    g = clone(f.stashes[0])
    g.addAttrib(hou.attribType.Vertex, "paint", (0.2, 0.6, 0.3))
    f.stashes[0].parm("stash").set(g)
    f.check(apply(), entry.session.status(), False, baseline)
    f.stashes[0].parm("stash").set(original)
    f.check(apply(), entry.session.status(), False, baseline)
    # Both native subdivision schemes interpolate the same constant named field.
    for scheme in ("linear", "catmull_clark"):
        configure_render(f.rop, dict(subdivision=dict(type=scheme, space="object", dicing_rate=0.1, max_level=2)))
        f.check(apply(), entry.session.status())
        events.append(dict(check="Subdivision " + scheme))
    set_subdivision(f.rop, type="none")
    # Warm missing/valid binding layouts reuse a bounded shader population.
    counts = []
    for n in range(12):
        f.parm.set(f.field if n % 2 else "")
        f.check(apply(), entry.session.status(), bool(n % 2))
        counts.append(entry.session.status()["engine_shader_resources"])
    assert max(counts[4:]) - min(counts[4:]) <= 1, counts
    f.parm.set("")
    # Native packed prototypes preserve demanded fields without flattening.
    packed = f.objects[0].createNode("pack", "pack_named_fields")
    packed.setInput(0, f.deletes[0])
    packed.setDisplayFlag(True)
    packed.setRenderFlag(True)
    f.check(apply(), entry.session.status())
    assert entry.session.status()["packed_instances"] == 1
    f.deletes[0].setDisplayFlag(True)
    f.deletes[0].setRenderFlag(True)
    packed.destroy()
    # A missing color field preserves independent displacement, and a missing
    # height field only removes its displacement branch. Other objects stay valid.
    displaced = f.material.copyTo(hou.node("/mat"))
    displaced.setName("named_displacement", unique_name=True)
    height = displaced.createNode("cycles::displacement::1", "height")
    height.setParms(dict(midlevel=0, scale=1, space="object"))
    mask = displaced.createNode("cycles::attribute::1", "height_field")
    mask.parm("attribute").set("mask")
    height.setNamedInput("height", mask, "fac")
    displaced.node("surface_output").setNamedInput("displacement", height, "displacement")
    displaced.parm("cycles_displacement_method").set("true")
    f.objects[0].parm("shop_materialpath").set(displaced.path())
    for field in ("", "paint", "mask", ""):
        f.parm.set(field)
        frame = apply()
        status = entry.session.status()
        pixels = f.planes(frame)
        visible = (pixels["combined"][:, :, 3] > 0.999) & (pixels["position"][:, :, 0] < -0.9)
        assert visible.sum() > 20 and np.isfinite(pixels["position"]).all()
        expected_height = 0 if field == "mask" else 0.35
        assert np.allclose(pixels["position"][visible, 2], expected_height, atol=1e-5)
        expected_color = (1, 0, 1) if field == "paint" else (0.2, 0.6, 0.3)
        assert np.allclose(pixels["combined"][visible, :3], expected_color, atol=2e-5)
        assert bool(status["source_diagnostics"]) == bool(field), status
    f.objects[0].parm("shop_materialpath").set(f.material.path())
    displaced.destroy()
    f.check(apply(), entry.session.status())
    events.append(dict(check="Packed field preservation and independent color/displacement failures"))
    # Direct final and ordered actual ROP/SOHO sequence use the same field policy.
    with ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=32,
        execution="sync",
        interactive=False,
        passes=["combined", "position", "normal"],
    ) as final:
        for broken in (False, True, False):
            f.parm.set("paint" if broken else "")
            f.check(final.wait(final.apply(native_scene.capture(f.rop), at_time=hou.time())), final.status(), broken)
    f.parm.setExpression('"paint" if hou.frame() in (2,4) else ""', hou.exprLanguage.Python)
    output = f.dest / "frames"
    output.mkdir(exist_ok=True)
    configure_render(f.rop, dict(trange=1, f1=1, f2=5, f3=1, picture=str(output / "fields.$F4.exr")))
    f.rop.render()
    for n in range(1, 6):
        reader = oiio.ImageInput.open(str(output / f"fields.{n:04d}.exr"))
        assert reader
        issues = json.loads(reader.spec().get_string_attribute("cycles_houdini:source_diagnostics") or "[]")
        data = reader.read_image()
        reader.close()
        assert np.isfinite(data).all() and data.max() > 0.1
        assert bool(issues) == (n in (2, 4)), (n, issues)
    f.parm.deleteAllKeyframes()
    f.parm.set("")
    f.rop.parm("trange").set(0)
    hou.setFrame(1)
    hou.hipFile.save(str(f.dest / "named-attributes.hiplc"))
finally:
    registry.close_all()
report = dict(
    result="passed",
    device=device,
    events=events,
    sequence_frames=5,
    warm_shader_counts=counts,
    contracts=contract_report,
)
path = artifacts_root() / "stage3/materials/named-attributes/houdini-named-attribute-smoke.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
