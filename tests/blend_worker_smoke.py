# SPDX-License-Identifier: Apache-2.0
"""Host Python integration check for persistent Blender binary transport.

python tests/blend_worker_smoke.py --blender <blender.exe> --output <directory>
"""

import argparse
from support.package_loader import import_product
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import numpy as np

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--blender", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
source = output / "source.blend"
root = Path(__file__).resolve().parents[1]
fixture = output / "fixture.py"
fixture.write_text(
    """import bpy
from pathlib import Path
root = Path(__file__).parent
bpy.ops.wm.read_factory_settings(use_empty=True)
library = bpy.data.collections.new('Library')
library.use_fake_user = True
libmesh = bpy.data.meshes.new('LinkedMesh')
libmesh.from_pydata([(0,0,0),(2,0,0),(0,2,0)], [], [(0,1,2)])
library.objects.link(bpy.data.objects.new('LinkedTriangle', libmesh))
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'library.blend'))
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
collection = bpy.data.collections.new('Detached')
collection.use_fake_user = True
mesh = bpy.data.meshes.new('Mesh')
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)], [], [(0,1,2)])
obj = bpy.data.objects.new('Triangle', mesh)
collection.objects.link(obj)
image = bpy.data.images.new('Texture', width=1, height=1)
image.pixels = (1,0,0,1)
image.filepath_raw = str(root / 'texture.png')
image.file_format = 'PNG'
image.save()
image.source = 'FILE'
material = bpy.data.materials.new('Material')
material.use_nodes = True
texture = material.node_tree.nodes.new('ShaderNodeTexImage')
texture.image = image
material.node_tree.links.new(texture.outputs['Color'], material.node_tree.nodes.get('Principled BSDF').inputs['Base Color'])
mesh.materials.append(material)
with bpy.data.libraries.load(str(root / 'library.blend'), link=True) as (available, selected):
    selected.collections = ['Library']
scene.collection.children.link(selected.collections[0])
obj.location = (1,2,3)
obj.keyframe_insert(data_path='location', frame=1)
obj.location.x = 5
obj.keyframe_insert(data_path='location', frame=2)
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'source.blend'))
""",
    encoding="utf-8",
)
subprocess.run(
    [
        args.blender,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python-exit-code",
        "1",
        "--python",
        str(fixture),
    ],
    check=True,
)
path = root / "houdini/python3.13libs/cycles_houdini/blend_worker.py"
worker = import_product("blend_worker")
textures = output / "transport"


def request(kind="COLLECTION", name="Detached", frame=1):
    return dict(source=str(source), selector_type=kind, selector=name, frame=frame)


try:
    requests = [
        request(),
        request("OBJECT", "Triangle", 2),
        request(),
        request("COLLECTION", "Missing"),
        request("OBJECT", "Triangle", 1),
        request(),
    ]
    results = worker.evaluate_many(source, args.blender, requests, textures, timeout=60)
    assert len(worker._workers) == 1
    process = next(iter(worker._workers.values())).process
    pid = process.pid
    initial_texture_hashes = [
        dependency["sha256"]
        for material in results[0][0]["materials"]
        for node in material["nodes"]
        for dependency in node.get("image", {}).get("dependencies", [])
    ]
    assert initial_texture_hashes
    assert isinstance(results[3], Exception), results[3]
    for index, result in enumerate(results):
        if index == 3:
            continue
        assert not isinstance(result, Exception), str(result)
        manifest, arrays = result
        assert len(manifest["instances"]) == 1, (index, manifest["instances"])
        assert manifest["depsgraph_mode"] == "RENDER"
        assert "geometry_file" not in manifest
        mesh = manifest["meshes"][0]
        assert arrays[mesh["arrays"]["positions"]].shape == (3, 3)
        assert np.allclose(manifest["instances"][0]["transform"][3::4][:3], (5 if index == 1 else 1, 3, -2)), manifest[
            "instances"
        ]
    # Shared topology/static fields are advertised by identity and transferred
    # once, including across selectors and frames with different transforms.
    stream_worker = next(iter(worker._workers.values()))
    assert stream_worker.metrics["reused_chunks"] > 0, stream_worker.metrics
    assert stream_worker.metrics["transferred_bytes"] < stream_worker.metrics["referenced_bytes"], stream_worker.metrics
    first_mesh = results[0][0]["meshes"][0]
    repeat_mesh = results[2][0]["meshes"][0]
    for field in first_mesh["arrays"]:
        first = results[0][1][first_mesh["arrays"][field]]
        repeated = results[2][1][repeat_mesh["arrays"][field]]
        assert first is repeated, field
        assert not first.flags.writeable
    before_transfer = stream_worker.metrics["transferred_bytes"]
    repeated = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60)[0]
    assert not isinstance(repeated, Exception), str(repeated)
    assert stream_worker.metrics["transferred_bytes"] == before_transfer, stream_worker.metrics
    assert stream_worker.metrics["batches"] >= 2

    # Deterministically invalidate after a real result has crossed the Windows
    # pipe, before the owner can publish it. The old revision is discarded and
    # the same request recovers through a fresh process without manual retry.
    original_recv = worker._recv_result
    received, resumed = threading.Event(), threading.Event()
    old_generation = worker._epoch(source)
    paused = [False]

    def pause_receive(*arguments):
        value = original_recv(*arguments)
        if not paused[0] and not isinstance(value, Exception):
            paused[0] = True
            received.set()
            assert resumed.wait(20), "Invalidation did not arrive"
            deadline = time.monotonic() + 20
            while worker._epoch(source) == old_generation and time.monotonic() < deadline:
                time.sleep(0.005)
            assert worker._epoch(source) != old_generation
        return value

    worker._recv_result = pause_receive

    def invalidate_inflight():
        assert received.wait(20), "No result received for invalidation"
        resumed.set()
        worker.invalidate(source)

    invalidator = threading.Thread(target=invalidate_inflight, name="test invalidator")
    invalidator.start()
    try:
        recovered = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60)[0]
    finally:
        worker._recv_result = original_recv
        invalidator.join(timeout=20)
    assert not invalidator.is_alive()
    assert not isinstance(recovered, Exception), str(recovered)
    process = next(iter(worker._workers.values())).process
    assert process.pid != pid, "Invalidated process published its old result"
    pid = process.pid

    # Detect a file version change at the same boundary too. No stale version
    # appears in the returned manifest, and subsequent calls reuse the worker.
    changed_once = [False]

    def touch_receive(*arguments):
        value = original_recv(*arguments)
        if not changed_once[0] and not isinstance(value, Exception):
            changed_once[0] = True
            stamp = source.stat()
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
        return value

    worker._recv_result = touch_receive
    try:
        recovered = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60)[0]
    finally:
        worker._recv_result = original_recv
    assert not isinstance(recovered, Exception), str(recovered)
    assert tuple(recovered[0]["dependency_versions"][str(source)]) == worker._signature(source)
    process = next(iter(worker._workers.values())).process
    pid = process.pid
    # Reopen an edited source inside the existing process, without relaunching.
    edit = output / "edit.py"
    edit.write_text(
        """import bpy
from pathlib import Path
source = str(Path(__file__).with_name('source.blend'))
bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
bpy.data.objects['Triangle'].data.vertices[1].co.x = 7
bpy.ops.wm.save_as_mainfile(filepath=source)
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            args.blender,
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(edit),
        ],
        check=True,
    )
    result = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60)[0]
    assert not isinstance(result, Exception), str(result)
    manifest, arrays = result
    assert arrays[manifest["meshes"][0]["arrays"]["positions"]][1, 0] == 7
    assert next(iter(worker._workers.values())).process.pid == pid
    # Linked libraries and external textures are tracked across selectors too.
    edit.write_text(
        """import bpy
from pathlib import Path
root = Path(__file__).parent
image = bpy.data.images.new('ChangedTexture', width=1, height=1)
image.pixels = (0,1,0,1)
image.filepath_raw = str(root / 'texture.png')
image.file_format = 'PNG'
image.save()
bpy.ops.wm.open_mainfile(filepath=str(root / 'library.blend'), load_ui=False, use_scripts=False)
bpy.data.objects['LinkedTriangle'].data.vertices[1].co.x = 11
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'library.blend'))
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            args.blender,
            "--background",
            "--factory-startup",
            "--disable-autoexec",
            "--python-exit-code",
            "1",
            "--python",
            str(edit),
        ],
        check=True,
    )
    changed = worker.evaluate_many(
        source, args.blender, [request("COLLECTION", "Library"), request()], textures, timeout=60
    )
    assert all(not isinstance(result, Exception) for result in changed), changed
    manifest, arrays = changed[0]
    assert arrays[manifest["meshes"][0]["arrays"]["positions"]][1, 0] == 11
    changed_texture_hashes = [
        dependency["sha256"]
        for material in changed[1][0]["materials"]
        for node in material["nodes"]
        for dependency in node.get("image", {}).get("dependencies", [])
    ]
    assert changed_texture_hashes != initial_texture_hashes
    assert next(iter(worker._workers.values())).process.pid == pid
    assert not list(textures.rglob("*.npz")) and not list(textures.rglob("*.bgeo*"))
    # Death between calls recovers with one replacement process.
    process.kill()
    process.wait(timeout=10)
    result = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60)[0]
    assert not isinstance(result, Exception), str(result)
    assert next(iter(worker._workers.values())).process.pid != pid
    # A source OCIO edit at the same path requires a new process. Preserve both
    # size and mtime to verify content identity rather than timestamps alone.
    configs = list(Path(args.blender).parent.glob("*/datafiles/colormanagement/config.ocio"))
    assert configs, "Installed Blender color configuration not found"
    original_config = configs[0]
    config = output / "source.ocio"
    text = original_config.read_text(encoding="utf-8")
    search_paths = [(original_config.parent / name).as_posix() for name in ("icc", "luts", "filmic")]
    text = re.sub(r"^search_path:.*$", "search_path: " + json.dumps(search_paths), text, flags=re.MULTILINE)
    config.write_text(text + "\n# launch revision A\n", encoding="utf-8")
    result = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60, source_config=config)[0]
    assert not isinstance(result, Exception), str(result)
    configured_worker = next(iter(worker._workers.values()))
    configured_pid = configured_worker.process.pid
    previous_signature = worker.launch_signature(args.blender, config)
    config_stat = config.stat()
    config.write_bytes(config.read_bytes().replace(b"# launch revision A", b"# launch revision B"))
    os.utime(config, ns=(config_stat.st_atime_ns, config_stat.st_mtime_ns))
    assert config.stat().st_size == config_stat.st_size
    assert config.stat().st_mtime_ns == config_stat.st_mtime_ns
    assert worker.launch_signature(args.blender, config) != previous_signature
    result = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60, source_config=config)[0]
    assert not isinstance(result, Exception), str(result)
    assert next(iter(worker._workers.values())).process.pid != configured_pid
    assert configured_worker.process.poll() is not None
    # Executable metadata identity changes without editing an installed binary.
    binary_fixture = output / "binary-revision.fixture"
    binary_fixture.write_bytes(b"first")
    binary_signature = worker.launch_signature(binary_fixture)
    binary_fixture.write_bytes(b"second-revision")
    assert worker.launch_signature(binary_fixture) != binary_signature
    # An unanswered pipe read is cancelled on timeout and its helper thread
    # finishes before the worker can be replaced or the caller can reuse it.
    timed_worker = next(iter(worker._workers.values()))
    try:
        timed_worker._bounded(lambda: worker._recv_json(timed_worker.connection), 0.02)
        raise AssertionError("An unanswered pipe read should time out")
    except TimeoutError:
        pass
    assert timed_worker.process.poll() is not None
    assert not timed_worker._active_rpc.is_alive()
    assert not timed_worker._diagnostics_thread.is_alive()
    result = worker.evaluate_many(source, args.blender, [request()], textures, timeout=60, source_config=config)[0]
    assert not isinstance(result, Exception), str(result)
    report = dict(
        result="passed",
        checks=[
            "repeated selectors",
            "frame changes",
            "per-request failure",
            "source/library/texture invalidation without relaunch",
            "process crash recovery",
            "no geometry files",
            "same-path OCIO content invalidation",
            "executable metadata identity",
            "timeout cancellation and thread teardown",
            "chunk identity acknowledgement and zero-byte repeated payloads",
            "immutable shared topology arrays",
            "in-flight explicit invalidation discard and recovery",
            "in-flight source edit never publishes stale version",
        ],
    )
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
finally:
    worker.shutdown_all()
    assert not worker._workers
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("CH Blender")]
