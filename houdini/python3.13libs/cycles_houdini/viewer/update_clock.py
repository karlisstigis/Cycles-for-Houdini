# SPDX-License-Identifier: Apache-2.0
"""Viewer time counted from the artist's action, not from the start of sampling.

The renderer reports how long the current image has been sampling
(``elapsed_seconds``). Before sampling starts the viewer waits for the edit
debounce, the host cook, extraction, the transfer to the renderer process and
the renderer's scene update. This clock starts when the update is triggered
(Render, a parameter edit, a frame change) and shows

    total = loading + render

where *loading* runs from the trigger to the start of the render and *render*
is the renderer's own elapsed time, unchanged. A finished update keeps its
final total. No Houdini or Qt access: the panel feeds it statuses.
"""

from collections import deque
import time

HISTORY = 20
# An update that produced no new render generation (nothing changed) is
# dropped after this long with nothing pending; the previous total stays.
NO_CHANGE_SECONDS = 0.5
# Renderer and viewer read the same monotonic clock; allow for rounding.
_TOLERANCE = 0.05
FINISHED = ("complete", "cancelled", "failed", "closed")
SINCE = {
    "render": "since Render",
    "edit": "since edit",
    "frame": "since frame change",
    "camera": "since camera move",
    "scene": "since update",
}
# Counters copied into the update history (Copy diagnostics).
COUNTS = (
    "object_updates",
    "geometry_conversions",
    "extractions",
    "cache_hits",
    "adapter_full_object_updates",
    "adapter_pose_updates",
    "motion_cooks",
    "coalesced_updates",
)


class Update:
    __slots__ = (
        "kind",
        "triggered_at",
        "base",
        "inferred",
        "host_started",
        "host_seconds",
        "bound_at",
        "loading",
        "render_valid",
        "generation",
        "first_image",
        "parts",
        "counts",
        "outcome",
        "final_total",
        "final_render",
        "triggers",
    )

    def __init__(self, kind, now, base, inferred=False):
        self.kind = kind
        self.triggered_at = now
        self.base = base
        self.inferred = inferred
        self.host_started = None
        self.host_seconds = 0.0
        self.bound_at = None
        self.loading = None
        self.render_valid = False
        self.generation = None
        self.first_image = None
        self.parts = {}
        self.counts = {}
        self.outcome = None
        self.final_total = None
        self.final_render = None
        self.triggers = 1

    @property
    def open(self):
        return self.outcome is None

    @property
    def bound(self):
        return self.bound_at is not None


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


class UpdateClock:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.current = None  # The update being waited for, or the last one shown.
        self.history = deque(maxlen=HISTORY)
        self._requested = None  # Last observed requested generation of the session.
        self._shown = None  # The last update that ended with an image or a stop.

    # Events -----------------------------------------------------------------

    def trigger(self, kind, now=None, *, restart=True):
        """An update was asked for. restart=False keeps an update already waiting."""
        now = self._clock() if now is None else now
        current = self.current
        if current is not None and current.open:
            if not restart:
                return current
            if not current.bound:
                # Coalesced edits: the image will show the latest one.
                current.triggers += 1
                current.kind = kind
                current.triggered_at = now
                current.host_started = None
                current.host_seconds = 0.0
                return current
            self._close(current, "superseded", now, None)
        self.current = Update(kind, now, self._requested)
        return self.current

    def host_capture(self, started, finished):
        """The viewer's own main-thread capture for the waiting update."""
        current = self.current
        if current is not None and current.open and not current.bound:
            if current.host_started is None:
                current.host_started = started
            current.host_seconds += max(0.0, finished - started)

    def rebase(self):
        """The viewer now shows another session: its generations are unrelated."""
        self._requested = None
        current = self.current
        if current is not None and current.open:
            if current.bound:
                self._close(current, "superseded", self._clock(), None)
            else:
                current.base = None

    def observe(self, status, now=None, *, waiting=False, details=None):
        """Advance from one session status. waiting: the viewer still has the update queued."""
        now = self._clock() if now is None else now
        state = status.get("state")
        requested = int(_number(status.get("generation")))
        committed = int(_number(status.get("committed_generation", requested)))
        pending = bool(status.get("pending_update") or status.get("capture_pending"))
        previous, self._requested = self._requested, requested
        current = self.current
        if current is not None and current.open and current.bound and requested > (current.generation or 0):
            # A newer generation without a trigger seen here (another viewer, the
            # registry, a hidden-viewer catch-up): the shown update is replaced.
            # This status's render time belongs to the newer generation.
            self._close(current, "superseded", now, {k: v for k, v in status.items() if k != "elapsed_seconds"})
            current = None
        if (current is None or not current.open) and previous is not None and requested > previous:
            current = self.current = Update("scene", now, previous, inferred=True)
        if current is None or not current.open:
            return
        if not current.bound:
            if state == "closed" or (state in ("failed", "cancelled") and not pending and not waiting):
                self._close(current, state, now, status)
                return
            if waiting or pending:
                if current.base is None and not pending and requested == committed:
                    current.base = requested  # The trigger came before this generation's successor.
                return
            if current.base is not None and requested <= current.base:
                if now - current.triggered_at >= NO_CHANGE_SECONDS:
                    self._drop(current)
                return
            if committed < requested:
                return
            self._bind(current, status, now, details)
        elif current.render_valid and _number(status.get("first_image_seconds")) > 0:
            # A fresh render reports its first image after it started.
            current.first_image = _number(status.get("first_image_seconds"))
        if state in FINISHED and not pending:
            self._close(current, state, now, status)

    # Display ----------------------------------------------------------------

    def view(self, status, now=None):
        """What the status line shows; None without a tracked update (legacy time)."""
        now = self._clock() if now is None else now
        current = self.current
        if current is None:
            return None
        render = _number(status.get("elapsed_seconds"))
        if current.open and current.bound and current.render_valid:
            total = current.loading + render
        elif current.open:
            total = now - current.triggered_at
        else:
            total = current.final_total
            render = current.final_render if current.final_render is not None else render
        loading = current.loading if current.bound else (now - current.triggered_at if current.open else None)
        sampling = current.bound and not (
            current.open
            and status.get("state") == "rendering"
            and not _number(status.get("current_sample"))
            and not _number(status.get("progress"))
        )
        return dict(
            phase="render" if sampling else "loading",
            open=current.open,
            kind=current.kind,
            total=max(0.0, total),
            loading=loading,
            render=render if (current.bound and current.render_valid) or not current.open else None,
            first_image=current.first_image,
            parts=dict(current.parts),
            outcome=current.outcome,
        )

    # Internals --------------------------------------------------------------

    def _bind(self, current, status, now, details):
        elapsed = _number(status.get("elapsed_seconds"))
        since = now - current.triggered_at
        # A reused (unchanged) render keeps reporting the previous image's
        # elapsed time; that is not this update's render time.
        current.render_valid = elapsed <= since + _TOLERANCE and status.get("state") != "cancelled"
        started = now - elapsed if current.render_valid else now
        current.loading = min(since, max(0.0, started - current.triggered_at))
        current.bound_at = now
        current.generation = int(_number(status.get("generation")))
        first = _number(status.get("first_image_seconds"))
        current.first_image = first if current.render_valid and first > 0 else None
        parts = {}
        if current.host_started is not None:
            parts["wait"] = max(0.0, current.host_started - current.triggered_at)
        for key, name in (("cooking_seconds", "cook"), ("extraction_seconds", "extract")):
            if isinstance(status.get(key), (int, float)):
                parts[name] = _number(status.get(key))
        if details is not None:
            try:
                parts.update({k: v for k, v in (details(current.generation) or {}).items() if v is not None})
            except Exception:
                pass  # Diagnostics are optional; the total stays honest.
        current.parts = parts

    def _close(self, current, outcome, now, status):
        status = status or {}
        if current.bound and current.render_valid:
            if "elapsed_seconds" in status:
                current.final_render = _number(status.get("elapsed_seconds"))
            else:
                current.final_render = max(0.0, now - current.bound_at)
            current.final_total = current.loading + current.final_render
        else:
            # Not started, or a reused image without its own render time:
            # the artist waited until this was seen to end.
            current.final_total = max(0.0, now - current.triggered_at)
        current.outcome = outcome
        current.counts = {key: status[key] for key in COUNTS if isinstance(status.get(key), (int, float))}
        self.history.append(record(current))
        self._shown = current

    def _drop(self, current):
        current.outcome = "no change"
        current.final_total = None
        self.history.append(record(current))
        # Nothing was rendered again: keep showing the last real update's total.
        self.current = self._shown


def record(update):
    """A plain dictionary of one finished update (for the diagnostics report)."""
    return dict(
        kind=update.kind,
        inferred=update.inferred,
        triggers=update.triggers,
        outcome=update.outcome,
        generation=update.generation,
        total=update.final_total,
        loading=update.loading,
        first_image=update.first_image,
        render=update.final_render,
        host=update.host_seconds or None,
        parts=dict(update.parts),
        counts=dict(update.counts),
    )


def seconds(value):
    return f"{value:.2f} s"


def tooltip(view, render_seconds):
    """Short timing lines for the status tooltip; render_seconds is today's pure render time."""
    lines = []
    if view is not None:
        from .viewer_timing import duration

        lines.append(f"Total {duration(view['total'])} {SINCE.get(view['kind'], '')}".rstrip())
        if view["loading"] is not None:
            parts = view["parts"]
            # A part longer than the loading time ran while an earlier preview
            # of the same edit (camera, object pose) was already rendering.
            named = " · ".join(
                f"{name} {parts[name]:.2f}"
                for name in ("wait", "cook", "extract", "transfer")
                if name in parts and parts[name] <= view["loading"] + _TOLERANCE
            )
            lines.append(f"Loading {seconds(view['loading'])}" + (f" ({named})" if named else ""))
        if view["first_image"] is not None:
            lines.append(f"First image {seconds(view['first_image'])} after start")
    lines.append(f"Render {seconds(render_seconds)}")
    return "\n".join(lines)
