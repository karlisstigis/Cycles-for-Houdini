# SPDX-License-Identifier: Apache-2.0
"""Live set-limit recovery, strict rejection and shared geometry ownership."""

import argparse, json, copy, traceback
import hou
import numpy as np
from cycles_houdini import native_scene, registry
from light_shader_fixture import Fixture

p = argparse.ArgumentParser()
p.add_argument("--device", default="CPU")
f = Fixture(p.parse_args().device)
entry = f.session()
report = dict(result="running", device=f.device, checks=[])
dest = f.dest.parent / "linking-limits"
dest.mkdir(exist_ok=True)
try:
    update = native_scene.capture(f.rop)
    template = update["geometry"][0]
    lamp = update["lights"][0]
    requests = []
    lights = []
    rules = []
    for index in range(65):
        request = copy.deepcopy(template)
        request["object_key"] = f"limit-object:{index}"
        request["object_properties"]["linking_id"] = f"root:{index}"
        requests.append(request)
        # Separate actual instance roots avoid undefined coplanar self-occlusion.
        root = f.scene.createNode("geo", f"limit_root_{index}", run_init_scripts=False)
        root.parm("tx").set(0 if index == 0 else 12 + index * 10)
        request["object_path"] = root.path()
        request["transform"][3] = root.evalParm("tx")
        light = copy.deepcopy(lamp)
        light.update(
            key=f"limit-light:{index}",
            linking_id=f"light:{index}",
            source_path=f"/obj/limit_light_{index}",
            strength=(100, 0, 0),
        )
        lights.append(light)
        rules.append(dict(objects=[f"root:{index}"], emitters=[f"light:{index}"]))
    update.update(geometry=requests, lights=lights, linking=dict(receivers=rules, blockers=rules))
    frame = entry.session.wait(entry.session.apply(update))
    status = entry.session.status()
    assert status["state"] == "complete" and status["geometry_resources"] == 1 and status["object_resources"] == 65, (
        status
    )
    issues = status["source_diagnostics"]
    assert len(issues) == 4 and all(d["code"] == "linking.limit" for d in issues), issues
    assert np.all(f.pixels(frame)[:, :, 3] == 1)
    report["checks"].append(
        dict(
            label="Sixty-five distinct receiver/blocker selections retain every object and one shared mesh, with local overflow diagnostics",
            status=status,
        )
    )
    converted = status["geometry_conversions"]
    pid = status["renderer_pid"]
    repaired = dict(receivers=[dict(objects=[f"root:{i}" for i in range(65)], emitters=["light:0"])], blockers=[])
    frame = entry.session.wait(entry.session.apply(dict(linking=repaired)))
    status = entry.session.status()
    f.check(frame, (1, 0, 0))
    assert (
        not status["source_diagnostics"]
        and status["geometry_conversions"] == converted
        and status["renderer_pid"] == pid
    ), status
    report["checks"].append(
        dict(
            label="Combining equivalent selections repairs both limits without geometry upload/conversion",
            status=status,
        )
    )
    committed = status.get("committed_generation", status["generation"])
    frame_generation = frame["generation"]
    invalid = dict(receivers=[dict(objects=["root:0"], emitters=[]), dict(objects=["root:0"], emitters=["light:0"])])
    try:
        entry.session.wait(entry.session.apply(dict(linking=invalid)), timeout=15)
    except (ValueError, RuntimeError) as error:
        assert "Conflicting linking" in str(error), str(error)
    else:
        raise AssertionError("Conflicting rules accepted")
    assert entry.session.status().get("committed_generation", entry.session.status()["generation"]) == committed
    assert entry.session.frame_info()["generation"] == frame_generation
    frame = entry.session.wait(entry.session.apply(dict(linking=repaired)))
    f.check(frame, (1, 0, 0))
    report["checks"].append(
        dict(
            label="Malformed internal rules reject before live mutation and explicit repair remains usable",
            status=entry.session.status(),
        )
    )
    entry.session.wait(entry.session.apply(dict(linking=dict(receivers=rules, blockers=rules))))
    entry.session.wait(entry.session.apply(dict(geometry=[])))
    status = entry.session.status()
    assert (
        status["state"] == "complete"
        and not status["source_diagnostics"]
        and status["object_resources"] == status["geometry_resources"] == 0
    ), status
    report["checks"].append(
        dict(
            label="Deleting affected objects clears obsolete limit warnings and retires shared geometry", status=status
        )
    )
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    registry.close_all()
    (dest / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))))
