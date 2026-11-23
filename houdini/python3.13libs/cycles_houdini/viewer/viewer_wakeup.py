# SPDX-License-Identifier: Apache-2.0
"""Coalesced GUI pulses, with one priority conversion delivery per edit."""

import threading
import weakref

import shiboken6
from PySide6 import QtCore


class _Pulse(QtCore.QEvent):
    kind = QtCore.QEvent.Type(QtCore.QEvent.registerEventType())

    def __init__(self, wakeup, priority):
        super().__init__(self.kind)
        self.wakeup = weakref.ref(wakeup)
        self.priority = priority


class _Relay(QtCore.QObject):
    def __init__(self, application):
        super().__init__(application)
        self.wakeups = weakref.WeakSet()
        application.aboutToQuit.connect(self.close)

    def event(self, event):
        if event.type() != _Pulse.kind:
            return super().event(event)
        wakeup = event.wakeup()
        callback = wakeup.consume(event.priority) if wakeup is not None else None
        if callback is not None and shiboken6.isValid(callback.__self__):
            callback()
        return True

    @QtCore.Slot()
    def close(self):
        for wakeup in list(self.wakeups):
            wakeup.close()


_relay = None


class ViewerWakeup:
    """Post only bounded notifications; pixel conversion stays off the GUI.

    At most one normal and one high event can be queued per subscription.
    Changing edits update the token of an existing high event, rather than
    posting obsolete events. The normal slot remains reserved until Qt consumes
    its event, so later normal notifications can reuse it after an upgrade.
    Callbacks read latest pixels; no event retains an image or receiver widget.
    """

    def __init__(self, callback, token_is_current=None):
        global _relay
        application = QtCore.QCoreApplication.instance()
        if application is None or QtCore.QThread.currentThread() != application.thread():
            raise RuntimeError("Create viewer wakeups on the GUI thread")
        if _relay is None or not shiboken6.isValid(_relay):
            _relay = _Relay(application)
        self.lock = threading.Lock()
        self.closed = False
        self.normal_posted = self.high_posted = False
        self.normal_requested = False
        self.token = None
        self.token_is_current = token_is_current
        self.callback = weakref.WeakMethod(callback)
        self.relay = _relay
        self.relay.wakeups.add(self)
        callback.__self__.destroyed.connect(self.close)

    def notify(self):
        self._post(False, None)

    def notify_first(self, token):
        self._post(True, token)

    @property
    def pending(self):
        with self.lock:
            return self.normal_requested or self.high_posted

    def _post(self, priority, token):
        with self.lock:
            if self.closed:
                return
            if priority:
                self.token = token
                self.normal_requested = False  # New priority covers earlier normal pulses.
                if self.high_posted:
                    return
                self.high_posted = True
            else:
                self.normal_requested = True
                if self.normal_posted or self.high_posted:
                    return
                self.normal_posted = True
            try:
                # Keep physical queued events distinct from logical work.
                # A high event adopts the newest token without re-posting.
                level = (
                    QtCore.Qt.EventPriority.HighEventPriority
                    if priority
                    else QtCore.Qt.EventPriority.NormalEventPriority
                )
                QtCore.QCoreApplication.postEvent(self.relay, _Pulse(self, priority), level.value)
            except RuntimeError:
                self.closed = True
                self.normal_posted = self.high_posted = False
                self.normal_requested = False
                self.callback = self.relay = self.token_is_current = None

    def consume(self, priority=None):
        with self.lock:
            if self.closed:
                return None
            if priority is None:
                priority = self.high_posted
            if priority:
                if not self.high_posted:
                    return None
                self.high_posted = False
                # A later normal update must survive an obsolete high token.
                normal_requested = self.normal_requested
                self.normal_requested = False
                token, validate = self.token, self.token_is_current
            else:
                if not self.normal_posted:
                    return None
                self.normal_posted = False
                # A queued high event will deliver the latest source state.
                # Do not clear the logical normal request before that happens.
                if self.high_posted or not self.normal_requested:
                    return None
                self.normal_requested = False
                normal_requested = True
                token = validate = None
            callback = self.callback()
        # Never take a source/display lock while holding the notification lock.
        if priority and validate is not None and not validate(token) and not normal_requested:
            return None
        return callback

    def close(self, *_):
        with self.lock:
            self.closed = True
            self.normal_posted = self.high_posted = False
            self.normal_requested = False
            self.callback = self.relay = self.token_is_current = None
