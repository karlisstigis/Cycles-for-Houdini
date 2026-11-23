# SPDX-License-Identifier: Apache-2.0
"""First delivered worker image after field edits on four shared million-triangle copies.

Includes Houdini cooking/capture, conversion, transport and engine work, using
the viewer feedback/polling path. Receipt is acknowledged without GUI paint;
this is not physical-input latency. Timing budgets remain prospective.
"""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

import argparse, hashlib, json, math, os, time, traceback
from pathlib import Path
import hou, numpy as np
import cycles_houdini as ch
from cycles_houdini import object_properties
from pointiness_fixture import Fixture

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="CPU")
parser.add_argument("--expect-rebuild", action="store_true")
args = parser.parse_args()
root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
dest = artifacts_root() / "stage3/performance/shading-delivery"
dest.mkdir(parents=True, exist_ok=True)
path = dest / ("baseline-report.json" if args.expect_rebuild else "report.json")
f = Fixture(args.device)
f.width, f.height = 960, 540
configure_render(
    f.rop,
    dict(
        resx=f.width,
        resy=f.height,
        samples=16,
        ipr_samples=16,
        pass_position=0,
        pass_normal=0,
        limit_subdivision=True,
        subdivision_limit=2000000,
    ),
)
grid = f.obj.createNode("grid", "benchmark_control_mesh")
pass_fixture.set_parameters(grid, dict(type=0, orient=0, rows=181, cols=181))
grid.parmTuple("size").set((2, 2))
group = f.wave.parmTemplateGroup()
group.append(hou.FloatParmTemplate("value", "Value", 1, default_value=(0.1,)))
f.wave.setParmTemplateGroup(group)
f.wave.setInput(0, grid)
pass_fixture.parm(f.wave, "snippet").set('@P.z=.1*sin(3*@P.x)*cos(2*@P.y); f@rand=chf("value");')
pass_fixture.parm(f.output, "input").set(1)
object_properties.install(f.obj)
pass_fixture.set_parameters(
    f.obj,
    dict(
        cycles_subdivision_override=1,
        cycles_subdivision_type="catmull_clark",
        cycles_subdivision_adaptive=0,
        cycles_subdivision_ipr_level=2,
        cycles_subdivision_render_level=2,
    ),
)
pass_fixture.parm(f.user, "attribute").set("rand")
f.shade("user")
f.displace(False)
height = f.mat.createNode("cycles::vector_displacement::1", "constant_object_offset")
pass_fixture.set_parameters(height, dict(space="object", midlevel=0, scale=0.03))
height.parmTuple("vector").set((0, 0, 1))
f.mat.node("surface_output").setNamedInput("displacement", height, "displacement")
pass_fixture.parm(f.mat, "cycles_displacement_method").set("both")
full_light_upload = os.environ.get("CYCLES_HOUDINI_DISABLE_RETAINED_LIGHT_UPLOAD") == "1"
report = dict(
    result="running",
    device=args.device,
    scope=__doc__,
    expected_rebuild=args.expect_rebuild,
    viewer_feedback=True,
    full_light_upload=full_light_upload,
    script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    events=[],
)
previous_profile = os.environ.get("CYCLES_HOUDINI_PROFILE_LIGHT_TREE")
os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = "1"
log_path = None


def cache_records():
    return (
        [
            json.loads(line.split(" ", 1)[1])
            for line in Path(log_path).read_text(errors="replace").splitlines()
            if line.startswith("CH_LIGHT_TREE_CACHE ")
        ]
        if log_path
        else []
    )


try:
    with ch.Session(
        device=args.device,
        width=f.width,
        height=f.height,
        samples=16,
        execution="worker",
        threads=2,
        use_texture_cache=False,
        collect_diagnostics=True,
        passes=["combined"],
    ) as session:
        session.set_feedback("delivery-benchmark", True)
        for index, value in enumerate((0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.25, 0.45, 0.65)):
            previous_records = len(cache_records())
            started = time.perf_counter()
            pass_fixture.parm(f.wave, "value").set(value)
            update = f.update()
            captured = time.perf_counter()
            generation = session.apply(update, reuse_if_unchanged=True)
            submitted = time.perf_counter()
            deadline = started + 120
            while time.perf_counter() < deadline:
                status = session.status()
                info = session.frame_info()
                assert status["state"] != "failed", status
                if info and info["generation"] == generation:
                    delivered = time.perf_counter()
                    break
                time.sleep(0.001)
            else:
                raise TimeoutError("No first worker image for field edit")
            session.notify_presented(generation, info["render_revision"])
            frame = session.wait(generation)
            finished = time.perf_counter()
            status = session.status()
            f.gray(frame, value)
            diagnostics = session.diagnostics()
            log_path = diagnostics["controller"]["worker_log"] or log_path
            cache = [record for record in cache_records()[previous_records:] if record.get("complete")]
            report.update(last_status=status, last_diagnostics=diagnostics, last_cache=cache)
            assert (
                not status["source_diagnostics"]
                and status["geometry_resources"] == status["mesh_variant_resources"] == 1
            ), status
            assert status["object_resources"] == 4 and status["subdivision_triangles"] >= 1000000, status
            counters = (status["displacement_rebuilds"], status["subdivision_tessellations"])
            if report["events"]:
                previous = report["events"][-1]["status"]
                assert status["renderer_pid"] == previous["renderer_pid"], status
                if args.expect_rebuild:
                    assert (
                        counters[0] > previous["displacement_rebuilds"]
                        and counters[1] > previous["subdivision_tessellations"]
                    ), status
                else:
                    assert counters == initial_counters, (initial_counters, counters)
                    assert status["owned_geometry_bytes"] == report["events"][0]["status"]["owned_geometry_bytes"], (
                        status
                    )
                    assert cache and all(record["hits"] >= 1 and record["misses"] == 0 for record in cache), cache
                    assert (
                        "optix temp vertex data" not in diagnostics["buffer_copy_requests"]
                        and "optix temp index data" not in diagnostics["buffer_copy_requests"]
                    ), diagnostics
                    for name in ("light_tree_nodes", "light_tree_emitters"):
                        copies = diagnostics["buffer_copy_requests"].get(name, {}).get("calls", 0)
                        assert (copies > 0) if full_light_upload else (copies == 0), (name, copies, diagnostics)
            else:
                initial_counters = counters
            report["events"].append(
                dict(
                    value=value,
                    warm=index > 2,
                    capture_seconds=captured - started,
                    submit_seconds=submitted - captured,
                    first_image_seconds=delivered - started,
                    complete_frame_seconds=finished - started,
                    first_image=info,
                    status=status,
                    capture=diagnostics["capture"]["events"][-1],
                    cache=cache,
                    diagnostics=diagnostics,
                )
            )
            path.write_text(json.dumps(report, indent=2))
        values = sorted(e["first_image_seconds"] for e in report["events"] if e["warm"])
        report["first_image_distribution"] = dict(
            count=len(values),
            median_seconds=values[len(values) // 2],
            p95_seconds=values[math.ceil(0.95 * len(values)) - 1],
            maximum_seconds=values[-1],
        )
        session.set_feedback("delivery-benchmark", False)
        session.close(wait=True)
        closed = session.status()
        assert closed["owned_geometry_bytes"] == closed["device_allocation_bytes"] == 0, closed
        report.update(result="passed", closed=closed)
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    if previous_profile is None:
        os.environ.pop("CYCLES_HOUDINI_PROFILE_LIGHT_TREE", None)
    else:
        os.environ["CYCLES_HOUDINI_PROFILE_LIGHT_TREE"] = previous_profile
    path.write_text(json.dumps(report, indent=2))
print(
    json.dumps(
        dict(result=report["result"], events=len(report["events"]), first_image=report.get("first_image_distribution"))
    )
)
