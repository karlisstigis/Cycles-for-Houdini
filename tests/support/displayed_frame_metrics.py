# SPDX-License-Identifier: Apache-2.0
"""Pure attribution contract for action-to-completed-Qt-paint measurements."""

import math
import statistics


def distribution(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return {"count": 0, "median": None, "p95": None, "maximum": None}
    # Nearest-rank p95; do not imply interpolation precision for small runs.
    return dict(
        count=len(values),
        median=statistics.median(values),
        p95=values[max(0, math.ceil(len(values) * 0.95) - 1)],
        maximum=values[-1],
    )


class ActionPaints:
    """Require a new physical scene image, current session, and display epoch.

    Logical generation alone is insufficient: no-op ACKs can change it without
    rendering, while session replacement resets counters. Epoch fences prevent
    a retained old-session image with coincidentally equal counters from passing.
    A display-option change or repaint alone never qualifies as scene delivery.
    """

    def __init__(self, started, before_session, before, resolution, *, allow_replacement=False):
        self.started = started
        self.before_session = before_session
        self.before = dict(before)
        self.resolution = tuple(resolution)
        self.allow_replacement = allow_replacement
        self.first_visible = None
        self.first_full_resolution_sample = None
        self.rejections = {}

    def reject(self, reason):
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        return False

    def observe(self, now, session, generation, metadata, status, display_epoch, *, visible=True):
        if now < self.started or not visible:
            return self.reject("before_action_or_hidden")
        if metadata.get("display_epoch") != display_epoch:
            return self.reject("retained_display_epoch")
        if generation != metadata.get("generation"):
            return self.reject("signal_metadata_mismatch")
        if status.get("pending_update") or (metadata.get("generation"), metadata.get("render_revision")) != (
            status.get("generation"),
            status.get("render_revision"),
        ):
            return self.reject("previous_or_uncommitted_scene")
        if session == self.before_session:
            if metadata.get("render_revision", 0) <= self.before.get("render_revision", 0):
                return self.reject("retained_physical_revision")
        elif not self.allow_replacement:
            return self.reject("unexpected_session_replacement")
        if (
            metadata.get("samples", 0) < 1
            or min(metadata.get("rendered_width", 0), metadata.get("rendered_height", 0)) <= 0
        ):
            return self.reject("no_rendered_sample")
        event = dict(seconds=now - self.started, session=session, metadata=dict(metadata))
        if self.first_visible is None:
            self.first_visible = event
        if (metadata["rendered_width"], metadata["rendered_height"]) == self.resolution:
            if self.first_full_resolution_sample is None:
                self.first_full_resolution_sample = event
        return True
