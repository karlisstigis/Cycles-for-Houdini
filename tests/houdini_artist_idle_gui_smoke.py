# SPDX-License-Identifier: Apache-2.0
"""Observe automatic capture causes in an unchanged artist scene; never save it."""

from support.paths import artifacts_root

from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import time
import traceback

import hou
import numpy as np
from PySide6 import QtCore, QtTest
from cycles_houdini import _native, device_selection, native_scene, registry


class Workflow:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.source = artifacts_root() / "examples/sop-fx-courtyard/Test.hiplc"
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.report = dict(
            result="running",
            source=str(self.source),
            source_sha256=self.digest,
            samples=[],
            events=[],
            states=[],
            scope="Read-only scene load, one live viewer; reduced resolution/samples and break/repair edits in memory only",
        )
        hou.hipFile.load(str(self.source), suppress_save_prompt=True, ignore_load_warnings=True)
        rops = [node for node in hou.node("/out").allSubChildren() if node.type().name() == "cycles_ipr"]
        assert rops, "Scene has no Cycles ROP"
        self.report["rops"] = [node.path() for node in rops]
        selected = os.environ.get("CYCLES_HOUDINI_IDLE_ROP", "/out/Test_2")
        self.rop = next((node for node in rops if node.path() == selected), rops[0])
        self.report["rop"] = self.rop.path()
        self.report["original_rop"] = {
            parm.name(): parm.evalAsString()
            for parm in self.rop.parms()
            if parm.name()
            in (
                "camera",
                "samples",
                "ipr_samples",
                "resx",
                "resy",
                "objectroot",
                "vobject",
                "allowmotionblur",
                "use_denoise",
                "ipr_use_denoise",
            )
        }
        device_selection.set_devices(self.rop, [os.environ["CYCLES_HOUDINI_GUI_DEVICE"]])
        self.rop.setParms(dict(resx=320, resy=180, samples=16, ipr_samples=16))
        self.events = deque(maxlen=300)
        self.counts = Counter()
        self.original_changed = native_scene.SceneObserver._changed
        workflow = self

        def changed(observer, **kwargs):
            node = kwargs.get("node")
            parm = kwargs.get("parm_tuple")
            event = dict(
                node=node.path() if node else None,
                event=str(kwargs.get("event_type")),
                parm=parm.name() if parm is not None else None,
                template=str(parm.parmTemplate().type()) if parm is not None else None,
                closed=observer._closed,
                callback=type(getattr(observer.callback, "__self__", None)).__name__,
                rop_exists=hou.nodeBySessionId(observer._rop_identity) is not None,
                detail={
                    key: str(value) for key, value in kwargs.items() if key not in ("node", "parm_tuple", "event_type")
                },
            )
            workflow.events.append(dict(time=time.monotonic(), **event))
            workflow.counts[(event["node"], event["event"], event["parm"])] += 1
            try:
                return workflow.original_changed(observer, **kwargs)
            except Exception:
                workflow.report.setdefault("callback_errors", []).append(traceback.format_exc())
                raise

        native_scene.SceneObserver._changed = changed
        self.tab = hou.ui.curDesktop().createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(900, 650))
        self.tab.setActiveInterface(hou.pypanel.interfaceByName("cycles_houdini_render"))
        self.panel = None
        self.phase = "mount"
        self.started = time.monotonic()
        self.observing = self.anchor = None
        self.next_sample = 0
        self.busy = False
        self.stage = 0
        self.minimum_generation = 0
        self.actor = hou.node("/obj/circle1")
        self.original_tx = self.actor.parm("tx").eval()
        self.texture_parm = None
        self.original_texture = None
        self.original_image = None
        self.object_count = self.worker_pid = None
        self.last_edit_at = None
        hou.ui.addEventLoopCallback(self.tick)

    def tick(self):
        if self.busy:
            return
        self.busy = True
        try:
            now = time.monotonic()
            assert now - self.started < 180, self.report
            if self.phase == "mount":
                self.panel = self.tab.activeInterfaceRootWidget()
                if self.panel is None:
                    return
                self.panel.set_rop(self.rop)
                QtTest.QTest.mouseClick(self.panel.render_button, QtCore.Qt.LeftButton)
                self.phase = "observe"
                return
            entry = self.panel.entry
            if entry is None:
                return
            status = entry.session.status()
            assert not entry.error and status["state"] != "failed", (entry.error, status)
            if self.stage and self.observing is None:
                assert now - self.last_edit_at < 20, dict(
                    reason="Artist edit did not complete",
                    stage=self.stage,
                    debug=self.report.get("edit_debug"),
                    status=status,
                )
            frame = entry.session.frame_info()
            if (
                self.observing is None
                and frame
                and frame["complete"]
                and status["state"] == "complete"
                and not status.get("pending_update")
                and not status.get("capture_pending")
                and status["generation"] > self.minimum_generation
                and frame["generation"] == status["generation"]
                and self.panel._live_generation == frame["generation"]
            ):
                image = entry.session.frame()["planes"]["combined"]
                assert entry.observer is not None and not entry.observer._closed
                assert self.panel._observer is not None and not self.panel._observer._closed
                pixels = np.frombuffer(image["pixels"], np.float32).copy()
                assert np.isfinite(pixels).all() and np.any(pixels > 0.01)
                if self.stage == 0:
                    self.original_image = pixels
                    self.object_count = status["object_resources"]
                    self.worker_pid = status["renderer_pid"]
                    for path in entry.session.native_material_paths():
                        parent = hou.node(path)
                        if parent is None:
                            continue
                        for node in (parent, *parent.allSubChildren()):
                            parm = node.parm("filename")
                            if parm is not None and parm.evalAsString() == "op:/obj/circle1/copnet1/color":
                                self.texture_parm = parm
                                break
                        if self.texture_parm is not None:
                            break
                    assert self.texture_parm is not None, "Accepted COP image binding was not found"
                    assert not self.texture_parm.keyframes(), "Fixture expects a literal image binding"
                    self.original_texture = self.texture_parm.evalAsString()
                assert status["object_resources"] == self.object_count
                assert status["renderer_pid"] == self.worker_pid
                assert bool(status["source_diagnostics"]) == (self.stage == 3), status["source_diagnostics"]
                if self.stage == 1:
                    moved = np.abs(
                        np.clip(pixels.reshape(-1, 4)[:, :3], 0, 1)
                        - np.clip(self.original_image.reshape(-1, 4)[:, :3], 0, 1)
                    ).mean()
                    self.report["movement_mean_error"] = float(moved)
                    assert moved > 0.001, "Object translation did not update the image"
                elif self.stage in (2, 4, 5):
                    delta = np.abs(pixels - self.original_image)
                    # This artist scene retains its 16-sample HDR/denoising
                    # settings. Rare bright paths can differ on GPU after a
                    # BVH refit. Compare bounded RGB structure and exact alpha;
                    # the companion uniform COP tests retain exact color checks.
                    rgb = np.abs(
                        np.clip(pixels.reshape(-1, 4)[:, :3], 0, 1)
                        - np.clip(self.original_image.reshape(-1, 4)[:, :3], 0, 1)
                    )
                    alpha = delta.reshape(-1, 4)[:, 3]
                    comparison = dict(
                        stage=self.stage,
                        mean_error=float(delta.mean()),
                        max_error=float(delta.max()),
                        frame_generation=frame["generation"],
                        requested_generation=status["generation"],
                        bounded_rgb_mean=float(rgb.mean()),
                        bounded_rgb_p999=float(np.quantile(rgb, 0.999)),
                        alpha_max_error=float(alpha.max()),
                    )
                    self.report.setdefault("image_comparisons", []).append(comparison)
                    if not (rgb.mean() < 0.001 and np.quantile(rgb, 0.999) < 0.02 and alpha.max() < 1e-6):
                        np.savez(
                            self.path.parent / ("image-comparison-" + str(self.stage) + ".npz"),
                            original=self.original_image,
                            restored=pixels,
                        )
                        raise AssertionError(("Restored scene did not restore its image", comparison))
                self.observing = now
                self.anchor = status["generation"]
                self.events.clear()
                self.counts.clear()
            if now >= self.next_sample:
                self.next_sample = now + 0.5
                self.report["samples"].append(
                    dict(
                        time=now - self.started,
                        state=status["state"],
                        generation=status["generation"],
                        revision=status["render_revision"],
                        frame_generation=frame["generation"] if frame else None,
                        serial=entry.change_serial,
                        applied=entry.applied_serial,
                        pending=status.get("pending_update"),
                        capture_pending=status.get("capture_pending"),
                        cop=_native.cop_capture_stats(entry.session._handle),
                    )
                )
                self.report["events"] = list(self.events)
                self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
            if self.observing is not None and now - self.observing >= (12 if self.stage == 0 else 2):
                assert status["generation"] == self.anchor, dict(
                    reason="Idle artist scene keeps submitting scene captures",
                    before=self.anchor,
                    after=status["generation"],
                    status=status,
                    event_counts=[
                        dict(node=k[0], event=k[1], parm=k[2], count=v) for k, v in self.counts.most_common()
                    ],
                )
                self.report["states"].append(
                    dict(
                        stage=self.stage,
                        generation=status["generation"],
                        revision=status["render_revision"],
                        idle_seconds=now - self.observing,
                        objects=status["object_resources"],
                        diagnostics=status["source_diagnostics"],
                    )
                )
                self.minimum_generation = status["generation"]
                self.stage += 1
                self.observing = self.anchor = None
                self.last_edit_at = now
                if self.stage == 1:
                    self.actor.parm("tx").set(self.original_tx + 0.5)
                elif self.stage == 2:
                    self.actor.parm("tx").set(self.original_tx)
                elif self.stage == 3:
                    self.texture_parm.set("op:/obj/circle1/copnet1/missing_idle_test")
                elif self.stage == 4:
                    self.texture_parm.set(self.original_texture)
                elif self.stage == 5:
                    self.panel.queue_render()
                else:
                    self.finish()
                    return
                self.report.setdefault("edit_debug", []).append(
                    dict(
                        stage=self.stage,
                        serial=entry.change_serial,
                        auto=self.panel.auto.isChecked(),
                        stopped=self.panel._stopped,
                        observer_closed=entry.observer._closed if entry.observer else None,
                        panel_observer_closed=self.panel._observer._closed if self.panel._observer else None,
                        events=list(self.events),
                    )
                )
        except Exception:
            self.finish(traceback.format_exc())
        finally:
            self.busy = False

    def finish(self, error=None):
        if self.tick in hou.ui.eventLoopCallbacks():
            hou.ui.removeEventLoopCallback(self.tick)
        if self.panel:
            self.panel.dispose()
        registry.close_all()
        native_scene.SceneObserver._changed = self.original_changed
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.digest
        self.report.update(
            result="failed" if error else "passed",
            error=error,
            events=list(self.events),
            event_counts=[dict(node=k[0], event=k[1], parm=k[2], count=v) for k, v in self.counts.most_common()],
        )
        self.path.write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        hou.exit(1 if error else 0, suppress_save_prompt=True)


workflow = Workflow()
