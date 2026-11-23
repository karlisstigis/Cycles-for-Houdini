# SPDX-License-Identifier: Apache-2.0
"""Scenes saved with the retired RGBA Color Ramp storage load into the inline
Color and Alpha ramps, preserving authored controls and stock257 evaluation.

tests/fixtures/legacy-rgba-color-ramps.hiplc and its .json were written by the
last RGBA-storage build (host 20261029T061546223) with
artifacts/ramp-inline-20261029/make_legacy_fixture.py: native bases with
animated alpha and an animated position, LAB, HSV/HSL with dormant bases and
coincident stops, every Blender interpolation, a fresh node, a shared
reference and a retired whole-RGBA material promotion.
"""

from support.paths import artifacts_root
import argparse, hashlib, json, os, shutil, struct, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import ramps, ramp_editor
from hue_ramp_fixture import Fixture
from ramp_table_reference import native_lookup

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
reference = json.loads((root / "tests/fixtures/legacy-rgba-color-ramps.json").read_text())
tables = json.loads((root / "tests/fixtures/legacy-rgba-color-ramps-tables.json").read_text())
assert (
    tables["legacy_fixture_sha256"]
    == hashlib.sha256((root / "tests/fixtures/legacy-rgba-color-ramps.json").read_bytes()).hexdigest()
)
dest = artifacts_root() / "stage3/materials/color-ramp-migration"
dest.mkdir(parents=True, exist_ok=True)
report = dict(result="running", device=device, scope=__doc__, checks=[], max_preview_error=0.0, max_render_error=0.0)
session = None
BASIS = ("constant", "linear", "catmull-rom", "monotonecubic", "bezier", "bspline", "hermite")
HOU_BASES = (
    hou.rampBasis.Constant,
    hou.rampBasis.Linear,
    hou.rampBasis.CatmullRom,
    hou.rampBasis.MonotoneCubic,
    hou.rampBasis.Bezier,
    hou.rampBasis.BSpline,
    hou.rampBasis.Hermite,
)


def expected_samples(name, frame):
    """Read frozen controls through independent public host evaluators."""
    saved = reference["expected"][name][str(frame)]
    result = []
    for output, source in zip(("color", "alpha"), saved["sources"]):
        if source[1] != 1:
            result.append(np.asarray(tables["cases"][name][str(frame)][output], np.float32))
            continue
        value = lambda bits: struct.unpack("f", struct.pack("i", bits))[0]
        stops = [source[i : i + 5] for i in range(8, len(source), 5)]
        values = [tuple(value(v) for v in stop[2:]) if source[4] else value(stop[2]) for stop in stops]
        ramp = hou.Ramp(
            tuple(HOU_BASES[stop[0]] for stop in stops), tuple(value(stop[1]) for stop in stops), tuple(values)
        )
        if source[4]:
            ramp.setColorType((hou.colorType.RGB, hou.colorType.HSV, hou.colorType.LAB)[source[3]])
        result.append(np.asarray([native_lookup(ramp, factor) for factor in reference["factors"]], np.float32))
    return np.column_stack(result)


def load():
    scene = dest / "legacy.hiplc"
    shutil.copyfile(root / "tests/fixtures/legacy-rgba-color-ramps.hiplc", scene)
    try:
        hou.hipFile.load(str(scene), suppress_save_prompt=True)
    except hou.LoadWarning as warning:
        report.setdefault("load_warnings", []).append(str(warning))
    return hou.node(reference["material"])


def compare(node, frames=None, label=""):
    for frame, want in reference["expected"][node.name()].items():
        if frames and int(frame) not in frames:
            continue
        sources = ramps.preview_sources(node, hou.frameToTime(int(frame)))
        assert sources == tuple(want["sources"]), (label, node.name(), frame, sources, want["sources"])
        data = ch._native.ramp_preview(*sources, tuple(reference["factors"]), dict(raw=True))
        assert not data["color_issue"] and not data["alpha_issue"], (
            node.name(),
            frame,
            data["color_issue"],
            data["alpha_issue"],
        )
        linear = np.frombuffer(data["linear"], np.float32).reshape(-1, 4)
        expected = expected_samples(node.name(), frame)
        error = float(np.max(np.abs(linear - expected)))
        report["max_preview_error"] = max(report["max_preview_error"], error)
        assert error <= 1e-6, (label, node.name(), frame, error)


try:
    mat = load()
    assert not any("ramp" in w.lower() for w in report.get("load_warnings", [])), report.get("load_warnings")
    for name in reference["expected"]:
        node = mat.node(name)
        assert len(node.parmTuple("ramp1c")) == 3 and node.parm("ramp1ca") is None and node.parm("edit_ramp") is None, (
            name
        )
        if name not in ("shared", "promoted"):
            assert node.parm("alpha_ramp").evalAsInt() == node.parm("ramp").evalAsInt(), name
            for i in range(1, node.parm("ramp").evalAsInt() + 1):
                assert node.parm(f"alpha_ramp{i}pos").eval() == node.parm(f"ramp{i}pos").eval(), (name, i)
        compare(node, label="migrated")
    report["checks"].append(
        "Every legacy node preserves its frozen controls and matches independent stock257 references at frames 1, 4 and 10"
    )
    native = mat.node("native_rgb")
    assert (
        native.parm("alpha_ramp2value").expression() == ".5+.05*$F"
        and native.parm("alpha_ramp3pos").expression() == ".5+.01*$F"
    )
    assert len(native.parm("alpha_ramp3value").keyframes()) == 2
    assert [native.parm(f"alpha_ramp{i}interp").evalAsString() for i in range(1, 6)] == [
        native.parm(f"ramp{i}interp").evalAsString() for i in range(1, 6)
    ]
    assert all(
        mat.node(n).parm(f"alpha_ramp{i}interp").evalAsString() == "linear"
        for n in ("hsv_far", "hsl_ccw")
        for i in range(1, mat.node(n).parm("alpha_ramp").evalAsInt() + 1)
    )
    assert mat.node("fresh").parm("alpha_ramp").evalAsRamp().values() == (0.0, 1.0)
    assert "native_rgb/alpha_ramp" in mat.node("shared").parm("alpha_ramp").expression()
    report["checks"].append(
        "Alpha animation, animated positions and stop bases migrate; HSV/HSL Alpha becomes Linear; shared references share Alpha"
    )
    # Retired whole-RGBA promotion: renders unchanged, then its saved button
    # converts it to inline ramps without changing the result.
    assert (
        mat.parm("palette").parmTemplate().type() == hou.parmTemplateType.Folder
        and mat.parm("palette_edit") is not None
    )
    compare(mat.node("promoted"), label="retired promotion")
    ramp_editor.edit(mat.parm("palette"))
    assert (
        mat.parm("palette").parmTemplate().type() == hou.parmTemplateType.Ramp
        and mat.parm("palette_alpha").parmTemplate().parmType() == hou.rampParmType.Float
    )
    assert mat.parm("palette_edit") is None and mat.parm("palette3ca") is None
    assert (
        mat.parm("palette_alpha3value").expression() == 'ch("palette_alpha2value")*.5'
        and mat.parm("palette_alpha2value").expression() == ".2+.05*$F"
    )
    promoted = mat.node("promoted")
    assert ramps.ColorRampControls(promoted).alpha_storage() == mat.parm("palette_alpha")
    compare(promoted, label="converted promotion")
    report["checks"].append(
        "A retired RGBA material promotion renders unchanged and converts to inline Color and Alpha ramps exactly"
    )
    # Saving migrated scenes writes only the new layout.
    saved = dest / "migrated.hiplc"
    hou.hipFile.save(str(saved))
    text = saved.read_bytes().decode("latin-1")
    assert "ramp1ca" not in text and "alpha_ramp1value" in text
    hou.hipFile.load(str(saved), suppress_save_prompt=True)
    mat = hou.node(reference["material"])
    for name in reference["expected"]:
        compare(mat.node(name), label="resaved")
    report["checks"].append("Resaved migrated scene reloads identically")
    # Rendered outputs preserve the legacy controls under the stock257 policy.
    hou.setFrame(1)
    f = Fixture(device, "blender-rgba-ramps.json")
    assert f.factors == reference["factors"]
    copies = {
        node.name(): node
        for node in hou.copyNodesTo(
            [
                mat.node(n)
                for n in (
                    "native_rgb",
                    "lab",
                    "hsv_far",
                    "hsl_ccw",
                    "ease",
                    "cardinal",
                    "b_spline",
                    "constant",
                    "shared",
                )
            ],
            f.mat,
        )
    }
    session = ch.Session(
        device=device,
        width=f.width,
        height=f.height,
        samples=1,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined", "position", "normal"],
    )
    for name, node in copies.items():
        node.setNamedInput("factor", f.attribute, "fac")
        expected = expected_samples(name, 1)
        for output, values in (("color", expected[:, :3]), ("alpha", np.repeat(expected[:, 3:4], 3, axis=1))):
            f.emission.setNamedInput("color", node, output)
            frame = session.wait(session.apply(f.update()), timeout=120)
            status = session.status()
            assert not status["error"] and not status["source_diagnostics"], (
                name,
                output,
                status["source_diagnostics"],
            )
            error = f.inspect(frame, values)
            report["max_render_error"] = max(report["max_render_error"], error)
            report["checks"].append(dict(label="Rendered " + output + " " + name, error=error))
    session.close(wait=True)
    closed = session.status()
    assert closed["device_allocation_bytes"] == closed["owned_geometry_bytes"] == 0
    report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if session:
        session.close(wait=True)
    (dest / "report.json").write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        dict(
            result=report["result"],
            checks=len(report["checks"]),
            max_preview_error=report["max_preview_error"],
            max_render_error=report["max_render_error"],
        )
    )
)
