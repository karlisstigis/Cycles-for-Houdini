# SPDX-License-Identifier: Apache-2.0
import copy
import unittest

from system_input_evidence import validate_records, validate_render_devices, mouse_burst_intervals, mouse_event_at


def identity():
    return [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


class GestureEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            dict(
                at=0.0,
                object_t=[0.0, 0.0, 0.0],
                object_r=[0.0, 0.0, 0.0],
                camera_t=[0.0, 0.0, 7.0],
                camera_r=[0.0, 0.0, 0.0],
                object_world=identity(),
                camera_world=identity(),
                view_transform=identity(),
                generation=1,
                render_revision=1,
                worker=42,
                conversions=1,
                state="rendering",
                input_burst=0,
                mouse_buttons=[],
            )
        ]
        self.paints = []
        for index, field in enumerate(("object_t",) * 3 + ("object_r",) * 3 + ("camera_r",) * 3, 1):
            record = copy.deepcopy(self.records[-1])
            record.update(at=index * 5.0, input_burst=index, mouse_buttons=["left"])
            record["phase"] = dict(object_t="translate", object_r="rotate", camera_r="orbit")[field]
            record[field][0] += 0.1
            if field == "camera_r":
                record["view_transform"][1] += 0.1
                record["camera_world"][1] += 0.1
            else:
                record["object_world"][1] += 0.1
            self.records.append(record)
            accepted = copy.deepcopy(record)
            accepted.update(at=record["at"] + 0.2, generation=index + 1, render_revision=index + 1, mouse_buttons=[])
            self.records.append(accepted)
            self.paints.append(
                dict(
                    at=record["at"] + 0.1,
                    metadata=dict(generation=index + 1, render_revision=index + 1, sequence=index),
                    stats=dict(finite=True, width=640, height=360, rgb_mean=0.5, rgb_std=0.1),
                )
            )

    def validate(self):
        return validate_records(self.records, self.paints)

    def test_repeated_gestures_updated_images_and_stable_worker(self):
        result = self.validate()
        self.assertEqual(result["worker"], 42)
        self.assertTrue(result["camera_rotation_observed"])
        self.assertTrue(result["updated_images_painted"])
        self.assertFalse(result["image_appearance_certified"])
        self.assertFalse(result["physical_latency_certified"])
        self.assertEqual(result["gesture_span_seconds"], 40.0)
        self.assertTrue(all(len(events) == 3 for events in result["gestures"].values()))

    def test_finish_without_each_actual_transform_fails(self):
        for field in ("object_t", "object_r", "camera_r"):
            with self.subTest(field=field):
                records = copy.deepcopy(self.records)
                for record in records:
                    record[field] = records[0][field]
                with self.assertRaises(AssertionError):
                    validate_records(records, self.paints)

    def test_static_generation_restart_or_reconversion_fails(self):
        for field, value in (("generation", 1), ("worker", 43), ("worker", 0), ("conversions", 2)):
            with self.subTest(field=field, value=value):
                records = copy.deepcopy(self.records)
                records[-1][field] = value
                with self.assertRaises(AssertionError):
                    validate_records(records, self.paints)

    def test_many_samples_from_one_drag_are_not_repeated_gestures(self):
        for record in self.records:
            record["input_burst"] = 1
        with self.assertRaises(AssertionError):
            self.validate()

    def test_pose_writes_without_mouse_do_not_qualify(self):
        for record in self.records:
            record["mouse_buttons"] = []
        with self.assertRaises(AssertionError):
            self.validate()

    def test_wheel_dolly_without_camera_rotation_does_not_qualify(self):
        for record in self.records:
            record["camera_t"][2] -= record["at"] * 0.01
            record["camera_r"] = [0.0, 0.0, 0.0]
        with self.assertRaises(AssertionError):
            self.validate()

    def test_rotation_without_native_view_change_does_not_qualify_orbit(self):
        for record in self.records:
            record["view_transform"] = identity()
        with self.assertRaises(AssertionError):
            self.validate()

    def test_rotation_without_camera_world_change_does_not_qualify_orbit(self):
        for record in self.records:
            record["camera_world"] = identity()
        with self.assertRaises(AssertionError):
            self.validate()

    def test_camera_handle_variant_is_explicit_and_not_viewport_chord_claim(self):
        result = validate_records(self.records, self.paints, orbit_method="native camera pivot rotation handle")
        self.assertEqual(result["orbit_method"], "native camera pivot rotation handle")
        self.assertFalse(result["viewport_tumble_chord_certified"])

    def test_unpainted_updated_generations_do_not_qualify(self):
        for paint in self.paints:
            paint["metadata"]["generation"] = 1
        with self.assertRaises(AssertionError):
            self.validate()

    def test_logical_generation_retags_without_new_physical_revision_fail(self):
        for record in self.records:
            record["render_revision"] = 1
        for paint in self.paints:
            paint["metadata"]["render_revision"] = 1
        with self.assertRaises(AssertionError):
            self.validate()

    def test_new_generation_with_stale_painted_revision_fails(self):
        for paint in self.paints:
            paint["metadata"]["render_revision"] = 1
        with self.assertRaises(AssertionError):
            self.validate()

    def release_commits(self, delay=0.0):
        records = copy.deepcopy(self.records)
        for index in range(1, len(records), 2):
            for field in (
                "object_t",
                "object_r",
                "camera_t",
                "camera_r",
                "object_world",
                "camera_world",
                "view_transform",
            ):
                records[index][field] = copy.deepcopy(records[index - 1][field])
            records[index + 1]["at"] += delay
        return records

    def test_native_pose_committed_on_release_or_next_tick_is_accepted(self):
        for delay in (0.0, 0.1, 0.25):
            with self.subTest(delay=delay):
                result = validate_records(self.release_commits(delay), self.paints, mouse_events=self.records)
                self.assertEqual(len(result["gestures"]["camera_orbit"]), 3)
                self.assertEqual(result["input_observer"], "independent OS events")

    def test_independent_observer_preserves_bursts_missed_by_qt_ticks(self):
        records = copy.deepcopy(self.records)
        for record in records:
            record["mouse_buttons"] = []
        result = validate_records(records, self.paints, mouse_events=self.records)
        self.assertEqual(len(result["gestures"]["object_translation"]), 3)

    def test_programmatic_or_stale_commits_outside_real_burst_window_fail(self):
        with self.assertRaises(AssertionError):
            validate_records(self.release_commits(0.3), self.paints, mouse_events=self.records)
        events = copy.deepcopy(self.records)
        for event in events:
            event["mouse_buttons"] = []
        with self.assertRaises(AssertionError):
            validate_records(self.records, self.paints, mouse_events=events)

    def test_pose_cannot_claim_unrelated_later_burst(self):
        records = copy.deepcopy(self.records)
        for index in range(1, len(records), 2):
            records[index]["input_burst"] += 1
        with self.assertRaises(AssertionError):
            validate_records(records, self.paints, mouse_events=self.records)

    def test_idle_ticks_do_not_extend_release_window(self):
        observations = [
            dict(at=1.0, input_burst=1, mouse_buttons=["left"]),
            dict(at=1.2, input_burst=1, mouse_buttons=[]),
            dict(at=100.0, input_burst=1, mouse_buttons=[]),
        ]
        self.assertEqual(mouse_burst_intervals(observations)[1]["released_at"], 1.2)

    def test_rotation_pivot_translation_does_not_count_as_translation_tool(self):
        records = copy.deepcopy(self.records)
        for record in records:
            if record.get("phase") == "translate":
                record["phase"] = "rotate"
        with self.assertRaises(AssertionError):
            validate_records(records, self.paints, mouse_events=self.records)

    def test_canonical_edge_corrects_delayed_state_snapshot_without_future_input(self):
        events = [
            dict(at=76.0, input_burst=7, mouse_buttons=[]),
            dict(at=81.234, input_burst=8, mouse_buttons=["left"]),
            dict(at=81.251, input_burst=8, mouse_buttons=[]),
            dict(at=88.0, input_burst=9, mouse_buttons=["left"]),
        ]
        self.assertEqual(mouse_event_at(events, 81.248)["input_burst"], 8)
        self.assertEqual(mouse_event_at(events, 81.300)["input_burst"], 8)
        self.assertEqual(mouse_event_at(events, 81.248)["mouse_buttons"], ["left"])

    def test_blank_or_nonfinite_paints_do_not_qualify(self):
        for field, value in (("rgb_std", 0.0), ("finite", False)):
            with self.subTest(field=field):
                paints = copy.deepcopy(self.paints)
                for paint in paints:
                    paint["stats"][field] = value
                with self.assertRaises(AssertionError):
                    validate_records(self.records, paints)

    def test_insufficient_sustained_time_rejected(self):
        for record in self.records:
            record["at"] *= 0.1
        for paint in self.paints:
            paint["at"] *= 0.1
        with self.assertRaises(AssertionError):
            self.validate()

    def test_cannot_lower_required_burst_or_time_bounds(self):
        for bounds in (dict(min_bursts=1), dict(min_seconds=1), dict(max_seconds=2000)):
            with self.subTest(bounds=bounds):
                with self.assertRaises(AssertionError):
                    validate_records(self.records, self.paints, **bounds)

    def test_queued_extraction_without_backend_device_is_allowed(self):
        validate_render_devices(
            dict(render_device_ids=[], renderer_pid=0, committed_generation=0, backend_state="idle"), ["CPU"]
        )

    def test_committed_device_required_at_finish(self):
        for status in (
            dict(render_device_ids=[], renderer_pid=42, committed_generation=1),
            dict(render_device_ids=["CPU"], renderer_pid=0, committed_generation=0),
            dict(render_device_ids=["wrong"], renderer_pid=42, committed_generation=1),
        ):
            with self.subTest(status=status):
                with self.assertRaises(AssertionError):
                    validate_render_devices(status, ["CPU"], require_committed=True)
        validate_render_devices(
            dict(render_device_ids=["CPU"], renderer_pid=42, committed_generation=1), ["CPU"], require_committed=True
        )


if __name__ == "__main__":
    unittest.main()
