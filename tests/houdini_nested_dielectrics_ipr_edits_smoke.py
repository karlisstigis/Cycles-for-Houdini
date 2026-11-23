# SPDX-License-Identifier: Apache-2.0
"""Nested dielectrics: edits during active IPR accumulation (review request).
glass shell > ice > water (priorities 3, 2, 1) with packed glass beads, Nested Dielectrics on, IPR (worker) session.
Edits land while samples accumulate, paced and in bursts: priority, IOR, ice transform, ROP toggle off/on,
add/remove a participating object, packed priority attribute. Every edit must restart (render_revision bump,
sample count reset, frames never mix revisions), and the settled image after the last edit must equal a fresh
render of that final state. --part gpu: toggle off -> on -> off on the GPU, the final off image must equal a
fresh feature-off render; device memory is recorded. Generated scene only; no artist HIP is opened or saved."""

from support.paths import artifacts_root, artifact_label

import sys, os

sys.path.insert(0, os.path.join(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"], "tests"))
import pass_fixtures as pass_fixture
import argparse, json, time, traceback, subprocess
from pathlib import Path
import numpy as np
import hou
import cycles_houdini as ch
from cycles_houdini import native_scene, object_properties, registry, materials
from cycles_houdini.device_selection import set_devices

parser = argparse.ArgumentParser()
parser.add_argument("--device", default="cpu")
parser.add_argument("--part", default="edits")
parser.add_argument("--samples", type=int, default=256)
parser.add_argument("--edit-samples", type=int, default=65536)
parser.add_argument("--res", default="192x108")
parser.add_argument("--out", default="")
parser.add_argument("--threads", type=int, default=4)
args = parser.parse_args()
S = max(args.samples, args.edit_samples) if args.part == "edits" else args.samples
W, H = (int(v) for v in args.res.split("x"))
out = Path(args.out) if args.out else artifacts_root() / "nested-ipr-edits"
out.mkdir(parents=True, exist_ok=True)
report = dict(
    host=os.environ.get("CYCLES_HOUDINI_HOST_ROOT"),
    part=args.part,
    device=args.device,
    samples=args.samples,
    accumulation_samples=max(args.samples, args.edit_samples),
    resolution=[W, H],
    checks=[],
    edits=[],
    trace_summary={},
)


def check(name, **d):
    report["checks"].append(dict(name=name, **d))
    print("ok:", name, json.dumps(d, default=str)[:300], flush=True)


devs = ch.devices()
want = args.device.lower()
pick = [
    d
    for d in devs
    if d["id"] == args.device
    or (
        d["backend"].upper() == "CPU"
        if want == "cpu"
        else d["backend"].upper() == "OPTIX" and want in d["name"].lower()
    )
]
assert len(pick) == 1, (want, devs)
device = pick[0]
report["device_info"] = device

# ---------------------------------------------------------------- scene
hou.setFrame(1)
scene = hou.node("/obj").createNode("subnet", "nested_ipr_edits")


def material(name, shader, **values):
    builder = materials.create_material(name=name, shader=shader)
    node = builder.node("surface_shader")
    for key, value in values.items():
        parm = next(p for p in node.parmTuples() if p.name().lower() == key)
        parm.set(value if isinstance(value, tuple) else (value,))
    return builder


def ior_parm(builder):
    return next(p for p in builder.node("surface_shader").parms() if p.name().lower() == "ior")


glass_mat = material("glass", "glass_bsdf", ior=1.5)
water_mat = material("water", "glass_bsdf", ior=1.333, color=(0.8, 0.92, 1.0))
ice_mat = material("ice", "glass_bsdf", ior=1.309)
bead_mat = material("bead", "glass_bsdf", ior=1.7)
floor_mat = material("floor", "diffuse_bsdf", color=(0.7, 0.5, 0.3))
wall_mat = material("wall", "emission", color=(0.2, 0.6, 1.0))


def geo(name, priority, mat):
    node = scene.createNode("geo", name)
    for c in node.children():
        c.destroy()
    assert object_properties.install(node)
    node.parm("cycles_dielectric_priority").set(priority)
    node.parm("shop_materialpath").set(mat.path())
    return node


def show(node):
    node.setRenderFlag(True)
    node.setDisplayFlag(True)


glass = geo("glass_shell", 3, glass_mat)
s = glass.createNode("sphere")
s.parm("type").set(1)
s.parm("freq").set(16)
wall = glass.createNode("polyextrude")
wall.setInput(0, s)
wall.parm("dist").set(-0.08)
wall.parm("outputback").set(1)
show(wall)
glass.parm("ty").set(1)
water = geo("water", 1, water_mat)
s = water.createNode("sphere")
s.parm("type").set(1)
s.parm("freq").set(16)
s.parmTuple("rad").set((0.95, 0.95, 0.95))
show(s)
water.parm("ty").set(1)
ice = geo("ice", 2, ice_mat)
b = ice.createNode("box")
b.parmTuple("size").set((0.45, 0.45, 0.45))
show(b)
ice.parmTuple("t").set((0.1, 1.1, 0.1))
ice.parmTuple("r").set((20, 35, 10))
beads = geo("beads", 0, bead_mat)
proto = beads.createNode("sphere")
proto.parm("type").set(1)
proto.parmTuple("rad").set((0.12, 0.12, 0.12))
pts = beads.createNode("add")
pts.parm("points").set(3)
for i, x in enumerate((-0.5, 0, 0.5)):
    pts.parm("usept%d" % i).set(1)
    pts.parmTuple("pt%d" % i).set((x, -0.4, 0.3))
copies = beads.createNode("copytopoints")
copies.setInput(0, proto)
copies.setInput(1, pts)
copies.parm("pack").set(1)
field = beads.createNode("attribwrangle", "priority_field")
field.setInput(0, copies)
field.parm("class").set(1)
field.parm("snippet").set("i@cycles_dielectric_priority = array(4, 0, 1)[@primnum];")
show(field)
beads.parm("ty").set(1)
floor = geo("floor", 0, floor_mat)
g = floor.createNode("grid")
g.parmTuple("size").set((12, 12))
show(g)
back = geo("backdrop", 0, wall_mat)
g = back.createNode("grid")
g.parmTuple("size").set((6, 3))
g.parm("orient").set(0)
show(g)
back.parmTuple("t").set((0, 1.2, -2.5))
camera = hou.node("/obj").createNode("cam", "nested_ipr_camera")
pass_fixture.set_parameters(camera, dict(tx=0, ty=1.1, tz=4.2, resx=W, resy=H))
rop = hou.node("/out").createNode("cycles_ipr", "nested_ipr_rop")
set_devices(rop, device["id"])
pass_fixture.set_parameters(
    rop,
    dict(
        source_mode="objects",
        objectroot=scene.path(),
        camera=camera.path(),
        resx=W,
        resy=H,
        samples=S,
        ipr_samples=S,
        render_threads=args.threads,
        adaptive_sampling=0,
        ipr_adaptive_sampling=0,
        use_denoise=0,
        ipr_use_denoise=0,
        allowmotionblur=0,
        max_bounce=16,
        transmission_bounce=16,
        transparent_max_bounce=16,
        nested_dielectrics=1,
    ),
)
rop.parmTuple("background").set((0.85, 0.85, 0.9))

# ---------------------------------------------------------------- IPR helpers
entry = None
trace = []
last_seq = 0


def session():
    return entry.session


def poll():
    global last_seq
    info = session().frame_info(last_seq)
    if info:
        last_seq = info["sequence"]
        trace.append(
            dict(
                seq=info["sequence"],
                rev=info["render_revision"],
                gen=info["generation"],
                samples=info["samples"],
                current=info.get("current_scene"),
                t=time.monotonic(),
            )
        )
    return session().status()


def revision(st):
    return st.get("render_revision", st["generation"])


def open_entry():
    global entry, last_seq
    registry.close_all()
    entry = registry.for_rop(rop, native_scene.configuration(rop))
    last_seq = 0


def submit():
    return entry.apply(native_scene.capture(rop), hou.time())


def sample_budget(samples):
    """Keep edit windows long, then settle the same scene at comparison cost."""
    global S
    S = samples
    rop.setParms(dict(samples=samples, ipr_samples=samples))
    return submit()


def settle(generation, timeout=600):
    t0 = time.monotonic()
    while True:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        f = session().frame() if trace and trace[-1]["samples"] >= S else None
        if (
            f
            and f["generation"] == generation
            and f["complete"]
            and f.get("current_scene", True)
            and f["samples"] == S
            and not st.get("pending_update")
            and not st.get("capture_pending")
        ):
            return f, st
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("settle")
        time.sleep(0.005)


def wait_accumulating(min_rev, timeout=120):
    """Return as soon as a frame of revision >= min_rev shows partial samples (or completes)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        if trace and trace[-1]["rev"] >= min_rev and trace[-1]["samples"] >= 1:
            return trace[-1]
        time.sleep(0.002)
    raise TimeoutError("no accumulating frame for revision %d" % min_rev)


def plane(frame):
    v = frame["planes"]["combined"]
    return np.frombuffer(v["pixels"], np.float32).reshape(frame["height"], frame["width"], v["channels"]).copy()


def wait_engine_accumulating(rev, timeout=180, generation=None):
    """Headless sessions publish only the cleared and the final frame, so judge accumulation by engine progress.
    Waits for the newest revision >= rev to be mid-accumulation (or complete, which edit() then rejects)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        committed = st.get("committed_generation", st["generation"])
        if (
            revision(st) >= rev
            and not st.get("pending_update")
            and not st.get("capture_pending")
            and (generation is None or committed == generation)
        ):
            if st["state"] == "rendering" and 1 <= st.get("current_sample", 0) < S:
                return st
            if st["state"] == "complete":
                return st
        time.sleep(0.002)
    raise TimeoutError("no accumulation seen for revision >= %d" % rev)


def edit(label, action, paced=True):
    st = poll()
    before = revision(st)
    acc = wait_engine_accumulating(before) if paced else st
    at = acc.get("current_sample")
    mid = acc["state"] == "rendering" and at is not None and 1 <= at < S
    action()
    gen = submit()
    t0 = time.monotonic()
    reset = None
    while True:
        st = poll()
        if st["state"] == "failed":
            raise RuntimeError(st["error"])
        if revision(st) > before and reset is None and st.get("current_sample", S) < (at if mid else S):
            reset = st.get("current_sample")
        if revision(st) > before and reset is not None:
            break
        if time.monotonic() - t0 > 120:
            raise TimeoutError(label)
        time.sleep(0.002)
    record = dict(
        label=label,
        paced=paced,
        generation=gen,
        revision_before=before,
        revision_after=revision(st),
        engine_samples_at_edit=at,
        edited_mid_accumulation=mid,
        engine_samples_after_restart=reset,
    )
    report["edits"].append(record)
    print("edit:", json.dumps(record), flush=True)
    assert mid, ("edit did not land during accumulation (raise --samples/--res)", record)
    return gen


def verify_trace(label):
    bad = []
    for a, b in zip(trace, trace[1:]):
        if b["rev"] < a["rev"]:
            bad.append(("revision went back", a, b))
        elif b["rev"] == a["rev"] and b["samples"] < a["samples"]:
            bad.append(("samples went back within a revision", a, b))
        if b["gen"] < a["gen"]:
            bad.append(("generation went back", a, b))
    revs = {}
    for f in trace:
        revs.setdefault(f["rev"], []).append(f["samples"])
    report.setdefault("trace_head", {})[label] = [dict(f, t=round(f["t"] - trace[0]["t"], 3)) for f in trace[:60]]
    report["trace_summary"][label] = dict(
        frames=len(trace),
        revisions=len(revs),
        per_revision={str(k): [min(v), max(v), len(v)] for k, v in sorted(revs.items())},
        violations=[str(x)[:300] for x in bad[:10]],
    )
    assert not bad, bad[:3]
    check(
        label + ": frame trace monotonic (no revision/sample regression, no stale frames)",
        frames=len(trace),
        revisions=len(revs),
    )


def gpu_memory(pid):
    # Per-process usage is [N/A] under WDDM; report the whole board's used memory for the selected GPU.
    try:
        q = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        rows = [l.rsplit(",", 1) for l in q.strip().splitlines() if l.strip()]
        return {n.strip(): int(m) for n, m in rows if want in n.lower()}
    except Exception as e:
        return "unavailable: %s" % e


try:
    if args.part == "edits":
        open_entry()
        submit()
        st = wait_engine_accumulating(1)
        assert st["rendering"]["nested_dielectrics"] is True
        check("Initial nested render accumulating", revision=revision(st), engine_samples=st.get("current_sample"))
        extra = {}

        def add_extra():
            node = geo("extra_glass", 5, glass_mat)
            sp = node.createNode("sphere")
            sp.parm("type").set(1)
            sp.parmTuple("rad").set((0.35, 0.35, 0.35))
            show(sp)
            node.parmTuple("t").set((-0.45, 1.35, 0.35))
            extra["node"] = node

        steps = [
            ("Glass priority 3 -> 5", lambda: glass.parm("cycles_dielectric_priority").set(5)),
            ("Water IOR 1.333 -> 1.31", lambda: ior_parm(water_mat).set(1.31)),
            ("Ice transform", lambda: (ice.parm("ty").set(0.95), ice.parm("ry").set(60))),
            ("ROP Nested Dielectrics off", lambda: rop.parm("nested_dielectrics").set(0)),
            ("ROP Nested Dielectrics on", lambda: rop.parm("nested_dielectrics").set(1)),
            ("Add participating object", add_extra),
            (
                "Packed priority attribute change",
                lambda: field.parm("snippet").set("i@cycles_dielectric_priority = array(1, 6, -2)[@primnum];"),
            ),
            ("Remove participating object", lambda: extra["node"].destroy()),
        ]
        for label, action in steps:
            edit(label, action)
        mids = sum(e["edited_mid_accumulation"] for e in report["edits"])
        check("Paced edits each restarted the render", edits=len(steps), edited_mid_accumulation=mids)
        assert mids == len(steps), ("edits did not land during accumulation", mids)
        # Burst: several edits in quick succession, no waiting in between.
        burst = [
            ("Burst: glass priority 6", lambda: glass.parm("cycles_dielectric_priority").set(6)),
            ("Burst: ice rotate", lambda: ice.parm("rx").set(45)),
            ("Burst: water IOR 1.34", lambda: ior_parm(water_mat).set(1.34)),
            ("Burst: toggle off", lambda: rop.parm("nested_dielectrics").set(0)),
            ("Burst: toggle on", lambda: rop.parm("nested_dielectrics").set(1)),
            (
                "Burst: packed priority",
                lambda: field.parm("snippet").set("i@cycles_dielectric_priority = array(7, 0, 3)[@primnum];"),
            ),
        ]
        before = revision(poll())
        acc = wait_engine_accumulating(before)
        gens = []
        assert acc["state"] == "rendering" and 0 < acc.get("current_sample", 0) < S, (
            "burst did not start during accumulation",
            acc.get("current_sample"),
        )
        for label, action in burst:
            action()
            gens.append(submit())
            poll()
        # The GPU can finish 256 samples between two Python/native capture
        # calls. Edit windows use a long budget; comparison renders retain the
        # requested sample count and exercise the final burst's scene state.
        burst_generation = gens[-1]
        burst_status = wait_engine_accumulating(before + 1, generation=burst_generation)
        assert burst_status["state"] == "rendering" and burst_status.get("current_sample", 0) < S, burst_status
        comparison_generation = sample_budget(args.samples)
        final, st = settle(comparison_generation)
        report["edits"].append(
            dict(
                label="burst of %d" % len(burst),
                engine_samples_at_start=acc.get("current_sample"),
                generations=gens,
                revision_before=before,
                revision_after=revision(st),
                coalesced_updates=st.get("coalesced_updates"),
                burst_final_generation=burst_generation,
                comparison_generation=comparison_generation,
                committed_generation_before_comparison=burst_status.get(
                    "committed_generation", burst_status["generation"]
                ),
            )
        )
        assert revision(st) > before and final["generation"] == comparison_generation
        check(
            "Burst of edits settled on the last generation",
            revision_before=before,
            revision_after=revision(st),
            generations=gens,
        )
        verify_trace("IPR edits")
        settled = plane(final)
        np.save(out / "settled-after-edits.npy", settled)
        assert st["rendering"]["nested_dielectrics"] is True
        # Fresh session, same final state.
        open_entry()
        trace.clear()
        fresh, fst = settle(submit())
        fresh_px = plane(fresh)
        np.save(out / "fresh-final-state.npy", fresh_px)
        diff = np.abs(settled - fresh_px)
        report["final_compare"] = dict(
            max_abs=float(diff.max()),
            mean_abs=float(diff.mean()),
            differing_pixels=int(np.count_nonzero(diff.max(axis=2))),
            identical=bool(np.array_equal(settled, fresh_px)),
            settled_mean=float(settled[..., :3].mean()),
        )
        print("final compare", report["final_compare"], flush=True)
        if device["backend"].upper() == "CPU":
            assert report["final_compare"]["identical"], report["final_compare"]
        check("Settled image after the last edit equals a fresh render of the final state", **report["final_compare"])
    else:
        # GPU: off -> on -> off, settled, with memory; then off image vs a fresh feature-off render.
        rop.parm("nested_dielectrics").set(0)
        open_entry()
        mem = []

        def measure(label, st):
            m = dict(
                label=label,
                device_allocation_bytes=st.get("device_allocation_bytes"),
                device_allocation_peak_bytes=st.get("device_allocation_peak_bytes"),
                renderer_pid=st.get("renderer_pid"),
                nvidia_smi_board_used_mib=gpu_memory(0),
            )
            mem.append(m)
            print("memory", m, flush=True)

        f, st = settle(submit())
        measure("off (initial)", st)
        before = revision(st)
        rop.parm("nested_dielectrics").set(1)
        f, st = settle(submit())
        assert st["rendering"]["nested_dielectrics"] is True and revision(st) > before
        measure("on", st)
        before = revision(st)
        rop.parm("nested_dielectrics").set(0)
        gen = sample_budget(max(args.samples, args.edit_samples))
        # Also flip on/off once more during accumulation before settling off.
        toggles = []
        for value in (1, 0):
            time.sleep(0.3)
            acc = wait_engine_accumulating(revision(poll()))
            toggles.append(
                dict(
                    to=value,
                    revision=revision(acc),
                    engine_samples_at_edit=acc.get("current_sample"),
                    state=acc["state"],
                )
            )
            assert acc["state"] == "rendering" and 0 < acc.get("current_sample", 0) < S, (
                "GPU toggle not during accumulation",
                toggles[-1],
            )
            rop.parm("nested_dielectrics").set(value)
            gen = submit()
        report["gpu_toggles_mid_accumulation"] = toggles
        print("gpu toggles", toggles, flush=True)
        gen = sample_budget(args.samples)
        last, st = settle(gen)
        assert st["rendering"]["nested_dielectrics"] is False
        measure("off (after on)", st)
        verify_trace("GPU toggles")
        off_px = plane(last)
        open_entry()
        trace.clear()
        fresh, fst = settle(submit())
        measure("fresh off session", fst)
        fresh_px = plane(fresh)
        diff = np.abs(off_px - fresh_px)
        report["final_compare"] = dict(
            max_abs=float(diff.max()),
            mean_abs=float(diff.mean()),
            differing_pixels=int(np.count_nonzero(diff.max(axis=2))),
            identical=bool(np.array_equal(off_px, fresh_px)),
        )

        def compare(a, b):
            d = np.abs(a - b)
            return dict(
                max_abs=float(d.max()),
                mean_abs=float(d.mean()),
                differing_pixels=int(np.count_nonzero(d.max(axis=2))),
                identical=bool(np.array_equal(a, b)),
            )

        # Controls: is the GPU deterministic between fresh off sessions, and between two off-after-on sessions?
        open_entry()
        trace.clear()
        fresh2, _ = settle(submit())
        fresh2_px = plane(fresh2)
        report["control_fresh_off_vs_fresh_off"] = compare(fresh_px, fresh2_px)
        rop.parm("nested_dielectrics").set(1)
        settle(submit())
        rop.parm("nested_dielectrics").set(0)
        again, _ = settle(submit())
        report["control_off_after_on_vs_off_after_on"] = compare(off_px, plane(again))
        np.save(out / "gpu-off-after-on.npy", off_px)
        np.save(out / "gpu-fresh-off.npy", fresh_px)
        report["memory"] = mem
        print(
            "final compare",
            report["final_compare"],
            "controls",
            report["control_fresh_off_vs_fresh_off"],
            report["control_off_after_on_vs_off_after_on"],
            flush=True,
        )
        check("GPU off after on vs a fresh feature-off render", **report["final_compare"])
        ff = report["control_fresh_off_vs_fresh_off"]
        aa = report["control_off_after_on_vs_off_after_on"]
        af = report["final_compare"]
        if af["identical"]:
            conclusion = "off-after-on is bit-identical to a fresh off render: no state left behind"
        elif ff["identical"] and aa["identical"]:
            conclusion = "STATE LEFT BEHIND: GPU is deterministic per state, but off-after-on differs from fresh off"
        elif not ff["identical"] and ff["max_abs"] >= af["max_abs"] * 0.1:
            conclusion = "NORMAL GPU NONDETERMINISM: fresh off sessions differ from each other by a comparable amount"
        else:
            conclusion = "MIXED: fresh off sessions differ only slightly; off-after-on differs more (likely state left behind on top of GPU nondeterminism)"
        report["verdict"] = dict(
            fresh_off_vs_fresh_off=ff,
            off_after_on_vs_off_after_on=aa,
            off_after_on_vs_fresh_off=af,
            conclusion=conclusion,
        )
    report["result"] = "passed"
except Exception:
    report["result"] = "failed"
    report["error"] = traceback.format_exc()
    print(report["error"], flush=True)
finally:
    registry.close_all()
(out / ("ipr-edits-%s-%s.json" % (artifact_label(args.part), artifact_label(want)))).write_text(
    json.dumps(report, indent=1, default=str)
)
if report.get("verdict"):
    v = report["verdict"]
    for k in ("fresh_off_vs_fresh_off", "off_after_on_vs_off_after_on", "off_after_on_vs_fresh_off"):
        print(
            "VERDICT %s: max_abs=%.3g mean_abs=%.3g differing_pixels=%d identical=%s"
            % (k, v[k]["max_abs"], v[k]["mean_abs"], v[k]["differing_pixels"], v[k]["identical"]),
            flush=True,
        )
    print(
        "VERDICT memory (Cycles device bytes):",
        ", ".join("%s=%s" % (m["label"], m["device_allocation_bytes"]) for m in report.get("memory", [])),
        flush=True,
    )
    print("VERDICT conclusion:", v["conclusion"], flush=True)
print(json.dumps(dict(result=report["result"], checks=len(report["checks"]))), flush=True)
hou.exit(0 if report["result"] == "passed" else 1)
