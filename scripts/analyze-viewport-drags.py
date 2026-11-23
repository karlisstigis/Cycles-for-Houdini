# SPDX-License-Identifier: Apache-2.0
"""Archive a verified native-drag run and correlate accepted poses with paint.

Run immediately after run-prototype-test.ps1, before restaging or changing source:
  python scripts/analyze-viewport-drags.py <GUI report> <unique archive name> --manifest <absolute build-manifest.json>
Latest-delivered-input ages are freshness observations, not causal input latency.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("report", type=Path)
parser.add_argument("archive")
parser.add_argument(
    "--manifest", type=Path, required=True, help="Explicit absolute manifest for the build which produced this report"
)
args = parser.parse_args()
report_path = args.report
report_bytes = report_path.read_bytes()
report = json.loads(report_bytes.decode("utf-8-sig"))
assert report["result"] == "passed"
manifest_path = args.manifest
assert manifest_path.is_absolute() and manifest_path.is_file(), "An explicit absolute manifest file is required"
manifest_bytes = manifest_path.read_bytes()
assert hashlib.sha256(manifest_bytes).hexdigest() == report["manifest_sha256"]
archive = root / "artifacts" / args.archive
archive = archive.resolve()
archive.relative_to((root / "artifacts").resolve())
archive.mkdir(parents=True, exist_ok=True)
manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
for entry in manifest["integration_source"]:
    path = root / entry["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    destination = archive / "sources" / entry["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assert destination.read_bytes() == path.read_bytes()
    else:
        shutil.copyfile(path, destination)
for content, name in ((manifest_bytes, "build-manifest.json"), (report_bytes, "report.json")):
    destination = archive / name
    if destination.exists():
        assert destination.read_bytes() == content, "Archive belongs to another run"
    else:
        destination.write_bytes(content)


def distribution(values):
    values = sorted(values)
    if not values:
        return dict(count=0)
    index = (len(values) - 1) * 0.95
    p95 = values[math.floor(index)] + (values[math.ceil(index)] - values[math.floor(index)]) * (index % 1)
    return dict(
        count=len(values), median_ms=statistics.median(values) * 1000, p95_ms=p95 * 1000, max_ms=max(values) * 1000
    )


submissions = {int(k): v for k, v in report["submissions"].items()}
events = {}
for case in report["drag_cases"]:
    for event in case.get("controller_events", []):
        events[event["submission"], event["physical_generation"]] = event
for event in report["final_diagnostics"]["controller"]["events"]:
    events[event["submission"], event["physical_generation"]] = event
results = []


def effect_pose(effect, case):
    if case["kind"] == "object" and effect["node"] == "/obj/heavy_packed" and effect["parm"] == "t":
        expected = list(case["object_before"])
        assert expected[:12] == [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0] and expected[15] == 1.0
        expected[12:15] = effect["value"]
        return expected
    return effect.get("parameter_pose", effect.get("world"))


for case in report["drag_cases"]:
    inputs = {record["index"]: record for record in case["inputs"]}
    effects = {}
    for record in case["effects"]:
        if (
            record["node"] == ("/obj/courtyard_camera" if case["kind"] == "camera" else "/obj/heavy_packed")
            and record["input"] is not None
        ):
            effects.setdefault(record["input"], record["at"])
    selected = []
    seen = set()
    seen_poses = []
    first_by_input = {}
    notification_latencies = []
    for paint in report["canvas_paints"]:
        metadata = paint["metadata"]
        generation = metadata.get("generation")
        revision = metadata.get("render_revision")
        command = submissions.get(generation)
        accepted = events.get((generation, revision))
        if accepted and case["kind"] == "object" and accepted["navigation_kind"] == "objects":
            # A prepared object revision can be committed using a newer logical
            # submission carrying a camera overlay. Match the prepared roots,
            # rather than attributing that image to the newest host acquisition.
            candidates = [
                record
                for record in submissions.values()
                if record["phase"] == case["phase"]
                and record.get("root_revision") is not None
                and record["root_revision"] == accepted["prepared_root_revision"]
            ]
            command = min(candidates, key=lambda record: record["start"]) if candidates else None
        if not command or command["phase"] != case["phase"] or command["input"] not in inputs:
            continue
        if not accepted or not accepted["accepted"]:
            continue
        if case["kind"] == "object" and accepted["navigation_kind"] != "objects":
            continue
        if paint["at"] < command["start"]:
            continue
        pose = command.get("world")
        before = case["camera_before" if case["kind"] == "camera" else "object_before"]
        if pose and max(abs(a - b) for a, b in zip(pose, before)) < 1e-7:
            continue
        notifications = [
            effect["at"]
            for effect in case["effects"]
            if pose
            and effect["at"] <= command["start"]
            and effect_pose(effect, case)
            and effect["node"] == ("/obj/courtyard_camera" if case["kind"] == "camera" else "/obj/heavy_packed")
            and max(abs(a - b) for a, b in zip(effect_pose(effect, case), pose)) < 1e-7
        ]
        # Measure first image of each pose; later samples/refinement at the same
        # pose are not another input-response sample.
        if pose and any(max(abs(a - b) for a, b in zip(pose, old)) < 1e-7 for old in seen_poses):
            continue
        identity = revision
        if identity in seen:
            continue
        seen.add(identity)
        if pose:
            seen_poses.append(pose)
        event = inputs[command["input"]]
        first_by_input.setdefault(event["index"], paint["at"] - event["deadline"])
        notification_latency = (paint["at"] - min(notifications)) * 1000 if notifications else None
        if notification_latency is not None:
            notification_latencies.append(notification_latency / 1000)
        selected.append(
            dict(
                input=event["index"],
                at=paint["at"],
                generation=generation,
                revision=revision,
                latest_delivered_input_age_ms=(paint["at"] - event["deadline"]) * 1000,
                matching_pose_notification_to_paint_ms=notification_latency,
                prepared_root_revision=accepted.get("prepared_root_revision"),
                samples=metadata.get("samples"),
                width=metadata.get("rendered_width"),
                height=metadata.get("rendered_height"),
                while_dragging=case["pressed"] <= paint["at"] <= case["released"],
                navigation_kind=accepted["navigation_kind"],
                engine_apply_ms=accepted["apply_seconds"] * 1000,
                queue_to_commit_ms=accepted["submission_to_commit_seconds"] * 1000,
            )
        )
    in_drag = [paint for paint in selected if paint["while_dragging"]]
    gaps = [b["at"] - a["at"] for a, b in zip(in_drag, in_drag[1:])]
    result = dict(
        phase=case["phase"],
        posted=len(inputs),
        delivered=case["delivered_events"],
        distinct_effect_inputs=len(effects),
        native_state=case["native_state"],
        producer_lateness=distribution([max(0, e["posted"] - e["deadline"]) for e in inputs.values()]),
        deadline_to_delivery=distribution(
            [e["delivered"] - e["deadline"] for e in inputs.values() if "delivered" in e]
        ),
        posted_to_delivery=distribution([e["delivered"] - e["posted"] for e in inputs.values() if "delivered" in e]),
        latest_delivered_input_age_at_native_effect=distribution(
            [at - inputs[index]["deadline"] for index, at in effects.items()]
        ),
        latest_delivered_input_age_at_paint=distribution(list(first_by_input.values())),
        matching_pose_notification_to_paint=distribution(notification_latencies),
        matching_pose_notification_to_paint_during_drag=distribution(
            [
                paint["matching_pose_notification_to_paint_ms"] / 1000
                for paint in in_drag
                if paint["matching_pose_notification_to_paint_ms"] is not None
            ]
        ),
        host_acquisition=distribution(
            [command["end"] - command["start"] for command in submissions.values() if command["phase"] == case["phase"]]
        ),
        accepted_images_during_drag=len(in_drag),
        accepted_images=len(selected),
        preview_gaps=distribution(gaps),
        images=selected,
        controller_events=[
            event
            for event in case.get("controller_events", [])
            if event["submission"] in submissions and submissions[event["submission"]]["phase"] == case["phase"]
        ],
    )
    results.append(result)
summary = dict(
    result="passed",
    source_report=str(report_path),
    source_snapshot=str(archive),
    manifest_sha256=report["manifest_sha256"],
    settings=report["settings"],
    fixture=report["fixture"],
    packed_fixture=report["packed_fixture"],
    expected_device_ids=report["expected_device_ids"],
    cases=results,
    scope="Synthetic native mouse gestures: exact deadline-to-Qt-delivery times, and exact native-pose notifications matched to command acquisition and accepted physical revisions at Qt paint. Latest-input ages are lower-bound freshness observations: Houdini can queue native events after Qt delivery, so that label is not a causal input-to-image measurement. No physical display or input-latency certification.",
)
(archive / "timing-analysis.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps({key: summary[key] for key in ("settings", "packed_fixture", "expected_device_ids")}, indent=2))
for case in results:
    print(json.dumps({key: value for key, value in case.items() if key not in ("images", "controller_events")}))
