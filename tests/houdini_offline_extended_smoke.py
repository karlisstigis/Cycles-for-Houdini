# SPDX-License-Identifier: Apache-2.0
"""Separate-process extended offline recovery and material-heavy save evidence.

All scenes are created here, never taken from an artist path. Run prepare,
missing, recover with independent isolated preferences, and performance with
the plugin. The saved scene deliberately contains no Viewer or Cycles ROP.
"""

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

import hou

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=("prepare", "missing", "recover", "performance"))
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
block = "cycles.offline.graphs"
original = args.output / "extended-original.hiplc"
resaved = args.output / "extended-offline-save-as.hiplc"


def load(path):
    try:
        hou.hipFile.load(str(path), suppress_save_prompt=True)
    except hou.LoadWarning:
        pass
    except hou.OperationFailed as error:
        lines = [line.strip() for line in str(error).splitlines() if line.strip().startswith("Error:")]
        if (
            args.mode != "missing"
            or "Bad node type found: cycles::" not in str(error)
            or not lines
            or any("Bad parent found (parent is not a network)" not in line for line in lines)
        ):
            raise
        assert block in hou.node("/obj").dataBlockKeys("")


def digest():
    return hashlib.sha256(hou.node("/obj").dataBlock(block)).hexdigest()


def graph(root):
    return [
        dict(
            path=root.relativePathTo(n),
            type=n.type().name(),
            templates=n.parmTemplateGroup().asDialogScript(),
            parms={p.name(): dict(raw=p.rawValue(), keys=[k.asCode() for k in p.keyframes()]) for p in n.parms()},
            links=[[c.inputIndex(), root.relativePathTo(c.inputNode()), c.outputIndex()] for c in n.inputConnections()],
        )
        for n in sorted((root, *root.allSubChildren()), key=lambda n: n.path())
    ]


def rich_material(ch, name):
    mat = ch.create_material(name=name, shader="emission")
    # The default this test was written for (new nodes now take Blender's).
    mat.node("surface_shader").setParms(dict(strength=10.0))
    outer = ch.create_subnet(mat, "outer")
    inner = ch.create_subnet(outer, "inner")
    ramp = inner.createNode("cycles::color_ramp::1", "palette")
    ramp.parm("ramp").set(
        hou.Ramp(
            (hou.rampBasis.Linear, hou.rampBasis.Constant, hou.rampBasis.Linear),
            (0.0, 0.35, 1.0),
            ((0.2, 0.4, 0.8), (0.8, 0.2, 0.1), (0.1, 0.9, 0.4)),
        )
    )
    ch.promote_parameter(mat, ramp, "ramp", name="promoted_palette")
    factor = inner.createNode("cycles::value::1", "external_factor")
    factor.parm("value").setExpression('ch("/obj/external_controls/tx") + $F * .001')
    ramp.setNamedInput("factor", factor, "value")
    terminal = ch.create_connector(inner, "color", kind="output", type="color")
    terminal.setInput(0, ramp)
    outer_terminal = ch.create_connector(outer, "color", kind="output", type="color")
    outer_terminal.setNamedInput(outer_terminal.inputNames()[0], inner, "color")
    shader = mat.node("surface_shader")
    shader.setNamedInput("color", outer, "color")
    ch.promote_parameter(mat, shader, "strength", name="animated_strength")
    for frame, value in ((1, 1.25), (10, 2.5)):
        key = hou.Keyframe()
        key.setFrame(frame)
        key.setValue(value)
        mat.parm("animated_strength").setKeyframe(key)
    return mat


def render(ch, material):
    from cycles_houdini import native_scene
    from cycles_houdini.device_selection import set_devices

    rop = hou.node("/out").createNode("cycles_ipr", "transient_recovery_render")
    try:
        set_devices(rop, "CPU")
        rop.setParms(
            dict(
                soppath="/obj/recovery_receiver/grid",
                camera="/obj/recovery_camera",
                materialpath=material.path(),
                resx=32,
                resy=32,
                samples=8,
                seed=1701,
                animate_seed=0,
                use_denoise=0,
                adaptive_sampling=0,
                viewer_interactive=0,
            )
        )
        rop.parmTuple("background").set((0, 0, 0))
        rop.parmTuple("lightstrength").set((0, 0, 0))
        with ch.Session(device="CPU", width=32, height=32, samples=8) as session:
            frame = session.wait(session.apply(native_scene.capture(rop)))
            assert not session.status().get("source_diagnostics"), session.status()
            pixels = bytes(frame["planes"]["combined"]["pixels"])
            values = array("f")
            values.frombytes(pixels)
            assert all(math.isfinite(v) for v in values), "nonfinite reference render"
            measured = values[(16 * 32 + 16) * 4 : (16 * 32 + 16) * 4 + 3]
            # Independent authored ramp interpolation at frame 1, multiplied
            # by the promoted emission strength. This rejects black/default
            # frames as well as a ROP that ignores the review-copy override.
            expected = (0.7075, 0.3475, 0.46625)
            assert all(abs(a - b) < 2e-5 for a, b in zip(measured, expected)), (
                material.path(),
                list(measured),
                expected,
            )
            return pixels
    finally:
        rop.destroy()


if args.mode == "prepare":
    import cycles_houdini as ch
    import cycles_houdini_offline as offline

    assert offline._installed
    hou.hipFile.clear(suppress_save_prompt=True)
    control = hou.node("/obj").createNode("null", "external_controls")
    control.parm("tx").set(0.2125)
    mat = rich_material(ch, "extended_surface")
    ch.create_material(name="extended_volume", shader="principled_volume")
    ch.create_light_material(name="extended_light")
    ch.create_world_material(name="extended_world")
    obj = hou.node("/obj").createNode("geo", "recovery_receiver", run_init_scripts=False)
    grid = obj.createNode("grid", "grid")
    grid.setParms(dict(type=0, orient=0, rows=2, cols=2))
    grid.parmTuple("size").set((8, 8))
    grid.setRenderFlag(True)
    obj.parm("shop_materialpath").set(mat.path())
    instance = hou.node("/obj").createNode("instance", "recovery_instance")
    instance.parm("instancepath").set(obj.path())
    camera = hou.node("/obj").createNode("cam", "recovery_camera")
    camera.setParms(dict(tz=5, projection=1, orthowidth=4))
    # An existing healthy HIP receives its first protected ordinary Save As.
    offline._busy = True
    try:
        hou.hipFile.save(str(args.output / "existing-unprotected.hiplc"), save_to_recent_files=False)
    finally:
        offline._busy = False
    load(args.output / "existing-unprotected.hiplc")
    mat = hou.node("/mat/extended_surface")
    expected = {n.name(): graph(n) for n in hou.node("/mat").children()}
    (args.output / "extended-expected.json").write_text(json.dumps(expected, indent=2))
    before = offline.statistics()
    for index in range(10):
        hou.node("/obj/recovery_camera").parm("tx").set(index * 0.01)
    assert offline.statistics() == before
    hou.node("/obj/recovery_camera").parm("tx").set(0)
    hou.hipFile.save(str(original), save_to_recent_files=False)
    assert len(offline.inspect()) == 4, offline.inspect()
    before_load = offline.statistics()
    load(original)
    assert offline.statistics() == before_load, "healthy load must not serialize"
    assert all("healthy graph" in row["status"] for row in offline.inspect())
    assert {n.name(): graph(n) for n in hou.node("/mat").children()} == expected
    pixels = render(ch, hou.node("/mat/extended_surface"))
    (args.output / "reference.rgba.f32").write_bytes(pixels)
    result = dict(
        result="passed",
        archive=digest(),
        pixel_sha256=hashlib.sha256(pixels).hexdigest(),
        statistics=offline.statistics(),
        no_saved_cycles_rop=not hou.node("/out").children(),
        graph_nodes={name: len(rows) for name, rows in expected.items()},
    )
elif args.mode == "missing":
    assert hou.nodeType(hou.vopNodeTypeCategory(), "cycles::material::1") is None
    load(original)
    before = digest()
    hou.node("/obj/recovery_receiver").parm("tx").set(9.125)
    # Deliberate replacement keeps the same path but lacks the archived UUID.
    hou.node("/mat/extended_world").destroy()
    hou.node("/mat").createNode("null", "extended_world")
    # Deliberate deletion must not be recreated when Cycles returns.
    hou.node("/mat/extended_light").destroy()
    # Offline authored metadata on a damaged placeholder must survive too.
    hou.node("/mat/extended_surface").setComment("offline artist note")
    hou.node("/mat/extended_surface").setUserData("artist_keep", "edited offline")
    for _ in range(2):
        hou.hipFile.save(str(resaved), save_to_recent_files=False)
        load(resaved)
    assert digest() == before
    result = dict(result="passed", archive=digest(), save_as=str(resaved))
elif args.mode == "recover":
    import cycles_houdini as ch
    import cycles_houdini_offline as offline

    load(resaved)
    before = digest()
    expected = json.loads((args.output / "extended-expected.json").read_text())
    identities = {n.path(): n.sessionId() for n in hou.node("/mat").children()}
    statuses = offline.inspect()
    assert any("missing" in r["status"] for r in statuses), statuses
    assert any("identity differs" in r["status"] for r in statuses), statuses
    recovered = {}
    for row in statuses:
        review = hou.node("/obj").createNode("matnet", "review_" + row["id"][:8])
        copy = offline.recover_copy(row["id"], review)
        actual = graph(copy)
        (args.output / (copy.name() + "-review.json")).write_text(json.dumps(actual, indent=2))
        assert actual == expected[copy.name()], copy.path()
        recovered[copy.name()] = copy
    assert identities == {n.path(): n.sessionId() for n in hou.node("/mat").children()}
    assert hou.node("/mat/extended_surface").comment() == "offline artist note"
    assert hou.node("/mat/extended_surface").userData("artist_keep") == "edited offline"
    assert hou.node("/mat/extended_light") is None
    assert hou.node("/obj/recovery_receiver").evalParm("tx") == 9.125
    assert hou.node("/obj/recovery_receiver").evalParm("shop_materialpath") == "/mat/extended_surface"
    assert hou.node("/obj/recovery_instance").evalParm("instancepath") == "/obj/recovery_receiver"
    # Restore only the unrelated transform for the independent fixed-seed check.
    hou.node("/obj/recovery_receiver").parm("tx").set(0)
    pixels = render(ch, recovered["extended_surface"])
    assert pixels == (args.output / "reference.rgba.f32").read_bytes(), "fixed-seed pixel mismatch"
    hou.node("/obj/recovery_receiver").parm("tx").set(9.125)
    duplicate = ch.create_material(name="identity_conflict")
    surface_id = next(row["id"] for row in statuses if row["path"] == "/mat/extended_surface")
    surface_status = next(row["status"] for row in statuses if row["id"] == surface_id)
    duplicate.setUserData(offline.MARKER, surface_id)
    # A healthy root carrying another root's identity is a copy: it gets its own
    # identity, while the root at the archived path keeps the identity, its status
    # and its archive record (byte for byte while it waits for review).
    rows = {row["id"]: row for row in offline.inspect()}
    assert duplicate.userData(offline.MARKER) != surface_id
    assert hou.node("/mat/extended_surface").userData(offline.MARKER) == surface_id
    assert rows[surface_id]["status"] == surface_status, rows[surface_id]
    retained = offline._read()
    hou.hipFile.save(str(args.output / "extended-reviewed.hiplc"), save_to_recent_files=False)
    saved = offline._read()
    assert saved[0][surface_id]["path"] == "/mat/extended_surface"
    assert saved[0][surface_id]["nodes"] == retained[0][surface_id]["nodes"]
    if surface_id in offline._unresolved:
        assert saved[1][surface_id] == retained[1][surface_id]
    assert duplicate.userData(offline.MARKER) in saved[0], "the separated copy is protected as a new graph"
    result = dict(
        result="passed",
        archive=before,
        copied=len(recovered),
        originals_untouched=True,
        fixed_seed_pixels_equal=True,
        pixel_sha256=hashlib.sha256(pixels).hexdigest(),
        deletion_replacement_and_artist_metadata_preserved=True,
        ambiguous_clone_archive_preserved=True,
        statistics=offline.statistics(),
    )
else:
    import cycles_houdini as ch
    import cycles_houdini_offline as offline

    hou.hipFile.clear(suppress_save_prompt=True)
    control = hou.node("/obj").createNode("null", "external_controls")
    control.parm("tx").set(0.2125)
    for index in range(96):
        rich_material(ch, "heavy_" + str(index))
    timings, sizes = {}, {}
    for protected in (False, True):
        label = "protected" if protected else "unprotected"
        samples = []
        offline._busy = not protected
        try:
            for index in range(3):
                path = args.output / (label + "-" + str(index) + ".hiplc")
                start = time.perf_counter()
                hou.hipFile.save(str(path), save_to_recent_files=False)
                samples.append(time.perf_counter() - start)
                sizes[label] = path.stat().st_size
        finally:
            offline._busy = False
        timings[label] = dict(samples=samples, median=statistics.median(samples))
    assert len(offline.inspect()) == 96
    result = dict(
        result="passed",
        materials=96,
        authored_nodes=sum(1 + len(n.allSubChildren()) for n in hou.node("/mat").children()),
        seconds=timings,
        hip_bytes=sizes,
        archive_bytes=len(hou.node("/obj").dataBlock(block)),
        statistics=offline.statistics(),
        overhead_seconds=timings["protected"]["median"] - timings["unprotected"]["median"],
        overhead_bytes=sizes["protected"] - sizes["unprotected"],
    )
(args.output / ("extended-" + args.mode + ".json")).write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
