# SPDX-License-Identifier: Apache-2.0
"""Negative control for run-gui-test/run-prototype-test; success is incorrect.

This is deliberately excluded from the normal render regression list.
"""

raise RuntimeError("Intentional GUI runner failure probe: report this run as failed")
