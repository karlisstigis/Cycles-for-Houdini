# SPDX-License-Identifier: Apache-2.0
"""Embedded in a test HIP's hou.session; records calls without changing their order."""

import json as _stop_json
import os as _stop_os
from pathlib import Path as _StopPath
import sys as _stop_sys
import time as _stop_time
import cycles_houdini as _stop_ch
from cycles_houdini import final_render as _stop_final

_stop_destination = _StopPath(__PROFILE_DIRECTORY__)
_stop_log = _stop_destination / ("session-" + str(_stop_os.getpid()) + ".jsonl")
_stop_job = (
    _StopPath(_stop_sys.argv[1]).parent
    if len(_stop_sys.argv) > 1 and _StopPath(_stop_sys.argv[1]).name == "job.json"
    else None
)
_stop_seen = False
_stop_next_progress = 0.0


def _stop_write(event, **data):
    with _stop_log.open("a", encoding="utf-8") as stream:
        stream.write(
            _stop_json.dumps(
                dict(
                    event=event,
                    wall_ns=_stop_time.time_ns(),
                    monotonic_ns=_stop_time.perf_counter_ns(),
                    pid=_stop_os.getpid(),
                    **data,
                )
            )
            + "\n"
        )


def _stop_install_method(name):
    original = getattr(_stop_ch.Session, name)

    def measured(self, *args, **kwargs):
        started = _stop_time.perf_counter()
        if name in ("cancel", "close"):
            _stop_write(name + "_begin", session=id(self))
        try:
            result = original(self, *args, **kwargs)
        except BaseException as error:
            _stop_write(
                name + "_error", session=id(self), seconds=_stop_time.perf_counter() - started, error=str(error)
            )
            raise
        elapsed = _stop_time.perf_counter() - started
        if name in ("apply", "cancel", "close") or elapsed > 0.02:
            fields = dict(session=id(self), seconds=elapsed)
            if name == "cancel":
                fields["diagnostics"] = self.diagnostics()
            if name == "status":
                fields["status"] = {
                    key: result.get(key) for key in ("state", "generation", "message", "current_sample", "progress")
                }
            _stop_write(name + "_end", **fields)
        return result

    setattr(_stop_ch.Session, name, measured)


if not getattr(_stop_ch.Session, "_stop_profile_installed", False):
    if _stop_os.environ.get("CYCLES_HOUDINI_STOP_PROFILE_DETAIL") == "1":
        _stop_original_init = _stop_ch.Session.__init__

        def _stop_profile_init(self, *args, **kwargs):
            kwargs["collect_diagnostics"] = True
            _stop_original_init(self, *args, **kwargs)

        _stop_ch.Session.__init__ = _stop_profile_init
    for _stop_method in ("apply", "status", "frame_info", "cancel", "close"):
        _stop_install_method(_stop_method)
    _stop_ch.Session._stop_profile_installed = True

    _stop_original_notify = _stop_final._notify

    def _stop_notify(event, value):
        global _stop_seen, _stop_next_progress
        if event == "progress":
            now = _stop_time.perf_counter()
            if not _stop_seen and _stop_job and (_stop_job / "stop").exists():
                _stop_seen = True
                _stop_write("stop_detected", requested_wall_ns=(_stop_job / "stop").stat().st_mtime_ns)
            if now >= _stop_next_progress:
                metadata = value[1]
                _stop_write(
                    "progress",
                    status={
                        key: metadata.get(key)
                        for key in ("state", "generation", "message", "current_sample", "progress")
                    },
                )
                _stop_next_progress = now + 0.25
        return _stop_original_notify(event, value)

    _stop_final._notify = _stop_notify
    _stop_write("installed")
