# SPDX-License-Identifier: Apache-2.0
"""Bounded display conversion of immutable frames, without Qt or host access."""

import atexit
import threading
import weakref


_queues = weakref.WeakSet()


class DisplayQueue:
    """One active conversion, one replaceable input and one completed image.

    The token identifies session, generation and display settings. A newer
    progressive frame with the same token must not starve a completed preview.
    A different token retires its result. Input capsules own their immutable
    pixels and color configuration; the converter never reads a render session.
    """

    def __init__(self, convert=None, on_ready=None, *, on_first_ready=None):
        if convert is None:
            from .. import _native

            convert = _native.display_image
        self.convert = convert
        self.on_ready = on_ready
        self.on_first_ready = on_first_ready
        self.first_notified = None
        self.cleanup = getattr(convert, "close", None)
        self.cleanup_called = False
        self.cleanup_error = None
        self.condition = threading.Condition()
        self.source_lock = threading.Lock()
        self.source_config = self.source_seen = None
        self.source_serial = 0
        self.token = self.pending = self.ready = self.last_request = None
        self.closing = False
        self.thread = None
        _queues.add(self)

    def follow(self, source, epoch, settings, describe):
        """Cache GUI choices; source/describe may read only immutable frames.

        A worker subscriber calls source_ready directly, so new pixels enter
        conversion even while Houdini's GUI is busy. Reference sessions use
        the same method from their normal GUI poll. Changing any display
        choice retires older results, including automatic pass-menu changes.
        """
        with self.condition:
            if self.closing:
                return
            values = (source, epoch, dict(settings), describe)
            if self.source_config is None or self.source_config[1:] != values:
                self.source_serial += 1
                self.source_config = (self.source_serial, *values) if source is not None else None
                self.source_seen = None
                self.invalidate(None)
        # The GUI must never wait for a producer reading a frame. A concurrent
        # configuration change is retried by the next pulse or fallback poll.
        self.source_ready(blocking=False)

    def source_ready(self, *, blocking=True):
        if not self.source_lock.acquire(blocking=blocking):
            return
        try:
            notify = self._update_source()
        finally:
            self.source_lock.release()
        if notify is not None:
            notify()

    def _update_source(self):
        with self.condition:
            config = self.source_config
        if config is None:
            return None
        serial, source, epoch, settings, describe = config
        # Fetch outside the display lock: controller callbacks likewise run
        # outside their lock. Serialization prevents a slower, older GUI read
        # from replacing a newer frame accepted by the producer.
        generation, revision, owned = source(0)
        token = (epoch, serial, generation, revision)
        identity = token, owned[2] if owned else None
        with self.condition:
            if self.closing or self.source_config is not config or identity == self.source_seen:
                return None
        info = describe(owned[0]) if owned else None
        with self.condition:
            if self.closing or self.source_config is not config:
                return None
            self.source_seen = identity
            self.invalidate(token)
            if info:
                selected = settings.get("pass", "combined")
                if selected in info["passes"] and selected in info.get("enabled_passes", info["passes"]):
                    options = dict(settings)
                    descriptor = info["pass_metadata"][selected]
                    if not descriptor["color"]:
                        options["channels"] = "rgba"
                    elif options.get("channels") == "alpha" and "A" not in descriptor["channels"]:
                        options["channels"] = "rgb"
                    self.request(token, *owned, options)
            return self.on_ready

    def is_current(self, token):
        with self.condition:
            return not self.closing and self.token == token

    def invalidate(self, token):
        with self.condition:
            if self.token != token:
                self.token = token
                self.pending = self.ready = self.last_request = None

    def request(self, token, frame, generation, sequence, settings):
        with self.condition:
            if self.closing:
                return
            if self.token != token:
                self.token = token
                self.pending = self.ready = self.last_request = None
            identity = (token, sequence)
            if identity == self.last_request:
                return
            self.last_request = identity
            self.pending = (token, frame, generation, sequence, dict(settings))
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="Cycles display conversion", daemon=True)
                self.thread.start()
            self.condition.notify()

    def take(self):
        with self.condition:
            result, self.ready = self.ready, None
            return result

    def run(self):
        try:
            self.run_jobs()
        finally:
            self.cleanup_conversion()

    def cleanup_conversion(self):
        with self.condition:
            if self.cleanup_called:
                return
            self.cleanup_called = True
        if self.cleanup:
            try:
                self.cleanup()
            except Exception as error:
                self.cleanup_error = str(error)

    def run_jobs(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closing or self.pending is not None)
                if self.closing:
                    return
                job, self.pending = self.pending, None
            token, frame, generation, sequence, settings = job
            image = None
            try:
                image = self.convert(frame, settings)
                if image is not None:
                    if "generation" in image:
                        image["render_revision"] = image["generation"]
                    image.update(generation=generation, sequence=sequence)
                result = (token, image, None)
            except Exception as error:
                result = (token, None, str(error))
            # Drop the owned input before waiting again, including on closure.
            job = frame = None
            with self.condition:
                if not self.closing and token == self.token:
                    self.ready = result
                    first = image is not None and result[2] is None and token != self.first_notified
                    priority_notify = self.on_first_ready if first else None
                    if priority_notify is not None:
                        self.first_notified = token
                    notify = self.on_ready if priority_notify is None else None
                else:
                    notify = priority_notify = None
            if priority_notify is not None:
                priority_notify(token)
            elif notify is not None:
                notify()
            result = image = None

    def close(self, wait=False):
        with self.condition:
            self.closing = True
            self.on_ready = self.on_first_ready = None
            self.source_config = self.source_seen = None
            self.pending = self.ready = None
            self.condition.notify()
        if wait and self.thread is not None:
            self.thread.join()
        if self.thread is None:
            self.cleanup_conversion()


def _shutdown():
    queues = list(_queues)
    for queue in queues:
        queue.close()
    for queue in queues:
        queue.close(wait=True)


atexit.register(_shutdown)
