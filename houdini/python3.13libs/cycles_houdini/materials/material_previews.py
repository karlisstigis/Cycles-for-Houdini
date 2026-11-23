# SPDX-License-Identifier: Apache-2.0
"""Bounded material swatches through Houdini's native thumbnail interface.

HOM capture/publication stays on the host thread. One persistent worker renders
one swatch at a time; a single background converter prepares display pixels.
"""

from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import hou
from .. import _native, material_graph
from . import BUILDER, VOLUME_BUILDER, WORLD_BUILDER, LIGHT_BUILDER, MaterialSourceError
from ..ipr.queued import WorkerSession

_service = None
_installed = False
_warnings_dialog = None
_domains = {BUILDER: "surface", VOLUME_BUILDER: "volume", WORLD_BUILDER: "world", LIGHT_BUILDER: "surface"}


class PreviewService:
    pending_limit = 16
    watch_limit = 16

    def __init__(self, device="CPU", *, samples=32, idle_seconds=10, busy=None):
        self.device, self.samples, self.idle_seconds = device, samples, idle_seconds
        self.pending = OrderedDict()
        self.watches = OrderedDict()
        self.errors = OrderedDict()
        self.events = deque(maxlen=64)
        self.worker = self.active = self.retiring = self.executor = None
        self.size = self.domain = self.fixture = None
        self.last_tick = 0.0
        self.last_work = time.monotonic()
        self.last_material = None
        self.texture_refresh_requested = False
        self.last_time = hou.time()
        self.closed = self.paused = False
        self.started = self.completed = self.discarded = self.coalesced = 0
        self.busy = busy or self._foreground_busy

    @staticmethod
    def _host():
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Material preview control requires the Houdini host thread")

    @staticmethod
    def _foreground_busy():
        from ..ipr import registry
        from ..rop import final_render

        if final_render.active_sequences():
            return True
        for entry in registry.entries():
            if entry.paused or entry.error:
                continue
            controller = getattr(entry.session, "_controller", None)
            if controller:
                if getattr(entry.session, "_active_capture", None) or getattr(entry.session, "_pending_capture", None):
                    return True
                value = controller.status()
                if value["state"] == "rendering" or value.get("pending_update"):
                    return True
        return False

    def request(self, node, size=128):
        self._host()
        if self.closed:
            raise RuntimeError("Material preview service is closed")
        while node and node.type().name() not in _domains:
            node = node.parent()
        if node is None:
            raise ValueError("Choose a Cycles material or one of its shader nodes")
        return _native.request_thumbnail(node.sessionId(), size)

    def _unwatch(self, identifier):
        record = self.watches.pop(identifier, None)
        if record:
            for node in record["nodes"]:
                try:
                    node.removeEventCallback(record["events"], record["callback"])
                except hou.ObjectWasDeleted:
                    pass

    def _watch(self, node, size):
        identifier = node.sessionId()
        self._unwatch(identifier)

        def changed(**kwargs):
            if kwargs.get("event_type") == hou.nodeEventType.BeingDeleted and kwargs.get("node") == node:
                self.errors.pop(identifier, None)
                self.pending.pop(identifier, None)
                self._unwatch(identifier)
                return
            if not self.closed:
                try:
                    self.request(node, size)
                except (hou.ObjectWasDeleted, ValueError):
                    self._unwatch(identifier)

        nodes = (node,) + node.allSubChildren()
        events = (
            hou.nodeEventType.ParmTupleChanged,
            hou.nodeEventType.InputRewired,
            hou.nodeEventType.ChildCreated,
            hou.nodeEventType.ChildDeleted,
            hou.nodeEventType.BeingDeleted,
            hou.nodeEventType.NameChanged,
            hou.nodeEventType.FlagChanged,
        )
        for child in nodes:
            child.addEventCallback(events, changed)
        self.watches[identifier] = dict(nodes=nodes, callback=changed, size=size, events=events)
        while len(self.watches) > self.watch_limit:
            self._unwatch(next(iter(self.watches)))

    def _error(self, token, message):
        if not _native.current_thumbnail(token):
            return
        node = hou.nodeBySessionId(token[0])
        message = message[:8192] + ("\n(details truncated)" if len(message) > 8192 else "")
        previous = self.errors.pop(token[0], None)
        self.errors[token[0]] = dict(revision=token[1], source=node.path() if node else "<deleted>", message=message)
        while len(self.errors) > self.watch_limit:
            self.errors.popitem(last=False)
        if hou.isUIAvailable() and (not previous or previous["message"] != message):
            hou.ui.setStatusMessage(
                "Cycles preview warning: "
                + self.errors[token[0]]["source"]
                + " — see Viewer information > Material Preview Warnings",
                severity=hou.severityType.Warning,
            )

    def _clear_error(self, identifier):
        previous = self.errors.pop(identifier, None)
        if previous and hou.isUIAvailable():
            hou.ui.setStatusMessage("Cycles material preview repaired: " + previous["source"])

    @staticmethod
    def _fallback(domain):
        if domain == "world":
            return dict(world={})
        return dict(
            materials=[
                dict(
                    key="preview/subject",
                    kind="principled_volume" if domain == "volume" else "diagnostic_surface",
                    color=(1, 0, 1),
                )
            ]
        )

    def _retire(self):
        if self.worker:
            self.worker.close(wait=False)
            self.retiring = self.worker
            self.worker = None
            self.size = self.domain = self.fixture = None
            self.texture_refresh_requested = False

    def _start(self, token):
        node = hou.nodeBySessionId(token[0])
        if node is None or not _native.current_thumbnail(token):
            return
        self._watch(node, token[2])
        warning = ""
        domain = _domains[node.type().name()]
        world_medium = False
        try:
            graph = material_graph(node, self.last_time)
            world_medium = domain == "world" and graph.get("volume") is not None
            update = (
                dict(world=dict(graph=graph, source_path=node.path()))
                if domain == "world"
                else dict(materials=[dict(key="preview/subject", kind="graph", graph=graph)])
            )
        except MaterialSourceError as error:
            warning = str(error)
            update = self._fallback(domain)
        if self.worker is None:
            self.worker = WorkerSession(
                device=self.device,
                width=token[2],
                height=token[2],
                samples=self.samples,
                threads=2,
                execution="worker",
                use_texture_cache=True,
                auto_texture_cache=False,
                denoising=dict(enabled=False),
                collect_diagnostics=True,
            )
            self.size = token[2]
        if self.fixture != (domain, world_medium):
            desired = _native.thumbnail_fixture(self.worker._handle, domain, world_medium)
            self.worker.notify_color_configuration()
            self.worker._controller.submit(desired)
            self.domain = domain
            self.fixture = (domain, world_medium)
        update["background"] = (0.12, 0.12, 0.12)
        generation = self.worker.apply(update, at_time=self.last_time)
        self.active = dict(
            token=token,
            generation=generation,
            warning=warning,
            fallback=bool(warning),
            domain=domain,
            cancelled=False,
            future=None,
            began=time.monotonic(),
        )
        self.last_material = (token[0], token[2])
        self.started += 1
        self.last_work = time.monotonic()
        self.events.append(dict(event="started", token=token, generation=generation, domain=domain))

    def tick(self, *, force=False):
        self._host()
        if self.closed:
            return
        now = time.monotonic()
        if not force and now - self.last_tick < 0.05:
            return
        self.last_tick = now
        if self.retiring:
            self.retiring.status()
            if not self.retiring._retirement_done:
                return
            self.retiring = None
        current_time = hou.time()
        if current_time != self.last_time:
            self.last_time = current_time
            for identifier, watch in tuple(self.watches.items()):
                node = hou.nodeBySessionId(identifier)
                if node:
                    self.request(node, watch["size"])
        for token in _native.take_thumbnails():
            if token[0] in self.pending:
                self.coalesced += 1
                self.pending.pop(token[0])
            self.pending[token[0]] = (token, now)
        while len(self.pending) > self.pending_limit:
            self.pending.popitem(last=False)
            self.discarded += 1
        foreground = self.busy()
        if self.worker and foreground != self.paused:
            self.worker.pause(foreground)
            self.paused = foreground
        # status() can advance owned COP acquisition on the host. Keep that
        # preparation out of the main render's active update interval as well.
        if foreground:
            return
        if self.worker and self.active is None:
            cached = self.worker._controller.status()
            if cached["state"] == "failed" and not cached.get("update_rejected"):
                self.events.append(dict(event="worker_failed", message=cached["error"]))
                self._retire()
                self.paused = False
                return
            changed = self.worker.textures_changed()
            if not changed:
                self.texture_refresh_requested = False
            if changed and not self.texture_refresh_requested and not self.pending and self.last_material:
                node = hou.nodeBySessionId(self.last_material[0])
                if node:
                    self.request(node, self.last_material[1])
                    self.texture_refresh_requested = True
        active = self.active
        if active:
            token = active["token"]
            stale = not _native.current_thumbnail(token)
            status = self.worker.status()
            future = active["future"]
            if stale and not active["cancelled"]:
                self.worker.cancel()
                active["cancelled"] = True
            if future is not None:
                if not future.done():
                    return
                image = future.result()
                if not stale and image:
                    if _native.publish_thumbnail(token, image["rgba"]):
                        self.completed += 1
                        issues = status.get("source_diagnostics", [])
                        message = active["warning"] or "; ".join(
                            issue["message"] + " — " + issue["action"] for issue in issues
                        )
                        if message:
                            self._error(token, message)
                        else:
                            self._clear_error(token[0])
                        self.events.append(
                            dict(event="published", token=token, seconds=now - active["began"], status=status)
                        )
                else:
                    self.discarded += 1
                self.active = None
                self.last_work = now
            elif (
                active["cancelled"]
                and not status.get("capture_pending")
                and status["state"] in ("cancelled", "complete", "idle")
            ):
                self.active = None
                self.discarded += 1
            elif status["state"] == "failed" and not status.get("update_rejected"):
                _native.publish_thumbnail(token, b"", status["error"] or status["message"])
                self._error(token, status["error"])
                self.active = None
                self._retire()
                return
            elif status.get("update_rejected") or status.get("rejected_update"):
                if active["fallback"]:
                    message = status.get("error") or status.get("message") or "Preview update rejected"
                    _native.publish_thumbnail(token, b"", message)
                    self._error(token, message)
                    self.active = None
                else:
                    active["fallback"] = True
                    active["warning"] = status.get("error") or status.get("message") or str(status)
                    active["generation"] = self.worker.apply(self._fallback(active["domain"]), at_time=self.last_time)
            elif not foreground:
                info = self.worker.frame_info()
                if (
                    info
                    and info["complete"]
                    and info["generation"] == active["generation"]
                    and not status.get("pending_update")
                    and not status.get("capture_pending")
                ):
                    if self.executor is None:
                        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Cycles swatch display")
                    frame = self.worker.display_frame()
                    if frame:
                        settings = dict(channels="rgba", checkerboard=False, exposure=0, gamma=1)
                        active["future"] = self.executor.submit(_native.display_image, frame[0], settings)
        if self.active is None and not foreground and self.pending:
            identifier, (token, queued_at) = next(iter(self.pending.items()))
            if now - queued_at < 0.15:
                return
            if self.worker and self.size != token[2]:
                self._retire()
                self.paused = False
                return
            self.pending.pop(identifier)
            try:
                self._start(token)
            except (ValueError, MaterialSourceError, hou.ObjectWasDeleted) as error:
                _native.publish_thumbnail(token, b"", str(error))
                self._error(token, str(error))
        if not self.active and not self.pending and self.worker and now - self.last_work > self.idle_seconds:
            self._retire()
            self.paused = False

    def diagnostics(self):
        return dict(
            started=self.started,
            completed=self.completed,
            discarded=self.discarded,
            coalesced=self.coalesced,
            active=int(self.active is not None),
            pending=len(self.pending),
            watches=len(self.watches),
            worker=int(self.worker is not None),
            retiring=int(self.retiring is not None),
            domain=self.domain,
            paused=self.paused,
            errors=dict(self.errors),
            events=list(self.events),
            native=_native.thumbnail_stats(),
        )

    def close(self, *, wait=False):
        self._host()
        self.closed = True
        self.pending.clear()
        if self is _service and _warnings_dialog is not None:
            _warnings_dialog.close()
        if self.active and self.active["future"] and wait:
            self.active["future"].result()
        for identifier in tuple(self.watches):
            self._unwatch(identifier)
        for session in (self.worker, self.retiring):
            if session:
                session.close(wait=wait)
        if self.executor:
            self.executor.shutdown(wait=wait, cancel_futures=True)
        self.worker = self.retiring = self.active = None


def service():
    global _service
    if _service is None or _service.closed:
        _service = PreviewService()
    return _service


def preview_selected():
    target = service()
    requested = set()
    skipped = []
    for node in hou.selectedNodes():
        try:
            requested.add(target.request(node)[0])
        except ValueError:
            skipped.append(node.path())
    if hou.isUIAvailable():
        for pane in hou.ui.paneTabs():
            if pane.type() == hou.paneTabType.NetworkEditor and pane.isCurrentTab():
                pane.setPref("showpreviews", "1")
        hou.ui.setStatusMessage(
            f"Cycles: requested {len(requested)} material preview(s)"
            + ("; choose Cycles materials for: " + ", ".join(skipped) if skipped else ""),
            severity=hou.severityType.Warning if skipped else hou.severityType.Message,
        )


def warning_details():
    issues = list(_service.errors.values()) if _service else []
    message = f"Material previews: {len(issues)} warning(s)"
    details = "\n\n".join(issue["source"] + "\n" + issue["message"] for issue in issues)
    return message, details or "No material preview warnings. Previews yield to active IPR work."


def show_warnings():
    global _warnings_dialog
    PreviewService._host()
    if _warnings_dialog is not None:
        _warnings_dialog.show()
        _warnings_dialog.raise_()
        _warnings_dialog.activateWindow()
        return _warnings_dialog
    from PySide6 import QtCore, QtWidgets

    dialog = QtWidgets.QDialog(hou.qt.mainWindow())
    dialog.setWindowTitle("Cycles Material Previews")
    dialog.setModal(False)
    dialog.setAttribute(QtCore.Qt.WA_DeleteOnClose)
    dialog.resize(650, 360)
    layout = QtWidgets.QVBoxLayout(dialog)
    dialog.summary = QtWidgets.QLabel()
    layout.addWidget(dialog.summary)
    dialog.details = QtWidgets.QPlainTextEdit()
    dialog.details.setReadOnly(True)
    layout.addWidget(dialog.details)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.close)
    layout.addWidget(buttons)

    def refresh():
        message, details = warning_details()
        dialog.summary.setText(message)
        if dialog.details.toPlainText() != details:
            dialog.details.setPlainText(details)

    def forget(*args):
        global _warnings_dialog
        if _warnings_dialog is dialog:
            _warnings_dialog = None

    timer = QtCore.QTimer(dialog)
    timer.timeout.connect(refresh)
    timer.start(250)
    dialog.finished.connect(forget)
    dialog.destroyed.connect(forget)
    _warnings_dialog = dialog
    refresh()
    dialog.show()
    return dialog


def install():
    global _installed
    if _installed or not hou.isUIAvailable():
        return
    _installed = True

    def poll():
        target = service()
        try:
            target.tick()
        except Exception as error:
            # An unhealthy preview service is an explicit local failure. Retire
            # its owned worker and report it; never tear down the artist IPR.
            target.close()
            hou.ui.setStatusMessage("Cycles material previews failed: " + str(error), severity=hou.severityType.Warning)

    hou.ui.addEventLoopCallback(poll)
