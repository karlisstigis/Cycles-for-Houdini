# SPDX-License-Identifier: Apache-2.0
"""Retained shutter fields survive pose edits; off-frame repairs invalidate them."""

from support.paths import artifacts_root
import argparse, json, os
from pathlib import Path
import hou
import cycles_houdini as ch
from sampled_instance_attribute_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
args = parser.parse_args()
f = Fixture(args.device)
events = []
with ch.Session(
    device=f.devices,
    width=f.width,
    height=f.height,
    samples=f.samples,
    threads=2,
    execution="worker",
    collect_diagnostics=True,
    use_texture_cache=False,
) as session:

    def render(label, reuse=False, broken=False):
        before = f.fields.cookCount()
        frame = session.wait(session.apply(f.update()), timeout=120)
        status = session.status()
        assert not status["error"] and status["state"] == "complete", status
        f.inspect(frame)
        assert bool(status["source_diagnostics"]) == broken, status["source_diagnostics"]
        if reuse:
            assert status["motion_cooks"] == 0, status
            assert f.fields.cookCount() == before, (before, f.fields.cookCount())
        events.append(
            dict(
                label=label,
                cooks=status["motion_cooks"],
                source_cooks=f.fields.cookCount() - before,
                diagnostics=status["source_diagnostics"],
            )
        )

    for nested in (False, True):
        f.configure(nested=nested)
        render("Warm nested=" + str(nested))
        render("Unchanged accepted shutter owners", True)
        f.obj.parm("tx").set(0.025)
        render("Object translation", True)
        f.obj.parm("ry").set(2)
        render("Object rotation", True)
        f.obj.parm("tx").set(0)
        f.obj.parm("ry").set(0)
        f.configure(nested=nested, fault=1)
        render("Broken off-frame attribute", broken=True)
        render("Cached invalid tuple keeps diagnostic", True, True)
        f.configure(nested=nested)
        render("Repair off-frame attribute")
        render("Repaired samples retained", True)
    f.stable_nominal(True)
    f.configure()
    render("Fixed nominal IDs")
    ids = f.nominal_ids()
    f.configure(fault=2)
    render("Missing off-frame field with identical nominal IDs", broken=True)
    assert f.nominal_ids() == ids
    f.configure()
    render("Repair with identical nominal IDs")
    assert f.nominal_ids() == ids
    render("Repaired fixed-ID samples retained", True)
report = dict(result="passed", device=args.device, events=events)
path = artifacts_root() / "stage3/materials/sampled-instance-reuse.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
