# SPDX-License-Identifier: Apache-2.0
"""Native images: independently specified colors, coordinates and resource edits."""

from support.paths import artifacts_root
from cycles_houdini.device_selection import set_devices as set_rop_devices
from array import array
from copy import deepcopy
import argparse
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
destination = Path(os.environ.get("CYCLES_HOUDINI_TEST_OUTPUT_ROOT", artifacts_root() / "stage3/textures"))
destination.mkdir(parents=True, exist_ok=True)
geo = hou.node("/obj").createNode("geo", "texture_receiver", run_init_scripts=False)
grid = geo.createNode("grid")
grid.parm("type").set(0)
grid.parm("orient").set(0)
grid.parm("rows").set(2)
grid.parm("cols").set(2)
grid.parmTuple("size").set((8, 8))
# The native XY Grid faces -Z. This reference explicitly faces the camera at +Z;
# its positive scalar displacement must follow that independently stated normal.
outward = geo.createNode("reverse", "positive_z_receiver")
outward.setInput(0, grid)
assert np.allclose(tuple(outward.geometry().prims()[0].normal()), (0, 0, 1))
uv = geo.createNode("attribwrangle")
uv.setInput(0, outward)
uv.parm("snippet").set("v@uv=set(@P.x/8+0.5,@P.y/8+0.5,0);")
camera = hou.node("/obj").createNode("cam", "texture_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)


def image(path, color, size=8, format=OIIO.FLOAT):
    pixels = np.asarray(color, dtype=np.float32)
    if pixels.ndim == 1:
        pixels = np.broadcast_to(pixels, (size, size, len(color))).copy()
    spec = OIIO.ImageSpec(pixels.shape[1], pixels.shape[0], pixels.shape[2], format)
    output = OIIO.ImageOutput.create(str(path))
    assert output, OIIO.geterror()
    assert output.open(str(path), spec), output.geterror()
    assert output.write_image(pixels), output.geterror()
    assert output.close(), output.geterror()
    # Explicitly advance metadata even on filesystems with coarse write clocks.
    metadata = path.stat()
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 10_000_000))


def node(id, type, **parameters):
    return dict(id=id, definition=f"cycles/{type}/1", parameters=parameters)


def endpoint(id, socket):
    return dict(node=id, socket=socket)


def link(a, output, b, input):
    return {"from": endpoint(a, output), "to": endpoint(b, input)}


def material(path, colorspace="data", tiles=None, **parameters):
    texture = node(
        "image",
        "image_texture",
        filename=str(path),
        colorspace=colorspace,
        interpolation="closest",
        alpha_type="channel_packed",
        **parameters,
    )
    if tiles is not None:
        texture["parameters"]["tiles"] = tiles
    return dict(
        key="surface",
        kind="graph",
        graph=dict(
            schema_version=1,
            nodes=[texture, node("emission", "emission", strength=1.0)],
            links=[link("image", "color", "emission", "color")],
            surface=endpoint("emission", "emission"),
        ),
    )


def plane(frame, name):
    value = frame["planes"][name]
    data = array("f")
    data.frombytes(value["pixels"])
    return np.asarray(data).reshape((64, 64, value["channels"]))


def center(frame):
    return plane(frame, "combined")[32, 32, :3]


def expect(frame, color, tolerance=1e-5):
    measured = center(frame)
    assert np.allclose(measured, color, rtol=0, atol=tolerance), (measured, color)
    return measured.tolist()


def expect_texture_change(session):
    # The persistent worker publishes file-watch state asynchronously. Do not
    # require a cached notification in the same instruction as the file write.
    deadline = time.monotonic() + 5
    while not session.textures_changed() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.textures_changed(), session.status()


def check_source_recovery(session, update, message):
    if message == "require integers":
        previous = session.status()
        before = previous.get("committed_generation", previous["generation"])
        try:
            session.apply(update)
        except RuntimeError as error:
            assert message in str(error), str(error)
        else:
            raise AssertionError("Malformed tile-ID API payload was accepted")
        current = session.status()
        assert current.get("committed_generation", current["generation"]) == before
        return
    session.wait(session.apply(update))
    status = session.status()
    assert not status["error"], status
    if message == "requires corner UVs":
        assert any(issue["code"] == "material.missing_uv" for issue in status["source_diagnostics"]), status
    else:
        assert any(message in issue["message"] for issue in status["source_diagnostics"]), status


parser = argparse.ArgumentParser()
parser.add_argument("--device", default=None)
args = parser.parse_args()
reports = []
for device in [args.device] if args.device else dict.fromkeys(("CPU", ch.default_device())):
    label = "cpu" if device == "CPU" else "optix"
    checks = []
    path = destination / (label + "-linear.exr")
    image(path, (0.2, 0.4, 0.8))
    request = ch.geometry_request(geo, uv, material="surface")
    with ch.Session(
        device=device, width=64, height=64, samples=32, threads=2, collect_diagnostics=True, use_texture_cache=False
    ) as session:
        graph = material(path)
        frame = session.wait(
            session.apply(dict(geometry=[request], materials=[graph], camera_path=camera.path(), background=(0, 0, 0)))
        )
        checks.append(
            dict(name="Raw float texture preserves specified linear radiance", measured=expect(frame, (0.2, 0.4, 0.8)))
        )
        assert not session.textures_changed()
        conversions = session.status()["geometry_conversions"]
        builds = session.diagnostics()["material_builds"]
        session.wait(session.apply(dict(camera_path=camera.path())))
        assert (
            session.status()["geometry_conversions"] == conversions
            and session.diagnostics()["material_builds"] == builds
        )
        checks.append(dict(name="Unchanged image and geometry retained on a camera update"))
        image(path, (0.8, 0.1, 2.0))
        expect_texture_change(session)
        frame = session.wait(session.apply({}))
        checks.append(
            dict(
                name="Changed retained image refreshes without a material payload",
                measured=expect(frame, (0.8, 0.1, 2.0)),
            )
        )
        assert (
            session.status()["geometry_conversions"] == conversions
            and session.diagnostics()["material_builds"] == builds + 1
        )
        assert not session.textures_changed()
        quadrant = destination / (label + "-quadrants.exr")
        image(quadrant, [[[1, 0, 0], [0, 1, 0]], [[0, 0, 1], [1, 1, 0]]])
        frame = session.wait(session.apply(dict(materials=[material(quadrant)])))
        beauty = plane(frame, "combined")
        positions = plane(frame, "position")
        for sx, sy, color in ((-1, -1, (0, 0, 1)), (1, -1, (1, 1, 0)), (-1, 1, (1, 0, 0)), (1, 1, (0, 1, 0))):
            mask = (positions[:, :, 0] * sx > 0.5) & (positions[:, :, 1] * sy > 0.5)
            assert mask.sum() > 100 and np.max(np.abs(beauty[:, :, :3][mask] - color)) < 1e-5, (
                sx,
                sy,
                beauty[:, :, :3][mask].mean(0),
            )
        checks.append(dict(name="UV orientation follows specified top-down source rows and bottom-left coordinates"))
        mapped = material(quadrant)
        mapped["graph"]["nodes"] += [
            node("uv", "uvmap", attribute="uv"),
            node("mapping", "mapping", mapping_type="point", scale=[-1.0, 1.0, 1.0], location=[1.0, 0.0, 0.0]),
        ]
        mapped["graph"]["links"] += [
            link("uv", "UV", "mapping", "vector"),
            link("mapping", "vector", "image", "vector"),
        ]
        frame = session.wait(session.apply(dict(materials=[mapped])))
        measured = plane(frame, "combined")
        assert np.max(np.abs(measured[48, 16, :3] - (0, 1, 0))) < 1e-5
        checks.append(dict(name="Explicit UV Map and coordinate mapping mirror the expected texel"))
        image(path, (0.5, 0.5, 0.5))
        input_space = "sRGB Encoded Rec.709 (sRGB)"
        expected = (
            OCIO.GetCurrentConfig()
            .getProcessor(input_space, OCIO.ROLE_SCENE_LINEAR)
            .getDefaultCPUProcessor()
            .applyRGB([0.5, 0.5, 0.5])
        )
        frame = session.wait(session.apply(dict(materials=[material(path, colorspace=input_space)])))
        checks.append(
            dict(
                name="Input sRGB conversion precedes shading",
                input_space=input_space,
                reference=expected,
                measured=expect(frame, expected, tolerance=1e-4),
            )
        )
        image(path, (0.8, 0.4, 0.1))
        expected = (
            OCIO.GetCurrentConfig()
            .getProcessor(input_space, OCIO.ROLE_SCENE_LINEAR)
            .getDefaultCPUProcessor()
            .applyRGB([0.8, 0.4, 0.1])
        )
        frame = session.wait(session.apply({}))
        checks.append(
            dict(
                name="Input RGB primaries agree with the active OCIO working-space transform",
                reference=expected,
                measured=expect(frame, expected, tolerance=1e-4),
            )
        )
        tiled = destination / (label + "-tile.<UDIM>.exr")
        image(destination / (label + "-tile.1001.exr"), (0.1, 0.2, 0.3))
        image(destination / (label + "-tile.1002.exr"), (0.7, 0.4, 0.2))
        tiled_graph = material(tiled)
        tiled_graph["graph"]["nodes"] += [node("uv", "uvmap"), node("shift", "mapping", location=[1.0, 0.0, 0.0])]
        tiled_graph["graph"]["links"] += [
            link("uv", "UV", "shift", "vector"),
            link("shift", "vector", "image", "vector"),
        ]
        frame = session.wait(session.apply(dict(materials=[tiled_graph])))
        checks.append(
            dict(
                name="Automatically discovered UDIM 1002 is selected by shifted UVs",
                measured=expect(frame, (0.7, 0.4, 0.2)),
            )
        )
        restricted = deepcopy(tiled_graph)
        restricted["graph"]["nodes"][0]["parameters"]["tiles"] = [1001, 1002]
        frame = session.wait(session.apply(dict(materials=[restricted])))
        expect(frame, (0.7, 0.4, 0.2))
        image(destination / (label + "-tile.1002.exr"), (0.3, 0.8, 0.1))
        expect_texture_change(session)
        frame = session.wait(session.apply({}))
        expect(frame, (0.3, 0.8, 0.1))
        checks.append(dict(name="Explicit tile list and retained tile-file updates"))
        uvtile = destination / (label + "-uvtile.<UVTILE>.exr")
        image(destination / (label + "-uvtile.u1_v1.exr"), (0.9, 0.2, 0.4))
        frame = session.wait(session.apply(dict(materials=[material(uvtile)])))
        checks.append(dict(name="UVTILE filename semantics", measured=expect(frame, (0.9, 0.2, 0.4))))
        # An unreachable image never allocates or validates a file resource.
        unused = material(path)
        unused["graph"]["nodes"][0]["parameters"]["filename"] = "missing-unused.exr"
        unused["graph"]["links"] = []
        unused["graph"]["nodes"][1]["parameters"]["color"] = [0.2, 0.6, 0.3]
        frame = session.wait(session.apply(dict(materials=[unused])))
        expect(frame, (0.2, 0.6, 0.3))
        assert not session.textures_changed()
        checks.append(dict(name="Unused images do not become live resources"))
        for candidate, message in (
            (material(destination / "missing.exr"), "Missing texture file"),
            (material(path, colorspace="missing-color-space"), "Unknown texture colorspace"),
            (material(path, tiles=[1001]), "tile IDs require"),
            (material(tiled, tiles=[1001, 1001]), "unique integers"),
            (material(tiled, tiles=[True]), "require integers"),
            (material("op:/img/missing"), "COP image"),
        ):
            check_source_recovery(session, dict(materials=[candidate]), message)
        check_source_recovery(
            session,
            dict(geometry=[ch.geometry_request(geo, grid, material="surface")], materials=[material(path)]),
            "requires corner UVs",
        )
        checks.append(
            dict(name="Asset, colorspace, tiles and missing-UV failures render with local diagnostics", cases=7)
        )
        image(path, (0.5, 0.5, 0.5))
        frame = session.wait(session.apply(dict(geometry=[request], materials=[material(path)])))
        expect(frame, (0.5, 0.5, 0.5))
        height = destination / (label + "-height.exr")
        image(height, (0.3, 0.3, 0.3))
        displaced = material(path)
        displaced["graph"]["nodes"] += [
            node("height", "image_texture", filename=str(height), colorspace="data", interpolation="closest"),
            node("luminance", "rgb_to_bw"),
            node("displace", "displacement", space="object", midlevel=0.0, scale=1.0),
        ]
        displaced["graph"]["links"] += [
            link("height", "color", "luminance", "color"),
            link("luminance", "val", "displace", "height"),
        ]
        displaced["graph"]["displacement"] = endpoint("displace", "displacement")
        displaced["graph"]["displacement_method"] = "true"
        frame = session.wait(session.apply(dict(materials=[displaced])))
        assert abs(float(plane(frame, "position")[32, 32, 2]) - 0.3) < 1e-5
        rebuilds = session.status()["displacement_rebuilds"]
        extractions = session.status()["extractions"]
        image(height, (0.6, 0.6, 0.6))
        frame = session.wait(session.apply({}))
        assert abs(float(plane(frame, "position")[32, 32, 2]) - 0.6) < 1e-5
        assert session.status()["displacement_rebuilds"] > rebuilds and session.status()["extractions"] == extractions
        checks.append(dict(name="Image revisions invalidate true displacement from the original mesh"))
        rebuilds = session.status()["displacement_rebuilds"]
        image(path, (0.2, 0.4, 0.8))
        frame = session.wait(session.apply({}))
        expect(frame, (0.2, 0.4, 0.8))
        assert (
            session.status()["displacement_rebuilds"] == rebuilds
            and abs(float(plane(frame, "position")[32, 32, 2]) - 0.6) < 1e-5
        )
        checks.append(dict(name="Surface-only image edits retain textured displacement"))
        frame = session.wait(session.apply(dict(materials=[material(path)])))
        allocations = []
        host_commit = []
        for iteration in range(24):
            color = (0.2 if iteration % 2 else 0.6, 0.4, 0.8)
            image(path, color)
            frame = session.wait(session.apply({}))
            expect(frame, color)
            allocations.append(session.status()["device_allocation_bytes"])
            host_commit.append(ch.process_memory()["host"]["private_commit_bytes"])
        assert max(allocations[8:]) - min(allocations[8:]) < 1024 * 1024, allocations
        assert host_commit[-1] - min(host_commit[8:]) < 16 * 1024 * 1024, host_commit
        checks.append(
            dict(
                name="Repeated image replacements reach a device-buffer plateau",
                allocation_bytes=allocations,
                process_private_commit_bytes=host_commit,
                memory_scope="Logical device buffers and whole-Hython private commit; this short fixed-path fixture does not certify arbitrary scene or driver memory.",
            )
        )
    assert session.status()["device_allocation_bytes"] == 0
    reports.append(dict(device=device, result="passed", checks=checks))

# Authoring and the persistent GUI fixture use the actual native VOP context.
builder = ch.create_material(hou.node("/mat"), "native_texture_surface", shader="emission")
emission = next(item for item in builder.children() if item.type().name() == "cycles::emission::1")
emission.parm("strength").set(1.0)
texture = builder.createNode("cycles::image_texture::1", "file_image")
texture.parm("filename").set("$HIP/gui-texture.exr")
texture.parm("colorspace").set("data")
texture.parm("interpolation").set("closest")
emission.setNamedInput("color", texture, "color")
texture.parm("tiles").set("1001 1002")
authored = ch.material_graph(builder)
image_node = next(item for item in authored["nodes"] if item["definition"] == "cycles/image_texture/1")
assert image_node["parameters"]["tiles"] == [1001, 1002] and Path(image_node["parameters"]["filename"]).is_absolute()
texture.parm("tiles").set("")
uv.setDisplayFlag(True)
uv.setRenderFlag(True)
geo.parm("shop_materialpath").set(builder.path())
rop = hou.node("/out").createNode("cycles_ipr", "native_texture_cycles")
set_rop_devices(rop, "CPU")
for name, value in dict(
    source_mode="objects",
    objectroot="/obj",
    vobject=geo.name(),
    lights="",
    use_material_assignments=1,
    soppath=uv.path(),
    camera=camera.path(),
    materialpath=builder.path(),
    resx=64,
    resy=64,
    samples=32,
    use_denoise=0,
    use_texture_cache=0,
    film_exposure=1,
    world_source="color",
).items():
    rop.parm(name).set(value)
rop.parmTuple("background").set((0, 0, 0))
rop.parmTuple("lightstrength").set((0, 0, 0))
image(destination / "gui-texture.exr", (0.2, 0.4, 0.8))
hou.hipFile.save(str(destination / "native-textures.hiplc"))
authored = ch.material_graph(builder)
image_node = next(item for item in authored["nodes"] if item["definition"] == "cycles/image_texture/1")
assert Path(image_node["parameters"]["filename"]) == destination / "gui-texture.exr"

report = dict(
    result="passed",
    scope="Native file images, authored coordinates, input conversion and ownership; render-working-space switches require separate evidence.",
    devices=reports,
)
(destination / "houdini-texture-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
