# SPDX-License-Identifier: Apache-2.0
"""Varying native voxels, mixed SOP components and concurrent frozen ownership."""

import argparse
import gc
import json
import math
import threading
import time
import traceback

import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, registry
from dense_volume_fixture import fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
device = parser.parse_args().device
rop, unused_file, assignment, dest = fixture(device)
obj = unused_file.parent()
volume = obj.createNode("volume", "varying_native_density")
volume.setParms(dict(name="density", initialval1=0.35, uniformsamples=0, divsx=19, divsy=13, divsz=11))
volume.parmTuple("size").set((2, 1.5, 1))
values = obj.createNode("volumewrangle", "voxel_variation")
values.setInput(0, volume)
values.addSpareParmTuple(hou.FloatParmTemplate("level", "Density", 1, default_value=(0.35,)))
values.parm("snippet").set('@density=chf("level")+.15*sin(11*@P.z);')
assignment.setInput(0, values)
reference = obj.createNode("convertvdb", "independent_native_conversion")
reference.setInput(0, values)
reference.setParms(dict(conversion="vdb", vdbclass="fog"))
aux = obj.createNode("volume", "auxiliary_field")
aux.setParms(dict(name="auxiliary", initialval1=0.2, uniformsamples=0, divsx=19, divsy=13, divsz=11))
aux.parmTuple("size").set((2, 1.5, 1))
aux_vdb = obj.createNode("convertvdb", "sparse_auxiliary")
aux_vdb.setInput(0, aux)
aux_vdb.setParms(dict(conversion="vdb", vdbclass="fog"))
strands = obj.createNode("python", "point_and_hair_fixture")
strands.parm("python").set("""import hou
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'shop_materialpath','')
g.addAttrib(hou.attribType.Prim,'shop_materialpath','')
g.addAttrib(hou.attribType.Point,'pscale',.08)
g.addAttrib(hou.attribType.Point,'width',.1)
p=g.createPoint();p.setPosition((-1.5,.5,0))
p.setAttribValue('shop_materialpath','/mat/dense_recovery_sentinel')
c=g.createPolygon(is_closed=False)
c.setAttribValue('shop_materialpath','/mat/dense_recovery_sentinel')
for y in (-.7,-.5,-.3):
 p=g.createPoint();p.setPosition((-1.5,y,0));c.addVertex(p)
""")
output = obj.node("OUT")
output.setInput(2, strands)
output.setInput(3, aux_vdb)
entry = registry.for_rop(rop, native_scene.configuration(rop))
report = dict(result="running", device=device, checks=[])
stop = threading.Event()
errors = []
count = [0]
lease = None
thread = None
try:
    lease = ch._native.preserve_volume(values.path(), hou.time())
    frozen = ch._native.convert_preserved_volume(lease)
    field = frozen["fields"][0]
    assert field["active_voxels"] == 19 * 13 * 11, field
    assert abs(field["active_sums"][0] - 0.35 * 19 * 13 * 11) < 0.0003, field
    expected_zero = 0.35 + 0.15 * math.sin(11 * (-0.5 + 0.5 / 11))
    assert abs(field["voxel_zero"][0] - expected_zero) < 1e-6, field
    report["owned_reference"] = frozen

    def inspect_frozen():
        try:
            while not stop.is_set():
                observed = ch._native.convert_preserved_volume(lease)
                assert observed["fingerprint"] == frozen["fingerprint"], "Frozen dense voxels changed"
                count[0] += 1
                stop.wait(0.005)
        except BaseException:
            errors.append(traceback.format_exc())

    thread = threading.Thread(target=inspect_frozen, name="Frozen dense voxel inspection")
    thread.start()

    def render(label):
        frame = entry.session.wait(entry.apply(native_scene.capture(rop), hou.time()))
        status = entry.session.status()
        plane = frame["planes"]["combined"]
        image = np.frombuffer(plane["pixels"], np.float32).reshape(48, 80, plane["channels"])[:, :, :3].copy()
        assert np.isfinite(image).all() and not status["source_diagnostics"] and status["state"] != "failed", status
        assert (
            status["geometry_resources"] == status["object_resources"] == 4
            and status["points"] == status["curves"] == 1
        ), status
        # Odd symmetric voxel samples cancel the sine term. Linear filtering
        # integrates their tent basis to sum(values)*voxel_spacing = level.
        expected = math.exp(-values.evalParm("level"))
        observed = float(image[21:27, 37:43, 0].mean())
        assert abs(observed - expected) < 0.004, (observed, expected)
        for x, y in ((70, 24), (10, 34), (10, 14)):
            np.testing.assert_allclose(image[y, x], (0.1, 0.7, 0.2), atol=1e-4)
        report["checks"].append(dict(label=label, expected=expected, observed=observed, status=status))
        return image

    baseline = render("Varying dense field with sparse auxiliary, mesh, point and hair in one SOP")
    for level in (0.65, 0.2, 0.35):
        values.parm("level").set(level)
        render("Native voxel recook " + str(level))
        assert not errors, errors
    assignment.setInput(0, reference)
    converted = render("Houdini Convert VDB reference")
    report["native_vdb_difference"] = float(np.abs(baseline - converted).mean())
    assert report["native_vdb_difference"] < 0.0012, report["native_vdb_difference"]
    assignment.setInput(0, values)
    render("Native dense field restored")
    before_delete = count[0]
    obj.destroy()
    assert ch._native.convert_preserved_volume(lease)["fingerprint"] == frozen["fingerprint"]
    deadline = time.monotonic() + 5
    while count[0] <= before_delete and not errors and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not errors and count[0] > before_delete, errors
    stop.set()
    thread.join(10)
    assert not thread.is_alive() and not errors, errors
    report["frozen_conversions"] = count[0]
    lease = None
    gc.collect()
    assert ch._native.geometry_lease_count() == 0
    session = entry.session
    registry.close_all()
    deadline = time.monotonic() + 15
    while session.status()["state"] != "closed" and time.monotonic() < deadline:
        time.sleep(0.01)
    report["closed"] = session.status()
    assert (
        report["closed"]["owned_geometry_bytes"]
        == report["closed"]["device_allocation_bytes"]
        == report["closed"]["retained_dense_voxel_bytes"]
        == 0
    ), report["closed"]
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    stop.set()
    if thread:
        thread.join(10)
    lease = None
    registry.close_all()
    (dest / "voxel-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
