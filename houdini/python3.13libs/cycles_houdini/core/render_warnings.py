# SPDX-License-Identifier: Apache-2.0
"""Engine render warnings (codes render.*) for the viewer status line and the final-render log.
The engine reports them as source diagnostics; this module only selects and words them for artists."""

import sys

PREFIX = "render."


def warnings(status):
    """Render warnings in a session status, in engine order: [(code, message, action)]."""
    result = []
    for issue in (status or {}).get("source_diagnostics", ()) or ():
        code = str(issue.get("code", ""))
        if code.startswith(PREFIX) and str(issue.get("severity", "warning")).lower() != "info":
            result.append((code, issue.get("message", ""), issue.get("action", "")))
    return result


def headline(status):
    """Short suffix for the viewer status line, or ''."""
    found = warnings(status)
    return "".join(" · Warning: " + message for _, message, _ in found)


class Reporter:
    """Print each warning once per render (key) to a render log. The interface does not print: its
    viewer shows warnings in the status line."""

    def __init__(self, stream=None, prefix="Cycles warning: "):
        self.stream, self.prefix, self.seen = stream, prefix, set()

    def report(self, key, status, context=""):
        printed = []
        for code, message, action in warnings(status):
            if (key, code) in self.seen:
                continue
            self.seen.add((key, code))
            text = self.prefix + message + (" " + action if action else "") + (f" ({context})" if context else "")
            print(text, file=self.stream or sys.stdout, flush=True)
            printed.append(text)
        return printed

    def report_changes(self, status, context=""):
        """Interactive log use: print a warning when it appears, not again while it persists across edits.
        A warning that disappears from a settled render is re-armed."""
        current = {code for code, _, _ in warnings(status)}
        printed = []
        for code, message, action in warnings(status):
            if code not in self.seen:
                self.seen.add(code)
                text = self.prefix + message + (" " + action if action else "") + (f" ({context})" if context else "")
                print(text, file=self.stream or sys.stdout, flush=True)
                printed.append(text)
        if (status or {}).get("state") == "complete":
            self.seen &= current
        return printed
