# SPDX-License-Identifier: Apache-2.0
"""Compile a read-only HDK probe and check synthetic hull GT instancing; no Blender or renderer."""

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import hou
import cycles_houdini as ch
from cycles_houdini.blender import blend_bounds, blend_catalogue, blend_preview, blend_worker, node_bounds


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--cmake", type=Path)
args = parser.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]
build = out / "probe-build"
project = out / "probe-project"
project.mkdir(exist_ok=True)
project.joinpath("CMakeLists.txt").write_text(
    "cmake_minimum_required(VERSION 3.24)\n"
    "project(blender_viewport_gt_probe LANGUAGES CXX)\n"
    "find_package(Houdini REQUIRED)\n"
    f'add_library(probe SHARED "{root.as_posix()}/tests/support/blender_viewport_gt_probe.cpp")\n'
    "target_compile_features(probe PRIVATE cxx_std_17)\n"
    "target_link_libraries(probe PRIVATE Houdini)\n"
    'houdini_configure_target(probe INSTDIR "${CMAKE_BINARY_DIR}/bin")\n'
    "foreach(configuration DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)\n"
    'set_target_properties(probe PROPERTIES RUNTIME_OUTPUT_DIRECTORY_${configuration} "${CMAKE_BINARY_DIR}/bin" '
    'LIBRARY_OUTPUT_DIRECTORY_${configuration} "${CMAKE_BINARY_DIR}/bin" '
    'ARCHIVE_OUTPUT_DIRECTORY_${configuration} "${CMAKE_BINARY_DIR}/lib")\n'
    "endforeach()\n",
    encoding="utf-8",
)
cmake = str(args.cmake) if args.cmake else shutil.which("cmake")
if not cmake:
    bundled = Path(
        "C:/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/Common7/IDE/"
        "CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
    )
    cmake = str(bundled) if bundled.is_file() else None
assert cmake, "Install CMake to compile the read-only HDK contract probe"
with out.joinpath("compile.log").open("w", encoding="utf-8") as log:
    for command in (
        [
            cmake,
            "-S",
            str(project),
            "-B",
            str(build),
            "-G",
            "Visual Studio 17 2022",
            "-A",
            "x64",
            "-DCMAKE_PREFIX_PATH=" + str(Path(hou.getenv("HFS")) / "toolkit/cmake"),
        ],
        [cmake, "--build", str(build), "--config", "Release", "--parallel", "2"],
    ):
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
library = ctypes.CDLL(str(build / "bin/probe.dll"))
probe = library.probe_blender_viewport_gt
probe.argtypes = [ctypes.c_char_p, ctypes.c_double, ctypes.c_int, ctypes.c_int]
probe.restype = ctypes.c_char_p


def forbidden(*values, **options):
    raise AssertionError("Synthetic GT contract must not launch Blender")


blend_worker.evaluate_many = forbidden
blend_worker.catalogue_many = forbidden
blend_catalogue.request_catalogues = lambda *values, **options: None
hou.hipFile.setName(str(out / "owned-fixture.hip"))
asset = out / "owned-fixture.blend"
asset.write_bytes(b"Synthetic metadata only; never opened by Blender")
stat = asset.stat()
versions = {str(asset): [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]}
obj = hou.node("/obj").createNode("geo", "viewport_gt_contract", run_init_scripts=False)
sources = []
for index, name in enumerate(("A", "B")):
    node = obj.createNode("cycles::blender_reference", "reference_" + name)
    node.parm("ref_frame1").deleteAllKeyframes()
    node.setParms(
        dict(
            file=str(asset),
            ref_target1=name,
            ref_target_kind1="object",
            ref_frame1=1,
            shape_frame=1,
            viewport_shape="hull",
            viewport_wireframe=0,
        )
    )
    node.cook(force=True)
    reference = node.geometry().prim(0).intrinsicValue("cycles_blender_reference")
    record = dict(
        schema_version=1,
        known=True,
        source=str(asset),
        selector_type="OBJECT",
        selector=name,
        source_identity=hashlib.sha256(name.encode()).hexdigest(),
        bounds=[0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
        dependency_versions=versions,
        frame=1.0,
    )
    blend_bounds.publish_record(reference, 1.0, record)
    base = json.loads(ch.load().serialized_blender_reference_bounds(reference, 1.0))
    preview = dict(
        base,
        max_hull_vertices=64,
        viewport_hulls=dict(
            max_vertices=64,
            whole=dict(
                points=[0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                triangles=[0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3],
            ),
        ),
    )
    preview["viewport_hulls"]["members"] = [preview["viewport_hulls"]["whole"]]
    assert blend_preview.valid_record(preview), preview
    ch.load().restore_blender_reference_preview(reference, 1.0, preview["source_identity"])
    blend_preview.persist(node, [preview])
    named = obj.createNode("attribwrangle", "name_" + name)
    named.setInput(0, node)
    named.parm("class").set(1)
    named.parm("snippet").set(f's@name="{name}";')
    sources.append((node, named))
merged = obj.createNode("merge")
for index, (_, named) in enumerate(sources):
    merged.setInput(index, named)
points = obj.createNode("attribwrangle", "placements")
points.parm("class").set(0)
points.parm("snippet").set(
    "for(int i=0;i<64;i++){int p=addpoint(0,set(i%8,(i/8)%8,@Frame*.01));"
    'setpointattrib(0,"name",p,i%2?"A":"B");'
    'setpointattrib(0,"Cd",p,set(i/64.,.25,.75));'
    'setpointattrib(0,"scale",p,set(i%3?-1.:1.,.7,1.4));'
    'setpointattrib(0,"orient",p,quaternion(.37*i,{0,1,0}));}'
)
copies = obj.createNode("copytopoints::2.0")
copies.setInput(0, merged)
copies.setInput(1, points)
copies.setParms(
    dict(pack=0, idattrib="name", targetattribs=1, useapply1=1, applyto1=0, applymethod1=0, applyattribs1="name Cd")
)
rows = []
for wire in (0, 1):
    for node, _ in sources:
        node.parm("viewport_wireframe").set(wire)
    for matching in (1, 0):
        copies.parm("useidattrib").set(matching)
        for frame in (1, 18, 1, 2, 3, 4):
            hou.setFrame(frame)
            node_bounds.drain_pending_node_bounds()
            geometry = copies.geometry()
            assert len(geometry.prims()) == (64 if matching else 128)
            for primitive in geometry.prims():
                assert primitive.intrinsicValue("cycles_blender_viewport_hull_points") == 4
            value = json.loads(probe(copies.path().encode(), hou.time(), 1, -1))
            assert value["batches"] == value["inner_sources"] == value["inner_prototypes"] == 2, value
            for key in ("entries", "unique_offsets", "names", "colors", "primitive_ids", "vertex_ids"):
                assert value[key] == len(geometry.prims()), (key, value)
            assert value["errors"] == 0 and value["matrix_error"] < 1e-6, value
            rows.append(dict(wire=wire, matching=matching, frame=frame, **value))
        for viewport, lod in ((0, -1), (1, 0), (1, 2), (1, 3)):
            value = json.loads(probe(copies.path().encode(), hou.time(), viewport, lod))
            assert value["inner_prototypes"] == 0, (viewport, lod, value)
            rows.append(dict(wire=wire, matching=matching, viewport=viewport, override_lod=lod, **value))
report = dict(
    result="passed",
    runtime=str(ch.__file__),
    rows=rows,
    scope="GT structure and update-source ownership; actual drawing requires the GUI gate",
)
out.joinpath("report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
