# SPDX-License-Identifier: Apache-2.0
"""Cycles viewer lifetime: a closed or hidden viewer must stop its work.

For each way an artist closes or hides the viewer, this records which Python
Panel hooks and Qt events Houdini delivers, and the viewer's work before and
after: 16 ms polls, frame presentations, canvas/status paints, display
conversions requested and worker frame notifications, plus the render
session's state, samples, generation and device allocations.

Closed (pane tab closed, pane tab type changed, floating panel closed, its
window closed): polls, paints and conversions stop and the viewer's session is
released. Hidden (another tab in front, desktop switched, another pane
maximised, floating window minimised): UI work stops, the in-progress render
continues, a scene edit does not restart it while hidden, and showing the
viewer again resumes polling and applies the edit once.

Known Houdini crash, not caused by the lifetime handling: Houdini occasionally dies
with a segmentation fault while panes are rebuilt
(pane tab closed, pane split for a new viewer, desktop switched back). It also
happens with the product files at the previous commit; the fatal stack is in
HOMUI_PaneTab::close -> ~FUSE_PythonPanel -> QWidget::setParent -> mi_malloc,
which points at earlier heap corruption. The control_* cases repeat the same
pane and desktop operations with a plain label panel and crash the same way,
after a first-chance access violation in Houdini's Vulkan viewport
(RV_VKDescriptorSetInfo).
A crash is reported as a crash (exit 139), never retried.

The panel interface is the product's own script, installed under a probe name
with wrappers that log each hook. Writes only the GUI report and one stderr
line; never saves.
"""

from collections import Counter
import ctypes
import json
import os
import re
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

import hou
import shiboken6
from PySide6 import QtCore, QtWidgets
from cycles_houdini.ipr import registry
from cycles_houdini.device_selection import set_devices
from cycles_houdini.ipr.display_queue import DisplayQueue
from cycles_houdini.viewer import panel as viewer_panel

GRACE = 0.4  # Seconds after an action before work is counted.
MEASURE = 2.0
RESTORE = 2.0
WAIT = 30.0
PROBE_INTERFACE = "cycles_lifetime_probe"
CLOSED = ("tab_closed", "tab_type_changed", "floating_closed", "floating_window_closed")
HIDDEN = ("tab_behind", "interface_switched", "desktop_switched", "other_pane_maximised", "floating_minimised")
# Not run by default: the same pane and desktop operations with a plain Qt label
# panel and no Cycles code, to tell Houdini crashes from viewer crashes.
CONTROLS = ("control_desktop_switched", "control_tab_type_changed")
# A viewer must come up alive however its tab is made: an existing tab whose type is
# changed to the viewer, a viewer -> Python Shell -> viewer round trip on one tab, and
# a viewer created while its tab is behind another one.
MOUNTS = ("tab_type_changed_to_viewer", "viewer_round_trip", "created_hidden")
CONTROL_INTERFACE = "cycles_lifetime_control"


def selected_cases():
    """All cases once, or a chosen subset repeated (to chase intermittent failures).

    Set CYCLES_HOUDINI_LIFETIME_CASES="desktop_switched*10,tab_closed" ("all*2" repeats
    every case), or, through
    the runner, pass run-gui-test.ps1 -RunName only__desktop_switched__x10 (the GUI
    report directory carries the run name).
    """
    spec = os.environ.get("CYCLES_HOUDINI_LIFETIME_CASES", "")
    if not spec:
        match = re.match(r"only__([a-z_]+?)__x(\d+)", Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent.name)
        spec = f"{match.group(1)}*{match.group(2)}" if match else ""
    if not spec:
        return list(CLOSED + HIDDEN + MOUNTS)
    cases = []
    for item in spec.split(","):
        name, _, count = item.strip().partition("*")
        if name in ("all", "mounts"):
            cases += list(CLOSED + HIDDEN + MOUNTS if name == "all" else MOUNTS) * int(count or 1)
            continue
        if name not in CLOSED + HIDDEN + CONTROLS + MOUNTS:
            raise ValueError("Unknown lifetime case " + name)
        cases += [name] * int(count or 1)
    return cases


class Log:
    def __init__(self):
        self.events = []
        self.counts = {}
        self.panels = {}
        self.owners = {}
        self.windows = {}

    def add(self, kind, panel_id, **fields):
        self.events.append(dict(t=round(time.monotonic(), 3), kind=kind, panel=panel_id, **fields))

    def count(self, panel_id, name):
        if panel_id is not None:
            self.counts.setdefault(panel_id, Counter())[name] += 1

    def hook(self, name, panel):
        self.add("hook:" + name, id(panel) if panel is not None else None)


log = Log()
module = types.ModuleType("cycles_lifetime_probe_log")
module.hook = log.hook
sys.modules[module.__name__] = module


def owner(widget):
    key = id(widget)
    if key not in log.owners:
        found = widget
        while found is not None and not isinstance(found, viewer_panel.RenderPanel):
            found = found.parentWidget()
        log.owners[key] = id(found) if found is not None else None
    return log.owners[key]


def counted(name, function, resolve):
    def wrapper(*args, **kwargs):
        try:
            log.count(resolve(args[0] if args else None), name)
        except RuntimeError:
            pass
        return function(*args, **kwargs)

    wrapper.__name__ = function.__name__
    return wrapper


# Patch classes before any panel exists so Qt and the workers dispatch the wrappers.
original_init = viewer_panel.RenderPanel.__init__


def tracked_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    panel_id = id(self)
    log.panels[panel_id] = self
    log.owners[id(self._display_queue)] = panel_id
    self.installEventFilter(events)
    self.destroyed.connect(lambda *_: log.add("qt:destroyed", panel_id))
    log.add("created", panel_id)


viewer_panel.RenderPanel.__init__ = tracked_init
viewer_panel.RenderPanel.poll = counted("poll", viewer_panel.RenderPanel.poll, id)
viewer_panel.RenderPanel.present_available = counted("present", viewer_panel.RenderPanel.present_available, id)
viewer_panel.ImageCanvas.paintEvent = counted("canvas_paint", viewer_panel.ImageCanvas.paintEvent, owner)
# Viewer display mode: the default, or display_<mode> in the run name (-RunName x__display_classic).
DISPLAY = re.search(r"display_(gpu|native|classic)", Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"]).parent.name)
if DISPLAY:
    os.environ["CYCLES_HOUDINI_VIEWER_DISPLAY"] = DISPLAY.group(1)
from cycles_houdini.viewer import viewer_display  # noqa: E402

GpuCanvas = viewer_display.gpu_canvas_class()
GpuCanvas.paintGL = counted("canvas_paint", GpuCanvas.paintGL, owner)
viewer_panel.StatusLine.paintEvent = counted("status_paint", viewer_panel.StatusLine.paintEvent, owner)
original_dispose = viewer_panel.RenderPanel.dispose


def logged_dispose(self, *args, **kwargs):
    if not self._disposed:
        log.add("dispose", id(self))
    return original_dispose(self, *args, **kwargs)


viewer_panel.RenderPanel.dispose = logged_dispose
REGISTRY = "registry"
registry.poll = counted("registry_poll", registry.poll, lambda _: REGISTRY)
viewer_panel.RenderPanel.render = counted("render", viewer_panel.RenderPanel.render, id)
DisplayQueue.request = counted("conversion", DisplayQueue.request, lambda q: log.owners.get(id(q)))
DisplayQueue.source_ready = counted("frame_notification", DisplayQueue.source_ready, lambda q: log.owners.get(id(q)))
for name in ("update_lifetime", "check_host"):
    if hasattr(viewer_panel.RenderPanel, name):
        original = getattr(viewer_panel.RenderPanel, name)

        def logged(self, *args, _original=original, _name=name, **kwargs):
            log.add("panel:" + _name, id(self), args=[str(a) for a in args])
            return _original(self, *args, **kwargs)

        setattr(viewer_panel.RenderPanel, name, logged)


class Events(QtCore.QObject):
    PANEL = {
        QtCore.QEvent.Show: "show",
        QtCore.QEvent.Hide: "hide",
        QtCore.QEvent.Close: "close",
        QtCore.QEvent.ParentChange: "parent_change",
        QtCore.QEvent.ParentAboutToChange: "parent_about_to_change",
        QtCore.QEvent.ShowToParent: "show_to_parent",
        QtCore.QEvent.HideToParent: "hide_to_parent",
        QtCore.QEvent.DeferredDelete: "deferred_delete",
    }
    WINDOW = {
        QtCore.QEvent.Show: "show",
        QtCore.QEvent.Hide: "hide",
        QtCore.QEvent.Close: "close",
        QtCore.QEvent.WindowStateChange: "window_state",
        QtCore.QEvent.DeferredDelete: "deferred_delete",
    }

    def eventFilter(self, obj, event):
        try:
            kind = event.type()
            if isinstance(obj, viewer_panel.RenderPanel) and kind in self.PANEL:
                log.add("qt:" + self.PANEL[kind], id(obj), spontaneous=event.spontaneous())
            elif kind in self.WINDOW and id(obj) in log.windows:
                log.add("window:" + self.WINDOW[kind], log.windows[id(obj)])
        except RuntimeError:
            pass
        return False


events = Events()


def process_alive(pid):
    if not pid or os.name != "nt":
        return None
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def install_probe_interface():
    source = Path(hou.getenv("CYCLES_HOUDINI_HOST_ROOT") or "").joinpath("python_panels", "cycles_render.pypanel")
    if not source.is_file():
        source = Path(viewer_panel.__file__).resolve().parents[3] / "python_panels" / "cycles_render.pypanel"
    script = re.search(r"<script><!\[CDATA\[(.*?)\]\]></script>", source.read_text(encoding="utf-8"), re.S).group(1)
    script += """
import sys as _probe_sys
def _probe_wrap(_name):
    _original = globals().get(_name)
    def _hook(*args, **keywords):
        _log = _probe_sys.modules.get("cycles_lifetime_probe_log")
        _before = globals().get("panel")
        _result = _original(*args, **keywords) if _original is not None else None
        if _log is not None:
            _log.hook(_name, _result if _name == "onCreateInterface" else _before)
        return _result
    globals()[_name] = _hook
for _probe_name in ("onCreateInterface", "onDestroyInterface", "onActivateInterface", "onDeactivateInterface"):
    _probe_wrap(_probe_name)
"""
    directory = Path(tempfile.mkdtemp(prefix="cycles_lifetime_"))
    path = directory / "cycles_lifetime_probe.pypanel"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<pythonPanelDocument>\n'
        f'  <interface name="{PROBE_INTERFACE}" label="Cycles Lifetime Probe" icon="MISC_python" '
        'showNetworkNavigationBar="false" help_url="">\n'
        f"    <script><![CDATA[{script}]]></script>\n  </interface>\n</pythonPanelDocument>\n",
        encoding="utf-8",
    )
    hou.pypanel.installFile(str(path))
    control = directory / "cycles_lifetime_control.pypanel"
    control.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<pythonPanelDocument>\n'
        f'  <interface name="{CONTROL_INTERFACE}" label="Lifetime Control" icon="MISC_python" '
        'showNetworkNavigationBar="false" help_url="">\n'
        "    <script><![CDATA[\nfrom PySide6 import QtWidgets\n"
        "def onCreateInterface():\n    return QtWidgets.QLabel('control')\n]]></script>\n"
        "  </interface>\n</pythonPanelDocument>\n",
        encoding="utf-8",
    )
    hou.pypanel.installFile(str(control))
    return str(source)


def progress(*parts):
    # The runner keeps the stderr tail; a crash then still shows the last step and finished cases.
    sys.__stderr__.write("VIEWER_LIFETIME " + " ".join(str(p) for p in parts) + "\n")
    sys.__stderr__.flush()


def sleep(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        yield


def wait_until(predicate, what, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for " + what)
        yield


class Probe:
    def __init__(self):
        self.path = Path(os.environ["CYCLES_HOUDINI_GUI_REPORT"])
        self.device = os.environ.get("CYCLES_HOUDINI_GUI_DEVICE", "CPU")
        self.report = dict(result="running", device=self.device, cases=[], failures=[], notes=[])
        self.report["interface_source"] = install_probe_interface()
        self.interface = hou.pypanel.interfaceByName(PROBE_INTERFACE)
        assert self.interface is not None, "Probe interface was not installed"
        QtWidgets.QApplication.instance().installEventFilter(events)
        self.desktop = hou.ui.curDesktop()
        self.viewer = self.desktop.paneTabOfType(hou.paneTabType.SceneViewer)
        assert self.viewer is not None, "Desktop has no Scene Viewer"
        geo = hou.node("/obj").createNode("geo", "lifetime_subject", run_init_scripts=False)
        sphere = geo.createNode("sphere")
        sphere.parm("type").set("polymesh")
        sphere.setDisplayFlag(True)
        sphere.setRenderFlag(True)
        self.subject = geo
        camera = hou.node("/obj").createNode("cam", "lifetime_camera")
        camera.parmTuple("t").set((0, 0, 5))
        self.rop = hou.node("/out").createNode("cycles_ipr", "lifetime_render")
        set_devices(self.rop, self.device)
        self.rop.setParms(
            dict(
                camera=camera.path(),
                resx=160,
                resy=90,
                ipr_samples=1048576,
                ipr_adaptive_sampling=0,
                ipr_use_denoise=0,
                allowmotionblur=0,
            )
        )
        self.cases = selected_cases()
        self.report["cases_run"] = list(self.cases)
        self.limit = 40 + 25 * len(self.cases)
        self.started = time.monotonic()
        self.finished = False
        self.edit = 0
        self.work = self.run()
        self.driver = QtCore.QTimer()
        self.driver.setInterval(16)
        self.driver.timeout.connect(self.on_drive)
        self.driver.start()

    def on_drive(self):
        if self.finished:
            return
        try:
            if time.monotonic() - self.started > self.limit:
                raise TimeoutError(f"Probe exceeded {self.limit} s")
            next(self.work)
        except StopIteration:
            self.finish()
        except Exception:
            self.finish(traceback.format_exc())

    def run(self):
        for index, case in enumerate(self.cases):
            progress("start", index + 1, "of", len(self.cases), case)
            try:
                if case in CONTROLS:
                    yield from self.run_control(case)
                elif case in MOUNTS:
                    yield from self.run_mount(case)
                else:
                    yield from self.run_case(case)
            except Exception:
                self.report["cases"].append(dict(case=case, error=traceback.format_exc()))
                self.report["failures"].append(case + ": probe error")
            progress("case", json.dumps(self.report["cases"][-1], separators=(",", ":"), default=str))
            progress("cleanup", case)
            yield from self.cleanup()
            self.save()

    # Mounting ---------------------------------------------------------------
    def mount(self, case):
        self.floating = self.other_tab = self.pane = self.other_pane = None
        if case.startswith("floating"):
            self.tab = self.desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel, size=(520, 420))
            self.floating = self.tab.pane().floatingPanel() if hasattr(self.tab.pane(), "floatingPanel") else None
        else:
            self.pane = self.split_host_pane()
            base = self.pane.tabs()[0]
            if case == "tab_closed" or case == "tab_behind":
                # The other tab keeps the pane open when the viewer tab closes.
                self.other_tab = base.setType(hou.paneTabType.PythonShell)
                self.tab = self.pane.createTab(hou.paneTabType.PythonPanel)
            else:
                self.tab = base.setType(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(self.interface)
        if hasattr(self.tab, "showToolbar"):
            self.tab.showToolbar(False)
        yield from wait_until(lambda: self.tab.activeInterfaceRootWidget() is not None, "viewer widget")
        self.panel = self.tab.activeInterfaceRootWidget()
        if case.startswith("floating") and self.floating is None:
            self.floating = self.find_floating()
        yield from wait_until(lambda: self.panel.isVisible(), "viewer visible", 5)
        log.windows[id(self.panel.window())] = id(self.panel)
        self.window = self.panel.window()

    def split_host_pane(self):
        """Split the network editor's pane, not the Scene Viewer's.

        Splitting a Scene Viewer pane first builds another Scene Viewer. Houdini
        22.0.466's Vulkan viewport faults while viewports are rebuilt
        (RV_VKDescriptorSetInfo), and the corrupted heap later crashes pane
        construction or destruction, with or without a Cycles viewer (see the
        control cases). Avoid creating extra viewports the cases do not need.
        """
        editor = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.NetworkEditor)
        return (editor or self.viewer).pane().splitHorizontally()

    def find_floating(self):
        for floating in hou.ui.floatingPanels():
            for pane in floating.paneTabs():
                if pane.type() == hou.paneTabType.PythonPanel and pane.activeInterfaceRootWidget() is self.panel:
                    return floating
        return None

    def start_render(self):
        panel = self.panel
        panel.set_rop(self.rop)
        panel.auto.setChecked(True)
        panel.queue_render()

        def rendering():
            entry = panel.entry
            if entry is None:
                return False
            status = entry.session.status()
            assert status["state"] != "failed", status
            return status["state"] == "rendering" and status.get("current_sample", 0) > 2 and panel._live_generation

        yield from wait_until(rendering, "render to start")

    # Measurement ------------------------------------------------------------
    def counters(self):
        return {k: Counter(v) for k, v in log.counts.items()}

    def session_facts(self, entry):
        if entry is None:
            return None
        status = entry.session.status()
        pid = status.get("renderer_pid")
        return dict(
            state=status.get("state"),
            generation=status.get("generation"),
            current_sample=status.get("current_sample"),
            device_allocation_bytes=status.get("device_allocation_bytes"),
            backend_alive=status.get("backend_alive"),
            renderer_pid=pid,
            renderer_alive=process_alive(pid),
            in_registry=registry.get(entry.key) is entry,
            views=entry.views,
            change_serial=entry.change_serial,
            applied_serial=entry.applied_serial,
            registry_polling=registry._polling,
        )

    def panel_facts(self, panel):
        valid = shiboken6.isValid(panel)
        facts = dict(valid=valid, disposed=panel._disposed)
        if valid:
            try:
                facts.update(
                    timer_active=panel.timer.isActive(),
                    visible=panel.isVisible(),
                    canvas_visible=panel.canvas.isVisible(),
                    region_empty=panel.visibleRegion().isEmpty(),
                    window_minimized=panel.window().isMinimized(),
                )
            except RuntimeError as error:
                facts["error"] = str(error)
        return facts

    def measure(self, seconds, entry, edit_at=None):
        before = self.counters()
        session_before = self.session_facts(entry)
        cpu = time.process_time()
        started = time.monotonic()
        edited = False
        while time.monotonic() - started < seconds:
            if edit_at is not None and not edited and time.monotonic() - started >= edit_at:
                self.edit += 1
                self.subject.parm("tx").set(0.1 * self.edit)
                edited = True
            yield
        elapsed = time.monotonic() - started
        cpu = time.process_time() - cpu
        after = self.counters()
        rates = {}
        for panel_id, counts in after.items():
            delta = counts - before.get(panel_id, Counter())
            if delta:
                rates[str(panel_id)] = {k: round(v / elapsed, 1) for k, v in sorted(delta.items())}
        return dict(
            seconds=round(elapsed, 2),
            houdini_cpu_ms_per_second=round(cpu * 1000.0 / elapsed, 1),
            per_second=rates,
            session_before=session_before,
            session_after=self.session_facts(entry),
        )

    # Cases ------------------------------------------------------------------
    def run_control(self, case):
        """Split a pane, show a plain label Python Panel, then switch desktop or change the tab type."""
        record = dict(case=case)
        self.report["cases"].append(record)
        self.floating = self.other_tab = None
        progress("mount", case)
        self.pane = self.split_host_pane()
        self.tab = self.pane.tabs()[0].setType(hou.paneTabType.PythonPanel)
        self.tab.setActiveInterface(hou.pypanel.interfaceByName(CONTROL_INTERFACE))
        yield from wait_until(lambda: self.tab.activeInterfaceRootWidget() is not None, "control widget")
        yield from sleep(1.0)
        progress("act", case)
        if case == "control_desktop_switched":
            self.act("desktop_switched")
            progress("acted", case)
            yield from sleep(2.4)
            progress("restore", case)
            yield from self.restore("desktop_switched")
        else:
            self.tab = self.tab.setType(hou.paneTabType.PythonShell)
            progress("acted", case)
            yield from sleep(2.4)
        progress("restored", case)
        yield from sleep(0.3)

    def viewer_tab(self, tab):
        """Show the probe viewer on a tab; return the tab and its widget once it exists."""
        tab.setActiveInterface(self.interface)
        yield from wait_until(lambda: tab.activeInterfaceRootWidget() is not None, "viewer widget")
        return tab.activeInterfaceRootWidget()

    def alive(self, panel):
        facts = self.panel_facts(panel)
        facts.update(
            lifetime=panel._lifetime.state,
            host_tab=self.describe_tab(panel._host_tab),
        )
        return facts

    @staticmethod
    def describe_tab(tab):
        if tab is None:
            return None
        try:
            return dict(name=tab.name(), type=str(tab.type()))
        except hou.Error as error:
            return "invalid: " + type(error).__name__

    def run_mount(self, case):
        """The viewer must stay alive and render, whichever way its tab was made."""
        record = dict(case=case)
        self.report["cases"].append(record)
        self.floating = self.other_tab = None
        progress("mount", case)
        self.pane = self.split_host_pane()
        base = self.pane.tabs()[0]
        if case == "tab_type_changed_to_viewer":
            shell = base.setType(hou.paneTabType.PythonShell)
            yield from sleep(0.5)
            self.tab = shell.setType(hou.paneTabType.PythonPanel)
            panel = yield from self.viewer_tab(self.tab)
        elif case == "viewer_round_trip":
            first = yield from self.viewer_tab(base.setType(hou.paneTabType.PythonPanel))
            yield from sleep(0.5)
            shell = self.pane.tabs()[0].setType(hou.paneTabType.PythonShell)
            yield from sleep(1.5)  # Let the first viewer notice its tab is gone.
            record["first_viewer"] = self.alive(first)
            self.check(case, first._disposed, "viewer kept after its tab became a Python Shell", record["first_viewer"])
            self.tab = shell.setType(hou.paneTabType.PythonPanel)
            panel = yield from self.viewer_tab(self.tab)
        else:  # created_hidden
            self.other_tab = base.setType(hou.paneTabType.PythonShell)
            self.tab = self.pane.createTab(hou.paneTabType.PythonPanel)
            self.tab.setActiveInterface(self.interface)
            self.other_tab.setIsCurrentTab()
            yield from sleep(1.5)  # Past the first liveness checks while hidden.
            panel = self.tab.activeInterfaceRootWidget()
            record["while_hidden"] = self.alive(panel) if panel is not None else None
            self.tab.setIsCurrentTab()
            yield from wait_until(lambda: self.tab.activeInterfaceRootWidget() is not None, "viewer widget")
            panel = self.tab.activeInterfaceRootWidget()
        yield from wait_until(lambda: panel._disposed or panel.isVisible(), "viewer visible", 5)
        yield from sleep(1.5)  # Past the liveness schedule (0, 0.25, 1 s).
        # A released viewer that Houdini shows again reloads into a fresh one.
        shown = panel
        panel = self.tab.activeInterfaceRootWidget()
        record["reloaded_interface"] = panel is not shown
        record["shown_events"] = [e["kind"] for e in log.events if e["panel"] == id(shown)][-12:]
        if shown._disposed:
            diagnosis = dict(
                revive_posted=getattr(shown, "_revive_posted", None),
                revive_attempts=getattr(shown, "_revive_attempts", None),
                revive_result=getattr(shown, "_revive_result", None),
                tab_shows_it=self.tab.activeInterfaceRootWidget() is shown,
            )
            try:
                diagnosis["found"] = self.describe_tab(shown._find_host_tab())
                diagnosis["pane_tabs"] = [self.describe_tab(t) for t in hou.ui.paneTabs()]
            except Exception:
                diagnosis["error"] = traceback.format_exc(limit=3)
            record["released_viewer"] = diagnosis
        self.panel = panel
        record["after_mount"] = self.alive(panel)
        ok = not panel._disposed and panel._lifetime.state == "visible"
        self.check(case, ok, "viewer disposed or not visible after mount", record["after_mount"])
        if not ok:
            return
        progress("render", case)
        yield from self.start_render()
        record["rendering"] = yield from self.measure(1.0, panel.entry)
        rates = record["rendering"]["per_second"].get(str(id(panel)), {})
        self.check(case, rates.get("poll", 0) >= 20, "viewer not polling after mount", rates)

    def run_case(self, case):
        record = dict(case=case)
        self.report["cases"].append(record)
        progress("mount", case)
        yield from self.mount(case)
        progress("render", case)
        panel = self.panel
        panel_id = id(panel)
        record["panel"] = panel_id
        record["window_is_main"] = self.window is hou.qt.mainWindow()
        record["display"] = panel.canvas.display_mode
        # GPU canvas: its textures and shaders (None until the first frame is drawn).
        holder = getattr(panel.canvas, "_resources", None)
        mark = len(log.events)
        yield from self.start_render()
        record["mount_events"] = [e["kind"] for e in log.events[:mark] if e["panel"] == panel_id]
        entry = panel.entry
        record["before"] = yield from self.measure(1.0, entry)
        mark = len(log.events)
        progress("act", case)
        self.act(case)
        progress("acted", case)
        yield from sleep(GRACE)
        record["after"] = yield from self.measure(MEASURE, entry, edit_at=0.3 if case in HIDDEN else None)
        after_counts = self.counters()
        record["events"] = (
            [dict(e, t=round(e["t"] - log.events[mark]["t"], 3)) for e in log.events[mark:]]
            if len(log.events) > mark
            else []
        )
        record["panel_after"] = self.panel_facts(panel)
        rates = record["after"]["per_second"].get(str(panel_id), {})
        if case in CLOSED:
            facts = record["after"]["session_after"]
            self.check(case, record["panel_after"]["disposed"], "viewer not disposed", record["panel_after"])
            self.check(case, not rates.get("poll"), "polls continue after close", rates)
            self.check(case, not rates.get("canvas_paint") and not rates.get("status_paint"), "paints", rates)
            self.check(case, not rates.get("conversion") and not rates.get("frame_notification"), "display work", rates)
            self.check(
                case, not facts["in_registry"] and facts["state"] in ("closed", "closing"), "session kept", facts
            )
            self.check(case, not facts["renderer_alive"], "renderer process still running", facts)
            self.check(case, not facts["registry_polling"], "registry host poll still running", facts)
            if holder is not None:
                self.check(case, holder[0] is None, "OpenGL resources kept after close", record["display"])
            return
        before, after = record["after"]["session_before"], record["after"]["session_after"]
        self.check(case, rates.get("poll", 0) <= 2, "polls continue while hidden", rates)
        self.check(case, not rates.get("canvas_paint") and not rates.get("status_paint"), "paints while hidden", rates)
        self.check(case, not rates.get("conversion") and not rates.get("present"), "display work while hidden", rates)
        self.check(case, not rates.get("frame_notification"), "frame notifications while hidden", rates)
        self.check(
            case,
            after["state"] == "rendering" and after["current_sample"] > before["current_sample"],
            "render stopped while hidden",
            after,
        )
        self.check(case, not rates.get("render"), "scene edit restarted the render while hidden", [before, after])
        self.check(case, after["generation"] == before["generation"], "generation changed while hidden", after)
        edited = after["change_serial"] > before["change_serial"]
        record["edit_observed_while_hidden"] = edited
        mark = len(log.events)
        progress("restore", case)
        yield from self.restore(case)
        progress("restored", case)
        yield from sleep(0.3)
        record["restored"] = yield from self.measure(RESTORE, panel.entry)
        record["restore_events"] = (
            [dict(e, t=round(e["t"] - log.events[mark]["t"], 3)) for e in log.events[mark:]]
            if len(log.events) > mark
            else []
        )
        restored = record["restored"]["per_second"].get(str(panel_id), {})
        # The resume poll and the edit's render can fall in the 0.3 s before measuring.
        renders = self.count_since(panel_id, "render", after_counts)
        record["renders_after_show"] = renders
        if not shiboken6.isValid(panel) or panel._disposed:
            self.check(case, False, "viewer was destroyed by the hide action", record["panel_after"])
            return
        facts = record["restored"]["session_after"]
        self.check(case, restored.get("poll", 0) >= 20, "polling did not resume", restored)
        self.check(case, restored.get("present"), "image not presented on show", restored)
        if holder is not None:
            self.check(case, restored.get("canvas_paint"), "canvas not painted on show", restored)
            self.check(case, not panel.canvas.gl_failure, "OpenGL drawing failed", panel.canvas.gl_failure)
            self.check(case, panel.canvas._resources[0] is not None, "no OpenGL resources on show", None)
        if edited:
            self.check(case, renders == 1, "edit made while hidden not applied exactly once on show", renders)
            self.check(case, facts["generation"] > after["generation"], "render not updated on show", facts)
            self.check(case, facts["applied_serial"] == facts["change_serial"], "edit still pending", facts)
        else:
            self.report["notes"].append(case + ": scene edit was not observed while hidden")

    def count_since(self, panel_id, name, snapshot):
        return log.counts.get(panel_id, Counter())[name] - snapshot.get(panel_id, Counter())[name]

    def check(self, case, ok, message, detail):
        if not ok:
            self.report["failures"].append(f"{case}: {message}: {json.dumps(detail, default=str)}")

    def act(self, case):
        if case == "tab_closed":
            self.tab.close()
        elif case == "tab_type_changed":
            self.tab = self.tab.setType(hou.paneTabType.PythonShell)
        elif case == "floating_closed":
            (self.floating or self.find_floating()).close()
        elif case == "floating_window_closed":
            self.window.close()
        elif case == "interface_switched":
            names = sorted(n for n in hou.pypanel.interfaces() if n not in (PROBE_INTERFACE, "cycles_houdini_render"))
            self.report["notes"].append(f"interfaces {names}")
            self.tab.setActiveInterface(hou.pypanel.interfaceByName(names[0]))
        elif case == "tab_behind":
            self.other_tab.setIsCurrentTab()
        elif case == "desktop_switched":
            names = [d.name() for d in hou.ui.desktops()]
            others = [d for d in hou.ui.desktops() if d.name() != self.desktop.name()]
            target = next((d for d in others if d.name() == "Technical"), others[0])
            self.report["notes"].append(f"desktops {names}; switched to {target.name()}")
            target.setAsCurrent()
        elif case == "other_pane_maximised":
            self.viewer.pane().setIsMaximized(True)
        elif case == "floating_minimised":
            self.window.showMinimized()

    def restore(self, case):
        if case == "tab_behind":
            self.tab.setIsCurrentTab()
        elif case == "interface_switched":
            self.tab.setActiveInterface(self.interface)
        elif case == "desktop_switched":
            self.desktop.setAsCurrent()
            yield from sleep(0.5)
            self.viewer = hou.ui.curDesktop().paneTabOfType(hou.paneTabType.SceneViewer)
        elif case == "other_pane_maximised":
            self.viewer.pane().setIsMaximized(False)
        elif case == "floating_minimised":
            self.window.showNormal()
        yield

    def cleanup(self):
        try:
            if self.floating is not None:
                self.floating.close()
        except Exception:
            pass
        try:
            if self.pane is not None:
                # Closing the last tab also removes the split this case added.
                for tab in list(self.pane.tabs()):
                    tab.close()
        except Exception:
            self.report["notes"].append("cleanup: " + traceback.format_exc(limit=2))
        yield from sleep(0.3)
        # Stop anything a missed close left running, so later cases measure only themselves.
        for panel in list(log.panels.values()):
            if shiboken6.isValid(panel) and not panel._disposed:
                self.report["notes"].append(f"cleanup disposed leaked viewer {id(panel)}")
                panel.dispose()
        registry.close_all()
        self.pane = self.floating = None
        yield from sleep(0.3)

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2, default=str), encoding="utf-8")

    def finish(self, error=None):
        if self.finished:
            return
        self.finished = True
        self.driver.stop()
        QtWidgets.QApplication.instance().removeEventFilter(events)
        try:
            for panel in list(log.panels.values()):
                if shiboken6.isValid(panel) and not panel._disposed:
                    panel.dispose()
            registry.close_all()
        except Exception:
            self.report["notes"].append("final cleanup: " + traceback.format_exc(limit=2))
        if error:
            self.report["failures"].append("probe: " + error)
        self.report["result"] = "failed" if self.report["failures"] else "passed"
        self.report["error"] = error
        self.save()
        sys.__stderr__.write(
            "VIEWER_LIFETIME_REPORT " + json.dumps(self.report, separators=(",", ":"), default=str) + "\n"
        )
        sys.__stderr__.flush()
        hou.ui.postEventCallback(lambda: hou.exit(1 if self.report["failures"] else 0, suppress_save_prompt=True))


probe = Probe()
