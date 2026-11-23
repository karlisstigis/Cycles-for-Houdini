# SPDX-License-Identifier: Apache-2.0
"""Development session selector; native artist workflows have a fixed policy."""

from __future__ import annotations


import os


MODES = ("sync", "worker")


def resolve(value=None):
    mode = value if value is not None else os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync")
    if mode not in MODES:
        if mode == "queued":
            raise ValueError("The in-process queued prototype is retired. Use the persistent worker for IPR.")
        raise ValueError("Unknown Cycles execution mode: " + str(mode))
    return mode
