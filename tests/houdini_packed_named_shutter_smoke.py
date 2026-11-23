# SPDX-License-Identifier: Apache-2.0
"""Unique named packed shutter fields survive off-frame reordering, with and without the nominal name index."""

import hashlib
import json
import os
import traceback
from contextlib import ExitStack
import hou
import cycles_houdini as ch
from sampled_instance_attribute_fixture import Fixture
from cycles_houdini.ipr import queued
from support.paths import artifacts_root

f = Fixture("CPU")
f.samples = 8
for name in ("samples", "ipr_samples"):
    f.rop.parm(name).set(8)
connections = list(f.fields.outputConnections())
names = f.obj.createNode("attribwrangle", "stable_packed_names")
names.setInput(0, f.fields)
names.parm("class").set(1)
names.parm("snippet").set('s@name=sprintf("stable-%d",@primnum);')
order = f.obj.createNode("sort", "shutter_permutation")
order.setInput(0, names)
reverse = next(i for i, s in enumerate(order.parm("primsort").menuItems()) if "rev" in s)
normal = order.parm("primsort").evalAsInt()
for connection in connections:
    connection.outputNode().setInput(connection.inputIndex(), order)
report = dict(result="running", rows=[])
original = queued._native
records = []


class Proxy:
    def __getattr__(self, name):
        value = getattr(original, name)
        if name != "encode_snapshot":
            return value

        def encode(*args, **kwargs):
            payload = value(*args, **kwargs)
            records.append(
                dict(
                    payload=hashlib.sha256(payload).hexdigest(),
                    inventory=original.snapshot_info(args[0]),
                    digest=original.snapshot_digest(args[0]),
                )
            )
            return payload

        return encode


queued._native = Proxy()
try:
    with ExitStack() as stack:
        sessions = {
            mode: stack.enter_context(
                ch.Session(
                    device="CPU",
                    width=f.width,
                    height=f.height,
                    samples=8,
                    threads=2,
                    execution="worker",
                    collect_diagnostics=True,
                    use_texture_cache=False,
                )
            )
            for mode in ("legacy", "skip")
        }
        for nested, rows in ((False, 2), (True, 2), (False, 30)):
            f.targets.setParms(dict(rows=rows, cols=rows))
            f.configure(nested=nested)
            # The 900-row control retains exact nominal data IDs while the
            # shader demands named shutter fields. This exercises a source
            # used by both nominal row and indexed shutter preparation.
            f.stable_nominal(rows == 30)
            previous = None
            nominal_ids = None
            for shuffled in (False, True):
                order.parm("primsort").deleteAllKeyframes()
                if shuffled:
                    order.parm("primsort").setExpression("if(abs($FF-floor($FF+0.5))>0.01,%d,%d)" % (reverse, normal))
                else:
                    order.parm("primsort").set(normal)
                capture = f.update()
                row = dict(nested=nested, population=rows * rows, shuffled=shuffled, modes={})
                if rows == 30:
                    current_ids = f.nominal_ids()
                    if nominal_ids is not None:
                        assert current_ids == nominal_ids, "nominal control data IDs changed"
                    nominal_ids = current_ids
                    row["nominal_ids"] = current_ids
                for mode in ("legacy", "skip"):
                    os.environ["CYCLES_HOUDINI_PACKED_NAMED_INDEX_LEGACY"] = "1" if mode == "legacy" else "0"
                    session = sessions[mode]
                    record_begin = len(records)
                    frame = session.wait(session.apply(capture), timeout=90)
                    status = session.status()
                    assert status["state"] == "complete" and not status["error"], status
                    row["modes"][mode] = dict(
                        image=hashlib.sha256(frame["planes"]["combined"]["pixels"]).hexdigest(),
                        diagnostics=status.get("source_diagnostics"),
                        motion_cooks=status.get("motion_cooks"),
                        records=records[record_begin:],
                    )
                    assert records[-1]["inventory"]["sampled_instance_attribute_fields"] > 0, records[-1]
                    if rows == 30:
                        assert records[-1]["inventory"]["objects"] == 900, records[-1]
                assert row["modes"]["legacy"] == row["modes"]["skip"], row
                if previous is not None:
                    assert row["modes"]["skip"]["image"] == previous, ("shutter permutation changed image", row)
                previous = row["modes"]["skip"]["image"]
                report["rows"].append(row)
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
finally:
    queued._native = original
    os.environ.pop("CYCLES_HOUDINI_PACKED_NAMED_INDEX_LEGACY", None)
    dest = artifacts_root() / "stage3/packed-named-shutter.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, default=str), flush=True)
assert report["result"] == "passed", report.get("error")
