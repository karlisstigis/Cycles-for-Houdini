# SPDX-License-Identifier: Apache-2.0
"""Qualify observed system-mouse bursts and matching painted render generations."""

import argparse
import json
import math
from pathlib import Path


def validate_render_devices(status, expected, *, require_committed=False):
    """A queued capture has no backend device until its first scene commits."""
    actual = status.get("render_device_ids", [])
    if actual or require_committed or status.get("committed_generation", 0) > 0:
        assert list(actual) == list(expected) and actual, ("Unexpected committed render devices", actual, expected)
    if require_committed:
        assert status.get("renderer_pid", 0) > 0 and status.get("committed_generation", 0) > 0, (
            "Renderer never committed the requested device"
        )


def mouse_burst_intervals(observations):
    """Retain the first release edge; later idle samples cannot extend a burst."""
    intervals = {}
    previous_at = -1.0
    for observation in observations:
        at = observation["at"]
        burst = observation["input_burst"]
        buttons = observation["mouse_buttons"]
        assert math.isfinite(at) and at >= previous_at >= -1.0, "Mouse observation times regressed"
        assert type(burst) is int and burst >= 0
        assert set(buttons).issubset({"left", "middle", "right"})
        previous_at = at
        if buttons:
            if not burst:
                continue  # A historical Qt sample without a captured down edge is insufficient.
            interval = intervals.setdefault(burst, dict(pressed_at=at, last_down_at=at, released_at=None))
            assert interval["released_at"] is None, "A released burst ID was reused for another press"
            interval["last_down_at"] = at
        elif burst in intervals and intervals[burst]["released_at"] is None:
            intervals[burst]["released_at"] = at
    return intervals


def mouse_event_at(events, at):
    """Use canonical event time, excluding input which happened after sampling."""
    return next((event for event in reversed(events) if event["at"] <= at), None)


def validate_records(
    records,
    paints=None,
    *,
    tolerance=1e-5,
    min_bursts=3,
    min_seconds=20.0,
    max_seconds=600.0,
    orbit_method="native viewport View tool",
    mouse_events=None,
):
    assert isinstance(records, list) and len(records) >= 2, "Need observed gesture records"
    assert type(min_bursts) is int and min_bursts >= 3, "Require at least three gesture bursts per family"
    assert 20 <= min_seconds < max_seconds <= 1200, "Invalid sustained-input time bounds"
    assert orbit_method in ("native viewport View tool", "native camera pivot rotation handle"), (
        "Undeclared orbit method"
    )
    fields = ("object_t", "object_r", "camera_t", "camera_r")
    matrices = ("object_world", "camera_world", "view_transform")
    for record in records:
        for field in fields + matrices:
            vector = record[field]
            assert len(vector) == (16 if field in matrices else 3) and all(math.isfinite(v) for v in vector), (
                field,
                vector,
            )
        for field in ("generation", "render_revision", "worker", "conversions", "input_burst"):
            assert type(record[field]) is int and record[field] >= 0, (field, record[field])
        assert math.isfinite(record["at"]) and record["at"] >= 0
        assert record["state"] not in ("failed", "closed", "cancelled"), record
    assert all(a["at"] <= b["at"] for a, b in zip(records, records[1:])), "Observation times regressed"
    assert all(a["generation"] <= b["generation"] for a, b in zip(records, records[1:])), "Render generation regressed"
    assert records[-1]["generation"] > records[0]["generation"], "Render generation never advanced"
    workers = {record["worker"] for record in records if record["worker"]}
    assert len(workers) == 1, ("Expected one stable nonzero renderer worker", workers)
    first_worker = next(i for i, record in enumerate(records) if record["worker"])
    assert all(record["worker"] in workers for record in records[first_worker:]), "Renderer worker disappeared"
    initialized = next((i for i, record in enumerate(records) if record["conversions"] > 0), None)
    assert initialized is not None, "Geometry was never converted"
    baseline_conversions = records[initialized]["conversions"]
    assert all(record["conversions"] == baseline_conversions for record in records[initialized:]), (
        "Navigation reconverted the geometry"
    )
    assert paints, "Need actual completed Qt paint observations"
    for paint in paints:
        assert paint["at"] >= 0 and math.isfinite(paint["at"])
        metadata, stats = paint["metadata"], paint["stats"]
        assert type(metadata["generation"]) is int and type(metadata["sequence"]) is int
        assert type(metadata["render_revision"]) is int and metadata["render_revision"] >= 0
        assert stats["finite"] and stats["width"] > 0 and stats["height"] > 0
        assert math.isfinite(stats["rgb_std"]) and math.isfinite(stats["rgb_mean"])
    painted_at = {}
    for paint in paints:
        if paint["stats"]["rgb_std"] > 0.001:
            identity = paint["metadata"]["generation"], paint["metadata"]["render_revision"]
            painted_at.setdefault(identity, []).append(paint["at"])

    def changed(a, b):
        return max(abs(x - y) for x, y in zip(a, b)) > tolerance

    intervals = mouse_burst_intervals(mouse_events if mouse_events is not None else records)
    # A native handle can commit on release or on the next host tick. Associate
    # the observed pose with its captured OS burst through release + 250 ms;
    # idle records must not stretch that window. A new physical render revision
    # and its matching nonblank Qt paint remain mandatory.
    families = dict(object_translation="object_t", object_rotation="object_r", camera_orbit="camera_r")
    phases = dict(object_translation="translate", object_rotation="rotate", camera_orbit="orbit")
    gestures = {}
    for family, field in families.items():
        events = {}
        for index in range(1, len(records)):
            before, edited = records[index - 1], records[index]
            burst = edited["input_burst"]
            if edited.get("phase") != phases[family]:
                continue
            interval = intervals.get(burst)
            if not interval or interval["released_at"] is None or not changed(before[field], edited[field]):
                continue
            if not interval["pressed_at"] <= edited["at"] <= interval["released_at"] + 0.25:
                continue
            if family == "camera_orbit" and (
                not changed(before["view_transform"], edited["view_transform"])
                or not changed(before["camera_world"], edited["camera_world"])
            ):
                continue
            accepted = next(
                (
                    record
                    for record in records[index:]
                    if not changed(record[field], edited[field])
                    and record["generation"] > before["generation"]
                    and record["render_revision"] > before["render_revision"]
                    and record["worker"] in workers
                    and record["conversions"] == baseline_conversions
                    and any(
                        max(before["at"], interval["pressed_at"]) <= at <= record["at"] + 0.1
                        for at in painted_at.get((record["generation"], record["render_revision"]), ())
                    )
                ),
                None,
            )
            if accepted:
                events[burst] = dict(
                    burst=burst,
                    edit_at=edited["at"],
                    accepted_at=accepted["at"],
                    before=before[field],
                    after=edited[field],
                    previous_generation=before["generation"],
                    accepted_generation=accepted["generation"],
                    previous_render_revision=before["render_revision"],
                    accepted_render_revision=accepted["render_revision"],
                    input_interval=interval,
                    observed_pose_change_window=[max(before["at"], interval["pressed_at"]), edited["at"]],
                )
        assert len(events) >= min_bursts, (
            family + " needs repeated system-mouse bursts with updated painted images",
            len(events),
            min_bursts,
        )
        gestures[family] = list(events.values())
    times = [event["edit_at"] for events in gestures.values() for event in events]
    active_span = max(times) - min(times)
    assert min_seconds <= active_span <= max_seconds, (
        "Observed gesture span outside qualification bounds",
        active_span,
    )
    return dict(
        result="passed",
        scope="Repeated OS-observed object translation/rotation and camera orbit with updated Qt paints",
        worker=next(iter(workers)),
        geometry_conversions=baseline_conversions,
        generation_range=[records[0]["generation"], records[-1]["generation"]],
        gestures=gestures,
        gesture_span_seconds=active_span,
        required_bursts_per_family=min_bursts,
        input_observer="independent OS events" if mouse_events is not None else "historical Qt samples",
        release_commit_window_seconds=0.25,
        camera_rotation_observed=True,
        updated_images_painted=True,
        orbit_method=orbit_method,
        viewport_tumble_chord_certified=False,
        image_appearance_certified=False,
        physical_latency_certified=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    original = json.loads(args.report.read_text(encoding="utf-8-sig"))
    result = validate_records(
        original["records"],
        original["paints"],
        orbit_method=original.get("orbit_method", "native viewport View tool"),
        mouse_events=original.get("mouse_events"),
        **original.get("qualification_bounds", {}),
    )
    result.update(
        source_report=str(args.report.resolve()),
        source_result=original.get("result"),
        fresh_reference=original.get("fresh_reference"),
        neutral_material_override_recorded=bool(original.get("material_override")),
    )
    reference = original.get("fresh_reference")
    if reference:
        assert reference["result"] == "passed"
        assert 0 <= reference["linear_mean_absolute_error"] < 0.002
        assert 0 <= reference["display_byte_mean_absolute_error"] < 1.0
        assert reference["live_generation"] == reference["painted_generation"]
        result["image_appearance_certified"] = True
    if args.output:
        assert args.output.resolve() != args.report.resolve(), "Preserve the original observed report"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
