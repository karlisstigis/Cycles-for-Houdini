# SPDX-License-Identifier: Apache-2.0
"""Skipping the unused nominal name index preserves multiple roots, nesting, holes and reorders.

Both modes consume the exact same capture in one Houdini process. The real
queued capture path uses acquire_packed_roots before conversion. Images and
snapshot payloads must match the legacy name-index path after each edit.
"""

import hashlib
import json
import os
import time
import traceback
from contextlib import ExitStack

import hou
import cycles_houdini as ch
from cycles_houdini import native_scene
from cycles_houdini.ipr import queued
from support.paths import artifacts_root

out = artifacts_root() / "stage3/packed-named-index"
out.mkdir(parents=True, exist_ok=True)
report = dict(result="running", rows=[])
scene = hou.node("/obj").createNode("subnet", "packed_ledger")
roots = []
for index in range(2):
    obj = scene.createNode("geo", "root%d" % index, run_init_scripts=False)
    shape = obj.createNode("box" if index == 0 else "sphere", "prototype")
    points = obj.createNode("grid", "placements")
    points.setParms(dict(type=0, orient=0, rows=7, cols=8))
    points.parmTuple("size").set((3, 3))
    copies = obj.createNode("copytopoints", "packed_copies")
    copies.setInput(0, shape)
    copies.setInput(1, points)
    copies.parm("pack").set(1)
    fields = obj.createNode("attribwrangle", "identities")
    fields.setInput(0, copies)
    fields.parm("class").set(1)
    fields.parm("snippet").set('s@name=sprintf("instance%d", @primnum);')
    deleted = obj.createNode("attribwrangle", "holes")
    deleted.setInput(0, fields)
    deleted.parm("class").set(1)
    deleted.parm("snippet").set("if(@primnum%5==0) removeprim(0,@primnum,1);")
    deleted.bypass(True)
    ordered = obj.createNode("sort", "reordered")
    ordered.setInput(0, deleted)
    ordered.parm("primsort").set(next(i for i, s in enumerate(ordered.parm("primsort").menuItems()) if "rev" in s))
    ordered.bypass(True)
    ordered.setRenderFlag(True)
    ordered.setDisplayFlag(True)
    obj.parm("tx").set(index * 4 - 2)
    roots.append(dict(obj=obj, shape=shape, points=points, end=ordered, holes=deleted, fields=fields))
# A third root wraps the first root in another packed level.
nested = scene.createNode("geo", "nested", run_init_scripts=False)
merged = nested.createNode("object_merge", "import_root")
merged.parm("objpath1").set(roots[0]["end"].path())
pack = nested.createNode("pack", "nested_pack")
pack.setInput(0, merged)
pack.setRenderFlag(True)
pack.setDisplayFlag(True)
nested.parm("ty").set(4)
cam = scene.createNode("cam", "camera")
cam.setParms(dict(tz=18, projection=1, orthowidth=14))
rop = hou.node("/out").createNode("cycles_ipr", "packed_ledger_rop")
rop.setParms(
    dict(
        objectroot=scene.path(),
        vobject="*",
        camera=cam.path(),
        lights="",
        forcelights="",
        excludelights="*",
        world_source="color",
        resx=160,
        resy=120,
        samples=2,
        ipr_samples=2,
        use_material_assignments=0,
        use_denoise=0,
        ipr_use_denoise=0,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        allowmotionblur=0,
    )
)
rop.parmTuple("background").set((0.25, 0.25, 0.25))
records = []
original = queued._native


class Proxy:
    def __getattr__(self, name):
        value = getattr(original, name)
        if name != "encode_snapshot":
            return value

        def encode(*args, **kwargs):
            payload = value(*args, **kwargs)
            records.append(dict(payload=hashlib.sha256(payload).hexdigest(), digest=original.snapshot_digest(args[0])))
            return payload

        return encode


def check_expired_packed_turns():
    # Force every preparation turn to enter the packed adapter after its 20 ms
    # deadline, as when earlier objects in a scene take up the whole slice.
    class SlowFlag:
        def __bool__(self):
            time.sleep(0.035)
            return True

    request = ch.geometry_request(roots[0]["obj"], roots[0]["end"], material_mode="uniform")
    update = dict(geometry=[request])
    handle = ch._native.create("CPU", 8, 8, 1, True, False, False, False, None, True)
    try:
        now = hou.time()
        job = ch._native.acquire_apply(handle, update, now)
        while True:
            ch._native.prewarm_apply(job)
            if not ch._native.expand_apply(job):
                break
        request["smooth"] = SlowFlag()
        for _ in range(16):
            if ch._native.prepare_apply(handle, update, now, False, job):
                break
        else:
            raise AssertionError("Packed preparation repeatedly yielded without finishing a small frozen root")
        ch._native.convert_apply(job)
        snapshot = ch._native.finish_apply(job)
        assert ch._native.snapshot_info(snapshot)["objects"] == 56
    finally:
        ch._native.close(handle)


queued._native = Proxy()
try:
    check_expired_packed_turns()
    with ExitStack() as stack:
        sessions = {
            mode: stack.enter_context(
                ch.Session(
                    device="CPU",
                    width=160,
                    height=120,
                    samples=2,
                    threads=2,
                    execution="worker",
                    use_texture_cache=False,
                )
            )
            for mode in ("legacy", "skip")
        }
        for index, step in enumerate(
            ("initial", "holes", "reorder", "prototype", "restore", "duplicates", "unique_restored", "unchanged")
        ):
            if step == "holes":
                for root in roots:
                    root["holes"].bypass(False)
            elif step == "reorder":
                for root in roots:
                    root["end"].bypass(False)
            elif step == "prototype":
                roots[0]["shape"].parm("sizex").set(1.4)
            elif step == "restore":
                for root in roots:
                    root["holes"].bypass(True)
                    root["end"].bypass(True)
            elif step == "duplicates":
                for root in roots:
                    root["fields"].parm("snippet").set('s@name="duplicate"; i@id=7;')
            elif step == "unique_restored":
                for root in roots:
                    root["fields"].parm("snippet").set('s@name=sprintf("instance%d", @primnum);')
            capture = native_scene.capture(rop)
            row = dict(step=step, modes={})
            for mode in ("legacy", "skip") if index % 2 == 0 else ("skip", "legacy"):
                os.environ["CYCLES_HOUDINI_PACKED_NAMED_INDEX_LEGACY"] = "1" if mode == "legacy" else "0"
                begin = len(records)
                session = sessions[mode]
                frame = session.wait(session.apply(capture), timeout=90)
                status = session.status()
                assert status["state"] == "complete" and not status["error"], status
                row["modes"][mode] = dict(
                    image=hashlib.sha256(frame["planes"]["combined"]["pixels"]).hexdigest(),
                    payloads=records[begin:],
                    objects=status.get("object_resources"),
                    packed=status.get("packed_instances"),
                )
            assert row["modes"]["legacy"] == row["modes"]["skip"], row
            assert row["modes"]["skip"]["payloads"], ("no captured worker payload", row)
            if step == "duplicates":
                # Duplicate recovery must keep every instance, including the
                # first ambiguous member, rather than merging equal names.
                assert (
                    row["modes"]["skip"]["payloads"][-1]["digest"]["objects"]
                    == report["rows"][4]["modes"]["skip"]["payloads"][-1]["digest"]["objects"]
                ), row
            report["rows"].append(row)
        assert report["rows"][1]["modes"]["skip"]["image"] != report["rows"][0]["modes"]["skip"]["image"]
        assert report["rows"][2]["modes"]["skip"]["image"] == report["rows"][1]["modes"]["skip"]["image"]
        assert report["rows"][3]["modes"]["skip"]["image"] != report["rows"][2]["modes"]["skip"]["image"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    queued._native = original
    os.environ.pop("CYCLES_HOUDINI_PACKED_NAMED_INDEX_LEGACY", None)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, default=str), flush=True)
assert report["result"] == "passed", report.get("error")
