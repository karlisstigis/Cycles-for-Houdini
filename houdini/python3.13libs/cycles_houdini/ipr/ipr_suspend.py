# SPDX-License-Identifier: Apache-2.0
"""Host-thread leases for local finals; user pause and Stop retain precedence."""


class LocalFinalSuspension:
    def __init__(self):
        self.tokens = set()
        self.records = {}

    @property
    def active(self):
        return bool(self.tokens)

    def begin(self, entries):
        token = object()
        self.tokens.add(token)
        try:
            for entry in entries:
                if entry.key in self.records:
                    continue
                session = entry.session
                running = session.status()["state"] == "rendering" and not entry.paused
                self.records[entry.key] = (entry, session, running, getattr(entry, "stop_serial", 0))
                if running:
                    session.pause(True)
                    entry.final_paused = True
        except BaseException:
            self.end(token)
            raise
        return token

    def holds(self, entry):
        return entry.key in self.records

    def retain_entry(self, entry):
        """Track a new scene entry while process-owned final jobs remain live."""
        if self.active and entry.key not in self.records:
            self.records[entry.key] = (entry, entry.session, False, getattr(entry, "stop_serial", 0))

    def clear_entries(self):
        """Scene replacement retires renderers; the local job leases outlive it."""
        for entry, _, _, _ in self.records.values():
            entry.final_paused = False
        self.records.clear()

    def end(self, token):
        self.tokens.discard(token)
        if self.tokens:
            return
        records, self.records = self.records, {}
        errors = []
        for entry, session, running, stop_serial in records.values():
            entry.final_paused = False
            if (
                running
                and entry.session is session
                and not entry.paused
                and getattr(entry, "stop_serial", 0) == stop_serial
            ):
                try:
                    # Stop, failed/closed sessions and replacement sessions must
                    # never be resurrected when a local final releases its lease.
                    if session.status()["state"] in ("rendering", "paused"):
                        session.pause(False)
                except Exception as error:
                    errors.append(str(error))
        return errors

    def clear(self):
        self.tokens.clear()
        self.records.clear()
