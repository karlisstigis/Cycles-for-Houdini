# SPDX-License-Identifier: Apache-2.0
"""Compact frame-time display; animation clocks are owned by the render job."""

import math
import re
import time


def duration(seconds):
    seconds = float(seconds or 0)
    ticks = int(max(0, seconds) * 100 + 0.5) if math.isfinite(seconds) else 0
    whole, fraction = divmod(ticks, 100)
    minutes, second = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{second:02}.{fraction:02}" if hours else f"{minute:02}:{second:02}.{fraction:02}"


def text(seconds, previous=None):
    result = "Time: " + duration(seconds)
    if previous is not None:
        result += " · Last: " + duration(previous)
    return result


def frame_seconds(job):
    elapsed = job.get("frame_elapsed_seconds", 0.0)
    started = job.get("frame_started_monotonic")
    # Supported Houdini/Python platforms share the monotonic clock across
    # processes. Keep advancing during a main-thread cook between job reports.
    if started is not None and job["state"] not in ("completed", "cancelled", "failed"):
        elapsed = max(elapsed, time.monotonic() - started)
    return elapsed


def summary(text):
    parts = text.splitlines()[0].split(" · ") if text else []
    return " · ".join(parts[:2] + [part for part in parts[2:] if part.startswith(("Time:", "Last:", "Frame "))])


# What the renderer is doing before its first sample, in the artist's words when the engine says nothing useful.
_WAITING = {
    "": "Waiting for the renderer",
    "Scene update queued": "Waiting for the renderer to take the queued scene update",
}


def loading_stage(message):
    """The stage shown while no sample has arrived (never an empty "Loading")."""
    text = " ".join(str(message or "").split())
    # Engine geometry stages carry an internal resource key: "Updating Mesh / Tessellating sop:13/... 1/1".
    text = re.sub(r"(Tessellating|Displacing|Subdividing) \S*[:/]\S*", r"\1", text)
    if text.startswith("Sample 0/"):
        text = ""
    return _WAITING.get(text, text)


def failure_headline(timing, message):
    """Keep a concise reason in the visible row and leave the original for the tooltip."""
    fragments = str(message or "See diagnostics for details").splitlines()
    reason = fragments[0].strip().removeprefix("Cycles Houdini SDK dependency: ")
    reason = reason or "See diagnostics for details"
    reason = reason.split(". Log: ", 1)[0].split("; log: ", 1)[0].replace(" · ", " - ")
    if len(reason) > 140:
        reason = reason[:137].rstrip() + "…"
    return f"Failed · {reason} · {timing}"


def loading_headline(timing, message):
    """Status row before the first sample, as Blender shows it: the time, then the stage in full.

    The row keeps only the first two " · " parts (summary) and elides at its right end, so at a narrow
    width an added explanation goes first, then the engine's own words; the time always stays.
    """
    return timing + " · " + loading_stage(message).replace(" · ", " - ")
