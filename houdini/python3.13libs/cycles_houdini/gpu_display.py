# SPDX-License-Identifier: Apache-2.0
"""Legacy import path; the canonical module owns all implementation and state."""

from importlib import import_module as _import_module
import sys as _sys

if not __package__:
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

_implementation = _import_module((__package__ or "cycles_houdini") + ".viewer.gpu_display")


def __getattr__(name):
    return getattr(_implementation, name)


def __dir__():
    return dir(_implementation)


_sys.modules[__name__] = _implementation
