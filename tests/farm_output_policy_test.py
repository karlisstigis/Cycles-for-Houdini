# SPDX-License-Identifier: Apache-2.0
"""Fast farm resume, full header contracts and sequence diagnostic policy tests."""

from support.package_loader import load_module, prepare_package
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini"
package = ModuleType("farm_policy_test_host")
package.__path__ = [str(source)]
sys.modules[package.__name__] = package
controls = ModuleType(package.__name__ + ".rop_controls")
controls.value = lambda rop, name, now: rop.settings[name]
sys.modules[controls.__name__] = controls
hou = ModuleType("hou")
hou.timeToFrame = lambda value: value
hou.isUIAvailable = lambda: False
hou.OperationInterrupted = type("OperationInterrupted", (Exception,), {})
sys.modules["hou"] = hou
package.registry = SimpleNamespace()
package.native_scene = SimpleNamespace(session_identity=lambda value: value)


def load(name):
    return load_module(package, name)


output = load("output")
final = load("final_render")


def spec(channels=("R", "G", "B", "A"), name="", **changes):
    fields = dict(
        x=0,
        y=0,
        z=0,
        width=32,
        height=24,
        depth=1,
        full_x=0,
        full_y=0,
        full_z=0,
        full_width=32,
        full_height=24,
        full_depth=1,
        channelnames=channels,
        nchannels=len(channels),
        tile_width=0,
        unreadable=False,
    )
    attrs = {
        "oiio:subimagename": name,
        "cycles_houdini:input_validation_version": 1,
        "cycles_houdini:missing_input_count": 0,
        "cycles_houdini:source_diagnostics": "[]",
    }
    attrs.update(changes.pop("attributes", {}))
    fields.update(changes)
    return SimpleNamespace(
        **fields,
        get_string_attribute=lambda name: attrs.get(name, ""),
        get_int_attribute=lambda name, default=0: attrs.get(name, default),
    )


class Image:
    def __init__(self, parts, format="openexr"):
        self.parts = parts
        self.index = 0
        self.format = format

    def seek_subimage(self, index, mip):
        self.index = index
        return index < len(self.parts)

    def spec(self):
        return self.parts[self.index]

    def format_name(self):
        return self.format

    def close(self):
        return True

    def geterror(self):
        return ""


class Rop:
    def __init__(self, strict=False):
        self.settings = {"output_existing": "overwrite"}
        self.strict = strict
        self.summary = None

    def sessionId(self):
        return id(self)

    def path(self):
        return "/out/farm"

    def parm(self, name):
        return SimpleNamespace(evalAtTime=lambda now: self.strict) if name == "fail_on_missing_inputs" else None

    def setCachedUserData(self, key, value):
        self.summary = value


class Session:
    def __init__(self, issues):
        self.issues = issues
        self.writes = []
        self.closed = False

    def apply(self, update, **kwargs):
        self.issues = update.get("source_diagnostics", self.issues)
        return 1

    def status(self):
        return dict(generation=1, state="complete", progress=1, source_diagnostics=self.issues)

    def frame_info(self):
        return dict(generation=1, complete=True)

    def write_exr(self, path, options):
        self.writes.append((path, dict(options)))

    def close(self):
        self.closed = True

    def cancel(self):
        pass


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "frame.exr"
        self.path.write_bytes(b"header fixture")
        self.originals = (output.plan, output.expected_headers, output.inspect_existing)
        self.images = {str(self.path): [spec()]}
        oiio = ModuleType("OpenImageIO")
        self.pixel_reads = []
        oiio.ImageInput = SimpleNamespace(
            open=lambda filename: Image(self.images[filename]) if filename in self.images else None
        )
        self.consumed_errors = []
        oiio.geterror = lambda: self.consumed_errors.append("consumed") or "fixture EXR open error"
        sys.modules["OpenImageIO"] = oiio

        def inspect_exr(filename, expected_parts, validate, checkpoint):
            stream = oiio.ImageInput.open(filename)
            if stream is None:
                raise RuntimeError(f"Existing output is not a readable EXR: {filename}: {oiio.geterror()}")
            if stream.format_name() != "openexr":
                raise RuntimeError(f"Existing output is not an EXR: {filename}")
            parts = stream.parts[: expected_parts + 1]
            if validate([output._existing_header(value) for value in parts]):
                for index, value in enumerate(parts):
                    if checkpoint is not None:
                        checkpoint()
                    self.pixel_reads.append((index, 0, value.nchannels))
                    if value.unreadable:
                        raise RuntimeError(
                            f"Existing EXR pixel data is unreadable; use Overwrite to replace it: {filename}"
                        )

        package._native = SimpleNamespace(inspect_existing_exr=inspect_exr)
        self.contract = dict(window=(0, 0, 32, 24, 0, 0, 32, 24), parts={"": ["R", "G", "B", "A"]}, multipart=False)
        self.sessions = []

        def factory(*args, **kwargs):
            result = Session([])
            self.sessions.append(result)
            return result

        package.registry.session_from_configuration = factory

    def tearDown(self):
        output.plan, output.expected_headers, output.inspect_existing = self.originals
        final._active.clear()
        self.temp.cleanup()

    def test_valid_header_and_absent_output(self):
        self.assertEqual(output.inspect_existing(self.path, self.contract), [])
        self.assertIsNone(output.inspect_existing(self.path.with_name("absent.exr"), self.contract))

    def test_directory_and_unreadable_are_not_skip_candidates(self):
        with self.assertRaisesRegex(ValueError, "regular file"):
            output.inspect_existing(self.path.parent, self.contract)
        self.images.clear()
        with self.assertRaisesRegex(ValueError, "readable EXR.*fixture EXR open error"):
            output.inspect_existing(self.path, self.contract)
        self.assertEqual(self.consumed_errors, ["consumed"])

    def test_non_exr_format_rejected(self):
        sys.modules["OpenImageIO"].ImageInput.open = lambda path: Image([spec()], format="tiff")
        with self.assertRaisesRegex(ValueError, "not an EXR"):
            output.inspect_existing(self.path, self.contract)

    def test_wrong_dimensions_window_or_channels_rejected(self):
        for changes in (dict(width=31), dict(x=2), dict(full_width=64), dict(channels=["R", "G", "B"])):
            with self.subTest(changes=changes):
                self.images[str(self.path)] = [spec(**changes)]
                with self.assertRaises(ValueError):
                    output.inspect_existing(self.path, self.contract)

    def test_multipart_names_missing_and_extra_parts_rejected(self):
        contract = dict(
            self.contract, multipart=True, parts={"Beauty": ["R", "G", "B", "A"], "N": ["N.X", "N.Y", "N.Z"]}
        )
        beauty = spec(name="Beauty")
        normal = spec(["N.X", "N.Y", "N.Z"], name="N")
        for parts in (
            [beauty],
            [beauty, spec(["N.X", "N.Y", "N.Z"], name="wrong")],
            [beauty, normal, spec(name="extra")],
        ):
            self.images[str(self.path)] = parts
            with self.assertRaises(ValueError):
                output.inspect_existing(self.path, contract)
        self.images[str(self.path)] = [normal, beauty]
        self.assertEqual(output.inspect_existing(self.path, contract), [])

    def test_stored_source_provenance_is_preserved_and_invalid_rejected(self):
        self.images[str(self.path)] = [
            spec(
                attributes={
                    "cycles_houdini:source_diagnostics": '[{"code":"texture.unavailable","source":"/tex/image"}]'
                }
            )
        ]
        self.assertEqual(output.inspect_existing(self.path, self.contract)[0]["source"], "/tex/image")
        self.images[str(self.path)] = [spec(attributes={"cycles_houdini:source_diagnostics": "{}"})]
        with self.assertRaisesRegex(ValueError, "provenance"):
            output.inspect_existing(self.path, self.contract)

    def setup_render(self, rop, files=None):
        jobs = [dict(filename=str(path), options={}) for path in (files or [self.path])]
        output.plan = lambda *args: jobs
        output.expected_headers = lambda *args, **kwargs: [self.contract] * len(jobs)
        return jobs

    def test_clean_skip_avoids_session_creation(self):
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertEqual(rop.summary["skipped_frames"], 1)
        self.assertEqual(self.sessions, [])

    def test_partial_set_preserves_existing_and_uses_native_skip_for_each_target(self):
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        missing = self.path.with_name("missing.exr")
        self.setup_render(rop, [self.path, missing])
        final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertEqual(rop.summary["completed_frames"], 1)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(
            [(path, options["existing_files"]) for path, options in self.sessions[0].writes],
            [(self.path, "skip"), (missing, "skip")],
        )
        self.assertEqual(self.pixel_reads, [(0, 0, 4)])
        self.assertEqual(self.path.read_bytes(), b"header fixture")

    def test_existing_file_replaced_after_preflight_is_revalidated_by_writer(self):
        rop = Rop(strict=True)
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        staged = self.path.with_name("concurrent.exr")
        staged.write_bytes(b"new external bytes")

        def factory(*args, **kwargs):
            session = Session([])
            self.sessions.append(session)
            staged.replace(self.path)
            return session

        package.registry.session_from_configuration = factory
        final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertEqual(
            [(path, options["existing_files"]) for path, options in self.sessions[0].writes], [(self.path, "skip")]
        )
        self.assertEqual(self.path.read_bytes(), b"new external bytes")

    def test_unreadable_existing_pixels_reject_partial_set_before_any_publication(self):
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        missing = self.path.with_name("missing.exr")
        self.setup_render(rop, [missing, self.path])
        self.images[str(self.path)] = [spec(unreadable=True)]
        with self.assertRaisesRegex(ValueError, "pixel data is unreadable.*Overwrite"):
            final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertFalse(self.sessions)
        self.assertEqual(self.pixel_reads, [(0, 0, 4)])
        self.assertFalse(missing.exists())
        self.assertEqual(self.path.read_bytes(), b"header fixture")

    def test_foreground_validation_runs_off_hom_and_interrupt_precedes_any_write(self):
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        missing = self.path.with_name("missing.exr")
        self.setup_render(rop, [self.path, missing])
        token = object()
        main_thread = threading.get_ident()
        inspect = output.inspect_existing
        entered = threading.Event()
        unblock = threading.Event()
        threads = []
        interrupt = True

        def inspect_off_hom(*args, **kwargs):
            threads.append(threading.get_ident())
            if len(threads) == 1:
                entered.set()
                self.assertTrue(unblock.wait(5), "Foreground cancellation waited for the blocked reader")
            return inspect(*args, **kwargs)

        class InterruptOperation:
            def __enter__(self):
                return self

            def __exit__(self, *error):
                pass

            def updateProgress(self, fraction):
                if interrupt:
                    raise hou.OperationInterrupted("Stopped checking existing files")

        with (
            patch.object(hou, "isUIAvailable", return_value=True),
            patch.object(hou, "InterruptableOperation", return_value=InterruptOperation(), create=True),
            patch.object(output, "inspect_existing", side_effect=inspect_off_hom),
            patch.object(package.registry, "begin_local_final", return_value=token, create=True),
            patch.object(package.registry, "end_local_final", create=True) as release,
        ):
            try:
                with self.assertRaises(hou.OperationInterrupted):
                    final.render_frame(rop, ("CPU", 32, 24), {}, 1)
                self.assertTrue(entered.is_set())
                self.assertTrue(rop.summary["cancelled"])
                pending = final._output_validation
                self.assertFalse(pending.finished.is_set())
                self.assertTrue(pending.stopped.is_set())
                release.assert_called_once_with(token)
                with self.assertRaises(hou.OperationInterrupted):
                    final.render_frame(rop, ("CPU", 32, 24), {}, 1)
                self.assertIs(final._output_validation, pending)
                self.assertEqual(len(threads), 1)
                self.assertNotEqual(threads[0], main_thread)
            finally:
                unblock.set()
            self.assertTrue(pending.finished.wait(2))
            self.assertIsInstance(pending.error, final.RenderCancelled)
            interrupt = False
            self.setup_render(rop)
            final.render_frame(rop, ("CPU", 32, 24), {}, 1)
            self.assertEqual(rop.summary["skipped_frames"], 1)
            self.assertIsNone(final._output_validation)
        self.assertFalse(self.sessions)
        self.assertFalse(missing.exists())
        self.assertEqual(self.path.read_bytes(), b"header fixture")

    def test_background_stop_between_parts_precedes_any_write(self):
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        self.contract.update(multipart=True, parts={"Beauty": ["R", "G", "B", "A"], "Depth": ["Z"]})
        self.images[str(self.path)] = [spec(name="Beauty"), spec(("Z",), name="Depth")]
        self.setup_render(rop)

        def observer(event, node):
            if event == "output-validation" and node is rop and self.pixel_reads:
                raise final.RenderCancelled("Stopped before the next part")

        with final.observe(observer), self.assertRaises(final.RenderCancelled):
            final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertTrue(rop.summary["cancelled"])
        self.assertEqual(self.pixel_reads, [(0, 0, 4)])
        self.assertFalse(self.sessions)
        self.assertEqual(self.path.read_bytes(), b"header fixture")

    def test_invalid_partial_set_fails_before_any_publication(self):
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop, [self.path.with_name("missing.exr"), self.path])
        self.images.clear()
        with self.assertRaises(ValueError):
            final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertFalse(self.sessions)

    def test_strict_skip_checks_current_inputs_and_blocks_every_file(self):
        rop = Rop(strict=True)
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        issue = dict(
            code="texture.unavailable", source="/textures/missing.exr", resource="shader", message="Cannot open texture"
        )
        with self.assertRaisesRegex(RuntimeError, "texture.unavailable"):
            final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=[issue]), 1)
        self.assertEqual(len(self.sessions), 1)
        self.assertFalse(self.sessions[0].writes)
        self.assertEqual(rop.summary["source_diagnostics"][0]["frame"], 1)
        self.assertTrue(self.sessions[0].closed)

    def test_earlier_frame_diagnostics_survive_clean_frame_and_skip(self):
        rop = Rop()
        state = final.Sequence(rop)
        final._active[rop.sessionId()] = state
        self.setup_render(rop)
        issue = dict(code="geometry.cook_failed", source="/obj/broken/OUT", resource="mesh", message="Cook failed")
        final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=[issue]), 1)
        final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=[]), 2)
        rop.settings["output_existing"] = "skip"
        final.render_frame(rop, ("CPU", 32, 24), {}, 3)
        self.assertEqual(rop.summary["completed_frames"], 2)
        self.assertEqual(rop.summary["skipped_frames"], 1)
        self.assertEqual(rop.summary["source_diagnostics"][0]["frame"], 1)
        self.assertIn("geometry.cook_failed", rop.summary["warnings"][0])
        self.assertIn("/obj/broken/OUT", rop.summary["warnings"][0])

    def test_strict_skip_rejects_fallback_pixels_after_current_scene_repair(self):
        rop = Rop(strict=True)
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        self.images[str(self.path)] = [
            spec(
                attributes={
                    "cycles_houdini:missing_input_count": 1,
                    "cycles_houdini:source_diagnostics": '[{"code":"texture.unavailable","source":"/textures/old.exr","message":"Missing"}]',
                }
            )
        ]
        with self.assertRaisesRegex(ValueError, "texture.unavailable"):
            final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=[]), 1)
        self.assertFalse(self.sessions)
        issue = rop.summary["source_diagnostics"][0]
        self.assertEqual((issue["frame"], issue["source"], issue["existing_output"]), (1, "/textures/old.exr", True))

    def test_external_header_keeps_fast_permissive_skip_but_fails_strict(self):
        self.images[str(self.path)] = [
            spec(attributes={"cycles_houdini:input_validation_version": 0, "cycles_houdini:missing_input_count": -1})
        ]
        rop = Rop()
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertFalse(self.sessions)
        rop.strict = True
        with self.assertRaisesRegex(ValueError, "uncertified.*Overwrite"):
            final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        self.assertFalse(self.sessions)
        self.assertEqual(rop.summary["source_diagnostics"][0]["code"], "output.uncertified_inputs")

    def test_strict_multipart_requires_certification_for_every_part(self):
        contract = dict(
            self.contract, multipart=True, parts={"Beauty": ["R", "G", "B", "A"], "N": ["N.X", "N.Y", "N.Z"]}
        )
        self.images[str(self.path)] = [
            spec(name="Beauty"),
            spec(["N.X", "N.Y", "N.Z"], name="N", attributes={"cycles_houdini:input_validation_version": 0}),
        ]
        with self.assertRaisesRegex(ValueError, "uncertified"):
            output.inspect_existing(self.path, contract, require_clean_inputs=True)

    def test_strict_concurrent_winner_diagnostics_reach_failure_report(self):
        rop = Rop(strict=True)
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        self.path.unlink()

        def factory(*args, **kwargs):
            session = Session([])

            def write(*args):
                self.path.write_bytes(b"concurrent winner")
                self.images[str(self.path)] = [
                    spec(
                        attributes={
                            "cycles_houdini:missing_input_count": 1,
                            "cycles_houdini:source_diagnostics": '[{"code":"geometry.cook_failed","source":"/obj/race/OUT","message":"Failed cook"}]',
                        }
                    )
                ]
                raise RuntimeError("Existing EXR contains missing render inputs: concurrent winner")

            session.write_exr = write
            return session

        package.registry.session_from_configuration = factory
        with self.assertRaisesRegex(RuntimeError, "concurrent winner"):
            final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=[]), 7)
        issue = rop.summary["source_diagnostics"][0]
        self.assertEqual(
            (issue["code"], issue["source"], issue["frame"], issue["existing_output"]),
            ("geometry.cook_failed", "/obj/race/OUT", 7, True),
        )

    def test_strict_clean_skip_checks_current_scene_and_accepts_certified_pixels(self):
        rop = Rop(strict=True)
        rop.settings["output_existing"] = "skip"
        self.setup_render(rop)
        final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=[]), 1)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(rop.summary["skipped_frames"], 1)

    def test_source_diagnostics_distinguish_same_code_different_paths(self):
        rop = Rop()
        self.setup_render(rop)
        issues = [dict(code="texture.unavailable", source=name, message="Missing") for name in ("/tex/a", "/tex/b")]
        final.render_frame(rop, ("CPU", 32, 24), dict(source_diagnostics=issues), 1)
        self.assertEqual(len(rop.summary["warnings"]), 2)

    def test_engine_quality_warning_does_not_fail_strict_inputs(self):
        rop = Rop(strict=True)
        self.setup_render(rop)
        final.render_frame(
            rop, ("CPU", 32, 24), dict(source_diagnostics=[dict(code="render.nested_probe_limit", message="Limit")]), 1
        )
        self.assertEqual(len(self.sessions[0].writes), 1)
        self.assertTrue(self.sessions[0].writes[0][1]["fail_on_missing_inputs"])

    def test_standalone_primary_channels_and_multipart_crypto_contract(self):
        package.passes = SimpleNamespace(
            catalogue=lambda: [
                dict(id="combined", channels=["R", "G", "B", "A"]),
                dict(id="normal", channels=["X", "Y", "Z"]),
            ],
            entries=lambda *args: [dict(type="crypto_object", source="crypto_object", levels=4)],
        )
        prepare_package(package)
        rop = Rop()
        rop.settings.update(resolution_percent=100, resx=32, resy=24, render_region=False)
        one = dict(filename="x.exr", options=dict(passes=[dict(source="normal", layer="N")]))
        self.assertEqual(output.expected_headers(rop, 1, [one])[0]["parts"], {"": ["R", "G", "B"]})
        cropped = output.expected_headers(rop, 1, [one], render_crop=(3, 4, 20, 12))[0]
        self.assertEqual(cropped["window"], (3, 8, 20, 12, 0, 0, 32, 24))
        one["options"]["crop_to_region"] = True
        self.assertEqual(
            output.expected_headers(rop, 1, [one], render_crop=(3, 4, 20, 12))[0]["window"],
            (0, 0, 20, 12, 0, 0, 20, 12),
        )
        multi = dict(
            filename="x.exr",
            options=dict(
                multipart=True,
                passes=[
                    dict(source="combined", layer=""),
                    dict(source="CryptoObject00", layer="IDs00"),
                    dict(source="CryptoObject01", layer="IDs01"),
                ],
            ),
        )
        self.assertEqual(
            output.expected_headers(rop, 1, [multi])[0]["parts"],
            {
                "Beauty": ["R", "G", "B", "A"],
                "IDs": ["IDs00.R", "IDs00.G", "IDs00.B", "IDs00.A", "IDs01.R", "IDs01.G", "IDs01.B", "IDs01.A"],
            },
        )


class ForegroundWriter(Session):
    """Controlled acknowledgement on the same boundary as WorkerSession."""

    def __init__(self, *, immediate=False, on_dispatch=None, error=""):
        super().__init__([])
        self.event = None
        self.waits = 0
        self.closes = []
        self.immediate = immediate
        self.on_dispatch = on_dispatch
        self.error = error

    def begin_write_exr(self, filename, options):
        self.writes.append(Path(filename))
        self.waits = 0
        self.event = SimpleNamespace(ready=False, wait=self.wait, error=self.error)
        if self.on_dispatch:
            self.on_dispatch(Path(filename))
        if self.immediate:
            self.acknowledge()
        return self.event

    def acknowledge(self):
        self.event.ready = True

    def wait(self, seconds):
        self.waits += 1
        if self.waits == 4:
            # Terminate the old uninterruptible loop in the failure control.
            self.acknowledge()

    def write_finished(self, event):
        return event.ready

    def finish_write_exr(self, event):
        assert event.ready
        if event.error:
            raise RuntimeError(event.error)

    def close(self, *, wait=True):
        self.closes.append((wait, self.event.ready))


class ForegroundWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.primary = Path(self.temp.name) / "primary.exr"
        self.sibling = self.primary.with_name("normal.exr")
        self.paths = [self.primary]
        self.callbacks, self.dialogs, self.trace, self.pauses, self.received = [], [], [], set(), []
        self.writer = ForegroundWriter()
        self.rop = Rop()
        self.on_progress = lambda label: None
        self.factory = lambda: self.writer
        self.patch_all(
            hou,
            isUIAvailable=lambda: True,
            InterruptableOperation=self.progress,
            ui=SimpleNamespace(
                addEventLoopCallback=self.callbacks.append, removeEventLoopCallback=self.callbacks.remove
            ),
        )
        self.patch_all(
            package.registry,
            begin_local_final=self.pause,
            end_local_final=self.pauses.remove,
            session_from_configuration=lambda *args, **kwargs: self.factory(),
            trace=lambda event, **fields: self.trace.append((event, fields)),
        )
        self.patch_all(
            output,
            plan=lambda *args: [dict(filename=str(path), options={}) for path in self.paths],
            header_metadata=lambda *args: {},
        )
        for name, empty in (
            ("_active", {}),
            ("_active_writes", []),
            ("_output_retirements", []),
            ("_viewer_receivers", {}),
        ):
            self.patch_all(final, **{name: empty})

    def patch_all(self, target, **attrs):
        for name, value in attrs.items():
            replace = patch.object(target, name, value, create=True)
            replace.start()
            self.addCleanup(replace.stop)

    def pause(self):
        token = object()
        self.pauses.add(token)
        return token

    @contextmanager
    def progress(self, label, **kwargs):
        self.dialogs.append(label)
        yield SimpleNamespace(updateProgress=lambda _: self.on_progress(label))

    def render(self, rop=None):
        rop = self.rop if rop is None else rop
        final._active[rop.sessionId()] = final.Sequence(rop)
        final.render_frame(rop, ("CPU", 32, 24), {}, 1)
        final.finish(rop)

    def cancel_while_writing(self, action=None, *, error=final.RenderCancelled):
        def at_write(label):
            if label == "Saving Cycles output":
                (action or (lambda: final.request_cancel(self.rop)))()

        final._viewer_receivers[self.rop.sessionId()] = lambda session: self.received.append(session) or True
        self.on_progress = at_write
        with self.assertRaises(error):
            self.render()
        self.assertTrue(self.rop.summary["cancelled"])
        self.assertEqual(self.rop.summary["completed_frames"], 0)
        self.assertEqual(self.writer.writes, [self.primary])
        self.assertFalse(self.pauses or self.received or self.writer.closes)

    def finish_cancelled_writer(self):
        self.writer.acknowledge()
        for callback in tuple(self.callbacks):
            callback()
        self.assertEqual(self.writer.closes, [(False, True)])
        self.assertFalse(self.callbacks)

    def test_stop_after_dispatch_preserves_winner_and_stops_sibling(self):
        self.paths.append(self.sibling)
        self.writer.on_dispatch = lambda path: path.write_bytes(b"competing publication")
        self.cancel_while_writing()
        self.finish_cancelled_writer()
        self.assertEqual(self.primary.read_bytes(), b"competing publication")
        self.assertFalse(self.sibling.exists())

    def test_native_interrupt_during_lone_file(self):
        def interrupt():
            raise hou.OperationInterrupted("Stop while waiting for EXR acknowledgement")

        self.cancel_while_writing(interrupt, error=hou.OperationInterrupted)
        self.finish_cancelled_writer()

    def test_quick_ack_skips_dialog_and_retains_healthy_viewer_session(self):
        self.writer.immediate = True
        final._viewer_receivers[self.rop.sessionId()] = lambda session: self.received.append(session) or True
        self.render()
        self.assertEqual(self.rop.summary["completed_frames"], 1)
        self.assertEqual(self.received, [self.writer])
        self.assertFalse(self.writer.closes or self.pauses)
        self.assertNotIn("Saving Cycles output", self.dialogs)

    def test_stop_wins_race_against_fast_lone_ack(self):
        self.writer.immediate = True
        self.writer.on_dispatch = lambda path: final.request_cancel(self.rop)
        with self.assertRaises(final.RenderCancelled):
            self.render()
        self.assertEqual(self.rop.summary["completed_frames"], 0)
        self.assertNotIn("Saving Cycles output", self.dialogs)
        self.finish_cancelled_writer()

    def test_late_write_error_retires_without_changing_cancelled_frame(self):
        self.writer.error = "late file I/O error"
        self.cancel_while_writing()
        self.finish_cancelled_writer()
        self.assertEqual(self.trace, [("cancelled_output_write_failed", {"error": "late file I/O error"})])
        self.assertEqual(self.rop.summary["reason"], "cancelled")

    def test_other_rop_same_path_waits_without_acquiring_pause_and_can_be_cancelled(self):
        self.cancel_while_writing()
        retry = Rop()

        def cancel_retry(label):
            if label == "Finishing previous Cycles output":
                self.assertFalse(self.pauses)
                final.request_cancel(retry)

        self.on_progress = cancel_retry
        with self.assertRaises(final.RenderCancelled):
            self.render(retry)
        self.assertFalse(self.writer.event.ready or self.pauses)
        replacement = ForegroundWriter(immediate=True, on_dispatch=lambda path: path.write_bytes(b"replacement"))

        def start_replacement():
            self.assertEqual(self.writer.closes, [(False, True)])
            return replacement

        self.factory = start_replacement
        self.on_progress = lambda label: (
            self.writer.acknowledge() if label == "Finishing previous Cycles output" else None
        )
        self.render(retry)
        self.assertEqual(retry.summary["completed_frames"], 1)
        self.assertEqual(self.primary.read_bytes(), b"replacement")
        self.assertFalse(self.callbacks or self.pauses)

    def test_reentrant_scene_close_detaches_the_writer_once(self):
        self.cancel_while_writing(final.close_all)
        self.finish_cancelled_writer()

    def test_reentrant_rop_deletion_detaches_the_writer_once(self):
        self.cancel_while_writing(lambda: final.finish(self.rop, reason="node-deleted"))
        self.finish_cancelled_writer()

    def test_reentrant_restart_waits_for_active_writer_before_it_is_detached(self):
        retry = Rop()
        replacement = ForegroundWriter(immediate=True, on_dispatch=lambda path: path.write_bytes(b"replacement"))
        started = False

        def start_replacement():
            self.assertTrue(self.writer.event.ready)
            self.assertFalse(self.writer.closes)
            return replacement

        def progress(label):
            nonlocal started
            if label == "Saving Cycles output" and not started:
                started = True
                final.request_cancel(self.rop)
                self.factory = start_replacement
                self.render(retry)
            if label == "Finishing previous Cycles output":
                self.writer.acknowledge()

        self.on_progress = progress
        with self.assertRaises(final.RenderCancelled):
            self.render()
        self.assertTrue(started)
        self.finish_cancelled_writer()
        self.assertEqual(retry.summary["completed_frames"], 1)
        self.assertEqual(self.primary.read_bytes(), b"replacement")
        self.assertFalse(self.pauses)


if __name__ == "__main__":
    unittest.main()
