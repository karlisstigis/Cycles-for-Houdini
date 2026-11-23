# SPDX-License-Identifier: Apache-2.0
"""Convert & Append in Houdini 22: every Blender attribute kind arrives, and curves and points
without a radius render as thick as in Link mode.

    hython tests/houdini_blend_converted_attributes_smoke.py [OUT] [--blender EXE] [--device CPU]

1. Blender (the bridge's executable) runs tests/blend_all_attributes_fixture.py: a .blend with one
   attribute per domain and data type on a mesh, a Curves object and a point cloud, vertex groups,
   UV maps and colour attributes with odd names, names that collide with Houdini's, material
   slots and an instance-domain attribute; its own values go to a JSON file.
2. The collection is imported Converted (Save to Disk, Without hierarchy) into a saved scene; the
   Blender File node's packed primitives are opened per object and every attribute is compared
   with blend_all_attributes_fixture.expectations: owner, type, size, name, type info
   (Quaternion, Matrix) and every value. Attributes that cannot be carried (edge attributes, the
   instance attribute) must be named in one import-log line per object, with the reason.
3. The collection "DefaultRadius" (poly curves and points without a radius attribute) is imported
   in Link mode and Converted and rendered at 96 x 96 with the same close camera; the covered
   pixels (alpha) of the curves and of the points must agree (the ROP's own fallbacks, point radius 0.05
   and curve width 0.001, would differ by factors of 5 and 10).
Prints a JSON summary (also OUT/converted_attributes_smoke.json) and exits non-zero on failure.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import hou

from cycles_houdini.blender import blend_convert
from cycles_houdini.blender import blend_file_node as fn

HERE = Path(__file__).resolve().parent
SIZE = 96
CHECKS = {}


def check(name):
    def wrap(function):
        started = time.time()
        try:
            value = function()
            CHECKS[name] = dict(ok=True, value=value)
        except Exception as error:  # noqa: BLE001 - every failure goes into the summary
            CHECKS[name] = dict(ok=False, error=repr(error)[:4000], trace=traceback.format_exc()[-3000:])
        CHECKS[name]["seconds"] = round(time.time() - started, 2)
        print(("PASS " if CHECKS[name]["ok"] else "FAIL ") + name)
        return function

    return wrap


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fixture_module():
    sys.path.insert(0, str(HERE))
    try:
        import blend_all_attributes_fixture as fixture
    finally:
        sys.path.remove(str(HERE))
    return fixture


def make_blend(blender, blend, report):
    from cycles_houdini.blender.blend_worker import blender_environment

    completed = subprocess.run(
        [
            blender,
            "-b",
            "--factory-startup",
            "--python",
            str(HERE / "blend_all_attributes_fixture.py"),
            "--",
            str(blend),
            str(report),
        ],
        timeout=300,
        capture_output=True,
        text=True,
        env=blender_environment(hou.getenv("CYCLES_HOUDINI_BLENDER_OCIO") or None),
    )
    require("ALL_ATTRIBUTES_DONE" in completed.stdout, completed.stdout[-3000:] + completed.stderr[-3000:])
    return json.loads(Path(report).read_text(encoding="utf-8"))


def cooked(node):
    node.cook(force=True)
    require(not node.errors(), f"{node.path()}: {node.errors()}")
    return node.geometry()


def per_object(geo):
    """{object name: embedded geometry} of the Blender File node's packed primitives."""
    result = {}
    for prim in geo.prims():
        name = prim.attribValue("name") if geo.findPrimAttrib("name") else ""
        if hasattr(prim, "getEmbeddedGeometry"):
            result[name.rsplit("/", 1)[-1]] = prim.getEmbeddedGeometry()
    return result


_KINDS = {hou.attribData.Float: "float", hou.attribData.Int: "int", hou.attribData.String: "string"}


def lookup_in(objects):
    def lookup(obj, name):
        geo = objects.get(obj)
        if geo is None:
            return None
        for owner, find in (
            ("point", geo.findPointAttrib),
            ("prim", geo.findPrimAttrib),
            ("vertex", geo.findVertexAttrib),
        ):
            attrib = find(name)
            if attrib is None:
                continue
            if owner == "point":
                elements = geo.points()
            elif owner == "prim":
                elements = geo.prims()
            else:
                elements = [vertex for prim in geo.prims() for vertex in prim.vertices()]
            values = []
            for element in elements:
                value = element.attribValue(attrib)
                values.append(list(value) if isinstance(value, (tuple, list)) else [value])
            return owner, _KINDS.get(attrib.dataType()), attrib.size(), attrib.qualifier(), values
        return None

    return lookup


def all_names(geo):
    return {
        attrib.name()
        for attribs in (geo.pointAttribs(), geo.primAttribs(), geo.vertexAttribs(), geo.globalAttribs())
        for attrib in attribs
    }


def drain():
    from cycles_houdini.blender import blend_catalogue

    for _ in range(600):
        if blend_catalogue.drain_catalogues():
            return
        time.sleep(0.05)


def render(container, device):
    import numpy as np

    from cycles_houdini import native_scene, registry
    from cycles_houdini.device_selection import set_devices
    from cycles_houdini.scene import light_properties

    obj = hou.node("/obj")
    cam = obj.node("radius_cam")
    if cam is None:
        # Above the assets looking down: Blender (x, y) is Houdini (x, -z); 0.216 units across.
        cam = obj.createNode("cam", "radius_cam")
        cam.parmTuple("t").set((0.0, 0.3, 0.0))
        cam.parmTuple("r").set((-90.0, 0.0, 0.0))
        cam.parm("focal").set(50.0)
        cam.parm("aperture").set(36.0)
        cam.parm("resx").set(SIZE)
        cam.parm("resy").set(SIZE)
        env = obj.createNode("envlight", "radius_black_world")
        light_properties.install(env)
        env.parm("light_intensity").set(0.0)
    rop = hou.node("/out").createNode("cycles_ipr")
    set_devices(rop, device)
    rop.setParms(
        dict(
            source_mode="objects",
            objectroot="/obj",
            vobject=container.path(),
            camera=cam.path(),
            lights="*",
            world_source="scene",
            resx=SIZE,
            resy=SIZE,
            samples=4,
            ipr_samples=4,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
            transparent_background=1,
        )
    )
    try:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        require(not status["error"], status["error"])
        plane = frame["planes"]["combined"]
        pixels = np.frombuffer(plane["pixels"], np.float32).reshape(SIZE, SIZE, plane["channels"])
    finally:
        registry.close_all()
        rop.destroy()
    return pixels.copy()


def coverage(pixels):
    """Covered pixels (alpha: Converted point clouds get no material, so their colour is black)
    of each image half (the points lie in one half, the curves in the other)."""
    mask = pixels[..., 3] > 0.5
    half = SIZE // 2
    return mask, dict(first=int(mask[:half].sum()), second=int(mask[half:].sum()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # Positional: scripts/run-hython.ps1 (PowerShell) takes "--out" for one of its own parameters.
    parser.add_argument(
        "out", type=Path, nargs="?", default=Path(tempfile.mkdtemp(prefix="cycles_converted_attributes_"))
    )
    parser.add_argument("--blender", help="Blender executable (default: the Cycles bridge discovery)")
    parser.add_argument("--device", default="CPU")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["CYCLES_HOUDINI_ASSET_CACHE"] = str(out / "cache")
    hou.putenv("CYCLES_HOUDINI_ASSET_CACHE", str(out / "cache"))
    fixture = fixture_module()
    summary = dict(houdini=hou.applicationVersionString(), out=str(out))
    blender = args.blender or fn.blender_executable()
    (out / "scene").mkdir(exist_ok=True)
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.hipFile.save(str(out / "scene" / "attributes.hip"))
    library = out / "library"
    library.mkdir(exist_ok=True)
    blend = library / "all_attributes.blend"
    expected = make_blend(blender, blend, library / "all_attributes.json")
    summary["blender"] = expected.get("blender_version")

    @check("all_attributes_converted")
    def _():
        lines = []
        [container] = blend_convert.import_blend(
            str(blend),
            [("collection", "AllAttributes")],
            storage="files",
            structure="packed",
            materials="none",
            executable=blender,
            report=lambda text, error=False: lines.append(text),
        )
        node = container.node("blender_file")
        objects = per_object(cooked(node))
        require({"AttrMesh", "AttrCurves", "AttrPoints"} <= set(objects), sorted(objects))
        rows, absent, groups, not_imported = fixture.expectations(expected)
        failures = fixture.compare(rows, lookup_in(objects))
        for obj, names in absent.items():
            geo = objects.get(obj)
            present = all_names(geo) & set(names) if geo is not None else set()
            if present:
                failures.append(f"{obj}: must not carry {sorted(present)}")
        for obj, wanted in groups.items():
            geo = objects[obj]
            for name, members in wanted["prims"].items():
                group = geo.findPrimGroup(name)
                got = sorted(prim.number() for prim in group.prims()) if group else None
                if got != members:
                    failures.append(f"{obj}: prim group {name} {got}, expected {members}")
            for name, pairs in wanted["edges"].items():
                group = geo.findEdgeGroup(name)
                got = (
                    sorted(sorted(point.number() for point in edge.points()) for edge in group.edges())
                    if group
                    else None
                )
                if got != sorted(sorted(pair) for pair in pairs):
                    failures.append(f"{obj}: edge group {name} {got}, expected {pairs}")
        parsed = dict(entry for entry in (fixture.parse_line(line) for line in lines) if entry)
        if parsed != not_imported:
            failures.append(f"import log lines {parsed}, expected {not_imported}")
        table = []
        lookup = lookup_in(objects)
        for row in rows:
            found = lookup(row["object"], row["name"])
            table.append([row["object"], row["name"]] + (list(found[:4]) if found else ["missing"]))
        require(not failures, "\n".join(failures[:60]))
        return dict(rows=len(rows), table=table, log=[line for line in lines if "not imported" in line])

    @check("default_radius_link_parity")
    def _():
        import numpy as np

        common = dict(structure="packed", materials="link", executable=blender, report=lambda *a, **k: None)
        # One at a time: only the rendered import exists in the scene.
        [link] = blend_convert.import_blend(
            str(blend), [("collection", "DefaultRadius")], storage="reference", **common
        )
        drain()
        cooked(link.node("blender_file"))
        link_pixels = render(link, args.device)
        link.destroy()
        [converted] = blend_convert.import_blend(
            str(blend), [("collection", "DefaultRadius")], storage="files", **common
        )
        objects = per_object(cooked(converted.node("blender_file")))
        widths = sorted({round(v, 6) for v in objects["RadiusCurves"].pointFloatAttribValues("width")})
        pscales = sorted({round(v, 6) for v in objects["RadiusPoints"].pointFloatAttribValues("pscale")})
        require(widths == [0.01] and pscales == [0.01], (widths, pscales))
        converted_pixels = render(converted, args.device)
        np.save(out / "link.npy", link_pixels)
        np.save(out / "converted.npy", converted_pixels)
        require(link_pixels[0, 0, 3] < 0.5 and converted_pixels[0, 0, 3] < 0.5, "the background must be transparent")
        link_mask, link_counts = coverage(link_pixels)
        converted_mask, converted_counts = coverage(converted_pixels)
        union = int((link_mask | converted_mask).sum())
        iou = float((link_mask & converted_mask).sum()) / max(union, 1)
        result = dict(link=link_counts, converted=converted_counts, iou=round(iou, 3))
        for half in ("first", "second"):
            a, b = link_counts[half], converted_counts[half]
            require(a > 20 and b > 20, result)
            require(0.8 <= b / a <= 1.25, result)
        require(iou > 0.75, result)
        return result

    failed = [name for name, value in CHECKS.items() if not value["ok"]]
    summary.update(checks=CHECKS, failed=failed, result="passed" if not failed else "failed")
    text = json.dumps(summary, indent=1, default=repr, ensure_ascii=False)
    (out / "converted_attributes_smoke.json").write_text(text, encoding="utf-8")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
        code = 1
    try:  # stop Blender workers before the hard exit (os._exit skips atexit)
        from cycles_houdini.blender import blend_worker

        blend_worker.shutdown_all()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    sys.stdout.flush()
    # Houdini's own shutdown, as the parity and integration smokes (os._exit crashes in exit handlers).
    hou.exit(code, suppress_save_prompt=True)
    os._exit(code)
