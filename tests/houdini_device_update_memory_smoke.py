# SPDX-License-Identifier: Apache-2.0
"""Per-device, per-update tracked device memory record (engine patch 0117, host protocol 92).

A live session renders a large curve geometry, moves a small object, deletes the curves and then moves the
small object again. After
each completed update the test reads diagnostics()["device_update_memory"] and asserts: a record of the
latest update exists with a new serial, outcome "complete" and the session generation of that update; one
entry per selected render device with distinct exact device ids and distinct physical ids; integer byte
fields with peak >= start and peak >= end; build counts and the largest OptiX build temporary for updates
that built acceleration structures; no build temporary kept at the attributes boundary; and that the peak
restarts per update: after the large geometry is gone, a small update reports lower start, end and peak
bytes than an update while it was alive, which a lifetime maximum could not. The first update also reports
the largest OptiX build temporary of the curve build, the local (not reused) allocation it took and nothing
kept at the attributes boundary. Scene in memory, never saved.
"""

import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import hou
from cycles_houdini import device_selection, native_scene, object_properties, registry

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
device = p.parse_args().device
device = json.loads(device) if device.startswith("[") else device
report = dict(result="running", device=device, scope=__doc__, records=[])

obj = hou.node("/obj")
cam = obj.createNode("cam", "dm_cam")
cam.setParms(dict(tx=0, ty=1.5, tz=8, resx=160, resy=90))
small = obj.createNode("geo", "dm_small", run_init_scripts=False)
object_properties.install(small)
small.createNode("box").setParms(dict(scale=0.5))
hair = obj.createNode("geo", "dm_curves", run_init_scripts=False)
object_properties.install(hair)
grid = hair.createNode("grid")
grid.setParms(dict(sizex=4, sizey=4, rows=300, cols=300))
line = hair.createNode("line")
line.setParms(dict(points=12, dist=0.6))
copy = hair.createNode("copytopoints::2.0")
copy.setInput(0, line)
copy.setInput(1, grid)
bend = hair.createNode("attribwrangle", "dm_bend")
bend.setInput(0, copy)
bend.addSpareParmTuple(hou.FloatParmTemplate("amp", "amp", 1))
bend.parm("snippet").set("@P.x += ch('amp') * @P.y * @P.y; f@width = 0.004;")
bend.setRenderFlag(True)
bend.setDisplayFlag(True)
rop = hou.node("/out").createNode("cycles_ipr", "dm_rop")
rop.setParms(
    dict(
        camera=cam.path(),
        resx=160,
        resy=90,
        ipr_samples=1,
        samples=1,
        ipr_adaptive_sampling=0,
        adaptive_sampling=0,
        ipr_use_denoise=0,
        use_denoise=0,
        allowmotionblur=0,
        world_source="color",
    )
)
device_selection.set_devices(rop, device)
_orig = registry.Session
registry.Session = lambda **kw: _orig(collect_diagnostics=True, **kw)
selected = device if isinstance(device, list) else [device]


def objects(nodes):
    rop.parm("vobject").set(" ".join(n.name() for n in nodes))


def render(entry, label):
    cap = native_scene.capture(rop)
    if entry is None:
        entry = registry.for_rop(rop, native_scene.configuration(rop))
    generation = entry.apply(cap, hou.time())
    entry.session.wait(generation)
    start = time.monotonic()
    while True:
        d = entry.session.diagnostics()
        record = d.get("device_update_memory")
        if record and record["generation"] >= generation and not d.get("engine_statistics_pending"):
            break
        assert time.monotonic() - start < 30, (
            label,
            record,
            d.get("generation"),
            generation,
            d.get("engine_statistics_pending"),
            sorted(k for k in d.get("engine_update_seconds", {}) if "total" in k),
        )
        time.sleep(0.05)
    status = entry.session.status()
    assert not status.get("error"), status.get("error")
    record = dict(record, label=label, applied_generation=generation)
    report["records"].append(record)
    print("DMRECORD " + json.dumps(record), flush=True)
    return entry, record


def check(record, builds):
    assert record["outcome"] == "complete", record
    assert record["scope"] == "scene_device_update", record
    devices = record["devices"]
    assert len(devices) == len(selected), (devices, selected)
    assert len({d["id"] for d in devices}) == len(devices), devices
    assert len({d["physical_id"] for d in devices}) == len(devices), devices
    for d in devices:
        for key in (
            "start_bytes",
            "peak_bytes",
            "end_bytes",
            "max_build_temp_bytes",
            "max_build_temp_allocation_bytes",
            "build_count",
            "reused_temp_build_count",
            "local_temp_build_count",
            "kept_temp_bytes_before_attributes",
        ):
            assert isinstance(d[key], int) and d[key] >= 0, (key, d)
        assert d["peak_bytes"] >= d["start_bytes"] and d["peak_bytes"] >= d["end_bytes"], d
        assert d["kept_temp_bytes_before_attributes"] == 0, d
        assert d["build_count"] == d["reused_temp_build_count"] + d["local_temp_build_count"], d
        if builds and d["backend"] == "OPTIX":
            assert d["build_count"] >= 1 and d["max_build_temp_bytes"] > 0, d
            assert d["max_build_temp_allocation_bytes"] >= d["max_build_temp_bytes"], d


entry = None
try:
    registry.close_all()
    hou.setFrame(1)
    objects([small, hair])
    entry, large = render(None, "large_curves")
    check(large, builds=True)
    for d in large["devices"]:
        if d["backend"] == "OPTIX":
            # The curve build's temporary exceeds the 64 MiB reuse limit (0104): a local allocation.
            assert d["max_build_temp_bytes"] > 64 * 1024 * 1024 and d["local_temp_build_count"] >= 1, d
    small.parm("ty").set(0.3)
    entry, second = render(entry, "move_with_curves")
    check(second, builds=False)
    assert second["update_serial"] > large["update_serial"], (large, second)
    objects([small])
    entry, removed = render(entry, "curves_removed")
    check(removed, builds=False)
    small.parm("tx").set(0.4)
    entry, moved = render(entry, "small_move")
    check(moved, builds=False)
    assert moved["update_serial"] > removed["update_serial"], (removed, moved)
    # The record restarts per update: the small update after the curves were retired reports lower live
    # and peak bytes on every device than the update before their removal (both after the session's render
    # buffers exist, which are allocated after the first update), which a lifetime maximum could not.
    for a, b in zip(second["devices"], moved["devices"]):
        assert a["id"] == b["id"], (a, b)
        assert b["peak_bytes"] < a["peak_bytes"] and b["end_bytes"] < a["end_bytes"], (a, b)
    report["result"] = "passed"
finally:
    from support.paths import artifact_path

    artifact_path(
        "engine-contracts",
        "houdini_device_update_memory_smoke-" + ("cpu" if device.upper().startswith("CPU") else "gpu") + ".json",
    ).write_text(json.dumps(report, indent=2) + "\n")
    registry.close_all()
    print(json.dumps(dict(result=report["result"], records=len(report["records"]))), flush=True)
