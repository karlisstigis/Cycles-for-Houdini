# SPDX-License-Identifier: Apache-2.0
"""When a Cycles viewer may work: visible, hidden but still open, or closed.

What Houdini 22.0 does (measured by tests/houdini_viewer_lifetime_gui_smoke.py):

- Closing a docked pane tab, a floating panel, or a floating panel's window
  hides the viewer, calls onDeactivateInterface, reparents it and then calls
  onDestroyInterface (which disposes the viewer).
- Changing the pane tab's type only hides the viewer and calls
  onDeactivateInterface. The widget stays alive; onDestroyInterface never
  comes. The old hou.PaneTab no longer holds a Python Panel.
- Switching a Python Panel tab to another interface hides and reparents the
  viewer without destroying it. Switching back shows the same widget again.
- Another tab in front, another desktop, or another pane maximised: hide and
  onDeactivateInterface; show and onActivateInterface when it returns.
- Minimising a floating panel sends a spontaneous hide, but isVisible() stays
  True; only the window's minimised state tells.

- Changing that tab's type back to Python Panel shows the SAME (old) widget
  again; Houdini does not call onCreateInterface.

So a viewer is visible while its image canvas is visible in a window that is
not minimised. A hidden viewer is released once its pane tab is gone or no
longer a Python Panel showing it (the stored tab is re-found among all pane
tabs first). Its canvas being visible always vetoes this. If Houdini shows a
released viewer again, the viewer asks its tab to reload the interface, so
the artist gets a fresh viewer instead of a dead one.
"""

# What a hidden (still open) viewer does with its render. Change after the
# artist decides; tests cover both values.
#   "finish": a render in progress keeps going to completion (the artist may be
#             waiting for the final image on another tab), but scene edits do
#             not restart it while hidden; on show, pending edits update the
#             render once.
#   "pause":  the viewer's own render pauses while hidden and resumes on show,
#             again applying pending edits once. A session shared with another
#             viewer or with Houdini's IPR viewer is never paused.
HIDDEN_RENDER = "finish"
HIDDEN_RENDER_CHOICES = ("finish", "pause")

# A hidden viewer checks that its pane tab still exists after these delays
# (seconds since hiding), then every LIVENESS_SECONDS. One check is two HOM
# calls on the GUI thread; nothing else runs while hidden.
LIVENESS_DELAYS = (0.0, 0.25, 1.0)
LIVENESS_SECONDS = 2.0

VISIBLE, HIDDEN, CLOSED = "visible", "hidden", "closed"


def canvas_visible(canvas):
    """The image is on screen: shown, and its window is not minimised.

    The canvas, not the panel, decides: in image-only fullscreen the canvas
    lives in its own window while the panel underneath may be hidden.
    """
    if not canvas.isVisible():
        return False
    window = canvas.window()
    return window is None or not window.isMinimized()


def pane_tab_holds(pane_tab, python_panel_type, errors=(Exception,)):
    """False once the pane tab can never show this viewer again.

    No pane tab (a viewer created outside a Python Panel) is never closed by
    this check. A Python Panel tab showing another interface keeps the viewer
    alive: Houdini shows the same widget when the tab switches back.
    """
    if pane_tab is None:
        return True
    try:
        return pane_tab.type() == python_panel_type
    except errors:
        return False  # hou.ObjectWasDeleted: the tab was closed.


def find_pane_tab(pane_tabs, widget, python_panel_type, errors=(Exception,)):
    """The Python Panel tab currently showing this widget, or None."""
    for tab in pane_tabs:
        try:
            if tab.type() == python_panel_type and tab.activeInterfaceRootWidget() is widget:
                return tab
        except errors:
            continue  # A tab closed while scanning.
    return None


class Lifetime:
    """Visible/hidden/closed transitions and the hidden liveness schedule."""

    def __init__(self, policy=None):
        policy = HIDDEN_RENDER if policy is None else policy
        if policy not in HIDDEN_RENDER_CHOICES:
            raise ValueError(f"Unknown hidden render policy {policy!r}")
        self.policy = policy
        self.state = VISIBLE
        self.checks = 0

    def update(self, visible):
        """Return "hide", "show" or None for a new visibility observation."""
        if self.state == CLOSED:
            return None
        if visible and self.state == HIDDEN:
            self.state = VISIBLE
            return "show"
        if not visible and self.state == VISIBLE:
            self.state = HIDDEN
            self.checks = 0
            return "hide"
        return None

    def next_check_ms(self):
        """Milliseconds until the next pane tab check while hidden, or None."""
        if self.state != HIDDEN:
            return None
        if self.checks < len(LIVENESS_DELAYS):
            previous = LIVENESS_DELAYS[self.checks - 1] if self.checks else 0.0
            delay = LIVENESS_DELAYS[self.checks] - previous
        else:
            delay = LIVENESS_SECONDS
        self.checks += 1
        return int(round(delay * 1000))

    def close(self):
        self.state = CLOSED
