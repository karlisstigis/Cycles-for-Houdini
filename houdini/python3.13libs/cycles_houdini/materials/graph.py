# SPDX-License-Identifier: Apache-2.0
"""Alias of the public material package API."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module(".", __package__)
