# SPDX-License-Identifier: Apache-2.0
"""Copy diagnostics: a plain-text snapshot of viewers, sessions and recent updates.

For the "every update was slow until I reopened the file" case: what was open,
which renderer processes lived how long, and how the last updates spent their
time. Read-only and cheap: only cached state is read. No cook, no render, no
renderer request; worker sessions are read through their controller's cached
status (session.status() can advance a pending scene capture).
"""

import json
import os
from pathlib import Path
import platform
import sys
import time

MANIFESTS = ("release-manifest.json", "provenance/build-manifest.json", "build-manifest.json")
MANIFEST_KEYS = ("package_id", "purpose", "source_fingerprint", "engine_upstream_commit", "runtime_fingerprint")


# Collection (Houdini side) ---------------------------------------------------


def _manifest(host_root):
    """A few identity fields of the installed or development build."""
    if not host_root:
        return {}
    root = Path(host_root)
    result = {}
    for base in (root.parent if root.name == "host" else root, root.parent):
        for relative in MANIFESTS:
            path = base / relative
            try:
                with path.open("rb") as stream:
                    data = stream.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    continue
                value = json.loads(data)
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                for key in MANIFEST_KEYS:
                    if isinstance(value.get(key), str) and key not in result:
                        result[key] = value[key]
        if result:
            break
    return result


def build_info():
    import hou

    host_root = os.environ.get("CYCLES_HOUDINI_HOST_ROOT", "")
    result = dict(
        release=os.environ.get("CYCLES_HOUDINI_RELEASE_ID") or "",
        host=Path(host_root).name if host_root else "",
        houdini=hou.applicationVersionString(),
        python=platform.python_version(),
        os=f"{platform.system()} {platform.release()}",
    )
    result.update(_manifest(host_root))
    return result


def process_times(process):
    """(age seconds, CPU seconds) of an owned child process, or (None, None)."""
    if sys.platform != "win32" or process is None:
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        times = kernel.GetProcessTimes
        times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        times.restype = wintypes.BOOL
        values = [ctypes.c_ulonglong() for _ in range(4)]
        if not times(wintypes.HANDLE(int(process._handle)), *(ctypes.byref(v) for v in values)):
            return None, None
        now = ctypes.c_ulonglong()
        kernel.GetSystemTimeAsFileTime(ctypes.byref(now))
        age = (now.value - values[0].value) * 1e-7
        return (age if age >= 0 else None), (values[2].value + values[3].value) * 1e-7
    except Exception:
        return None, None


def _rop_path(node):
    import hou

    if node is None:
        return ""
    try:
        return node.path()
    except hou.Error:
        return "(deleted)"


def _session_status(session):
    """The cached status, without advancing a pending capture."""
    controller = getattr(session, "_controller", None)
    if controller is not None:
        result = controller.status()
        result["capture_pending"] = bool(
            getattr(session, "_active_capture", None) is not None or getattr(session, "_pending_capture", None)
        )
        return result
    return session.status()


STATUS_FIELDS = (
    "state",
    "backend_state",
    "generation",
    "committed_generation",
    "pending_update",
    "capture_pending",
    "update_rejected",
    "renderer_pid",
    "backend_alive",
    "backend_epoch",
    "execution",
    "elapsed_seconds",
    "first_image_seconds",
    "current_sample",
    "render_device_ids",
    "worker_devices",
    "denoiser",
    "denoise_device_id",
    "device_allocation_bytes",
    "owned_geometry_bytes",
    "retired_shader_resources",
    "unchanged_reuses",
    "coalesced_updates",
    "error",
)


def _plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


def collect(panel):
    import hou
    from . import panel as panel_module
    from ..ipr import queued, registry

    errors = []

    def guarded(name, function, default):
        try:
            return function()
        except Exception as error:
            errors.append(f"{name}: {error}")
            return default

    data = dict(time=time.strftime("%Y-%m-%d %H:%M:%S"), errors=errors)
    data["build"] = guarded("build", build_info, {})
    canvas = getattr(panel, "canvas", None)
    if canvas is not None:
        from .viewer_display import describe

        data["display"] = dict(
            mode=getattr(canvas, "display_mode", "classic"), detail=guarded("display", lambda: describe(canvas), "")
        )

    def viewer(view):
        lifetime = getattr(view, "_lifetime", None)
        entry = getattr(view, "entry", None)
        return dict(
            this=view is panel,
            state="released" if view._disposed else (lifetime.state if lifetime else "unknown"),
            rop=_rop_path(view.__dict__.get("_rop_node")),
            session=entry.key if entry else "",
            display=getattr(getattr(view, "canvas", None), "display_mode", ""),
            polling=bool(view.timer.isActive()) if not view._disposed else False,
            auto=bool(view.auto.isChecked()) if not view._disposed else None,
            render_mode=bool(view._render_mode),
            stopped=bool(view._stopped),
            pending=bool(view._pending),
        )

    data["viewers"] = [guarded("viewer", lambda v=v: viewer(v), {}) for v in list(panel_module.viewers)]

    def session(entry):
        status = _session_status(entry.session)
        return dict(
            key=entry.key,
            rop=_rop_path(entry.rop),
            views=entry.views,
            native_ipr=bool(entry.channel),
            paused=entry.paused,
            final_paused=entry.final_paused,
            entry_error=entry.error,
            **{name: _plain(status.get(name)) for name in STATUS_FIELDS if name in status},
        )

    data["sessions"] = [guarded("session", lambda e=e: session(e), {}) for e in registry.entries()]

    def workers():
        result = []
        for controller in list(queued._controllers):
            process = getattr(controller.backend, "process", None) or controller.starting_process
            if process is None:
                result.append(dict(pid=None, alive=False, closed=controller.closed, epoch=controller.epoch))
                continue
            age, cpu = process_times(process)
            result.append(
                dict(
                    pid=process.pid,
                    alive=process.poll() is None,
                    age=age,
                    cpu=cpu,
                    closed=controller.closed,
                    closing=controller.closing,
                    epoch=controller.epoch,
                    starting=controller.backend is None,
                )
            )
        return result

    data["workers"] = guarded("workers", workers, [])

    def polling():
        callbacks = hou.ui.eventLoopCallbacks() if hou.isUIAvailable() else ()
        return dict(
            polling=registry._polling,
            callback_registered=registry.poll in callbacks,
            in_poll=registry._in_poll,
            retire_requests=len(registry._retire_requested),
            local_final=registry.local_final_active(),
            local_final_jobs=len(registry._local_final_jobs),
            retiring_sessions=len(queued._retiring),
            retirement_polling=queued._retirement_polling,
        )

    data["registry"] = guarded("registry", polling, {})
    clock = getattr(panel, "_update_clock", None)
    updates = list(clock.history) if clock is not None else []
    current = clock.current if clock is not None else None
    if current is not None and current.open:
        from .update_clock import record

        open_update = record(current)
        open_update["outcome"] = "in progress"
        updates.append(open_update)
    data["updates"] = updates[-20:]
    return data


# Formatting (pure) -----------------------------------------------------------


def _seconds(value):
    return "-" if value is None else f"{value:.2f}"


def _age(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    return f"{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _yes(value):
    return "yes" if value else "no"


def _mib(value):
    return "-" if not isinstance(value, (int, float)) else f"{value / 2**20:.1f} MiB"


COUNT_LABELS = (
    ("object_updates", "objects"),
    ("adapter_full_object_updates", "full objects"),
    ("adapter_pose_updates", "poses"),
    ("geometry_conversions", "geo conversions"),
    ("extractions", "extractions"),
    ("cache_hits", "cache hits"),
    ("motion_cooks", "motion cooks"),
    ("coalesced_updates", "coalesced"),
)


def format_report(data):
    lines = [f"Cycles for Houdini diagnostics  {data.get('time', '')}"]
    build = data.get("build", {})
    identity = [
        f"release {build['release']}" if build.get("release") else "development build",
        f"host {build['host']}" if build.get("host") else "",
        f"package {build['package_id']}" if build.get("package_id") else "",
        f"source {build['source_fingerprint'][:12]}" if build.get("source_fingerprint") else "",
        f"engine {build['engine_upstream_commit'][:12]}" if build.get("engine_upstream_commit") else "",
        f"Houdini {build['houdini']}" if build.get("houdini") else "",
        f"Python {build['python']}" if build.get("python") else "",
        build.get("os", ""),
    ]
    lines.append("Build: " + " · ".join(part for part in identity if part))
    display = data.get("display")
    if display:
        lines.append(f"Display: {display.get('mode')} ({display.get('detail', '')})")

    viewers = data.get("viewers", [])
    counts = {}
    for view in viewers:
        counts[view.get("state")] = counts.get(view.get("state"), 0) + 1
    summary = ", ".join(f"{count} {state}" for state, count in sorted(counts.items(), key=lambda kv: str(kv[0])))
    lines.append(f"Viewers: {len(viewers)}" + (f" ({summary})" if summary else ""))
    for index, view in enumerate(viewers, 1):
        if not view:
            continue
        lines.append(
            f"  {index}. {'this · ' if view.get('this') else ''}{view.get('state')} · ROP {view.get('rop') or '-'}"
            f" · session {view.get('session') or '-'} · display {view.get('display') or '-'}"
            f" · poll {'on' if view.get('polling') else 'off'} · auto {_yes(view.get('auto'))}"
            f" · render mode {_yes(view.get('render_mode'))} · stopped {_yes(view.get('stopped'))}"
            f" · edit pending {_yes(view.get('pending'))}"
        )

    sessions = data.get("sessions", [])
    lines.append(f"IPR sessions: {len(sessions)}")
    for item in sessions:
        if not item:
            continue
        devices = item.get("render_device_ids") or item.get("worker_devices") or []
        lines.append(
            f"  {item.get('key')} {item.get('rop') or '-'} · views {item.get('views')}"
            f"{' · Houdini IPR viewer' if item.get('native_ipr') else ''}"
            f" · {item.get('state')} (renderer {item.get('backend_state', item.get('state'))})"
            f"{' · paused' if item.get('paused') else ''}"
            f" · generation {item.get('generation')}/{item.get('committed_generation', item.get('generation'))}"
            f" · pending {_yes(item.get('pending_update'))} · capture {_yes(item.get('capture_pending'))}"
        )
        lines.append(
            f"    worker pid {item.get('renderer_pid') or '-'} alive {_yes(item.get('backend_alive'))}"
            f" · {item.get('execution', 'in process')} · devices {', '.join(map(str, devices)) or '-'}"
            f" · denoise {item.get('denoiser') or '-'} {item.get('denoise_device_id') or ''}".rstrip()
        )
        lines.append(
            f"    render {_seconds(item.get('elapsed_seconds'))} s · first image {_seconds(item.get('first_image_seconds'))} s"
            f" · sample {item.get('current_sample')} · device memory {_mib(item.get('device_allocation_bytes'))}"
            f" · geometry {_mib(item.get('owned_geometry_bytes'))} · retired shaders {item.get('retired_shader_resources', '-')}"
            f" · unchanged reuses {item.get('unchanged_reuses', '-')} · coalesced {item.get('coalesced_updates', '-')}"
        )
        for error in (item.get("entry_error"), item.get("error")):
            if error:
                lines.append(f"    error: {error}")

    workers = data.get("workers", [])
    lines.append(f"Renderer processes: {len(workers)}")
    for worker in workers:
        lines.append(
            f"  pid {worker.get('pid') or '-'} · alive {_yes(worker.get('alive'))} · age {_age(worker.get('age'))}"
            f" · CPU {_seconds(worker.get('cpu'))} s · epoch {worker.get('epoch')}"
            f"{' · closing' if worker.get('closing') else ''}{' · closed' if worker.get('closed') else ''}"
            f"{' · starting' if worker.get('starting') else ''}"
        )
    registry = data.get("registry", {})
    if registry:
        lines.append(
            f"Registry: polling {_yes(registry.get('polling'))} (callback {_yes(registry.get('callback_registered'))})"
            f" · retire requests {registry.get('retire_requests')} · retiring sessions {registry.get('retiring_sessions')}"
            f" (polling {_yes(registry.get('retirement_polling'))}) · local final {_yes(registry.get('local_final'))}"
            f" ({registry.get('local_final_jobs')} jobs)"
        )

    updates = data.get("updates", [])
    lines.append(f"Updates (last {len(updates)}, newest last; seconds):")
    for update in updates:
        parts = update.get("parts", {})
        named = " ".join(
            f"{name} {parts[name]:.2f}" for name in ("wait", "cook", "extract", "transfer") if name in parts
        )
        counts = update.get("counts", {})
        numbers = " · ".join(f"{label} {counts[key]}" for key, label in COUNT_LABELS if key in counts)
        triggers = update.get("triggers") or 1
        lines.append(
            f"  {update.get('kind')}{' (seen late)' if update.get('inferred') else ''}"
            f"{f' x{triggers}' if triggers > 1 else ''}"
            f" · {update.get('outcome')} · gen {update.get('generation') if update.get('generation') is not None else '-'}"
            f" · total {_seconds(update.get('total'))} · loading {_seconds(update.get('loading'))}"
            f" · first image {_seconds(update.get('first_image'))} · render {_seconds(update.get('render'))}"
            + (f" · {named}" if named else "")
            + (f" · host capture {_seconds(update.get('host'))}" if update.get("host") else "")
            + (f" · {numbers}" if numbers else "")
        )
    for error in data.get("errors", []):
        lines.append("Not collected: " + error)
    return "\n".join(lines) + "\n"


def copy(panel):
    """The menu action: report to the clipboard, short confirmation in the status line."""
    from PySide6 import QtWidgets

    try:
        text = format_report(collect(panel))
    except Exception as error:
        panel.notify("Could not copy diagnostics: " + str(error))
        return None
    QtWidgets.QApplication.clipboard().setText(text)
    panel.notify("Diagnostics copied")
    return text
