# SPDX-License-Identifier: Apache-2.0
"""One viewer/worker: HOM point-style edits, repair and displayed reference pixels."""

import houdini_geometry_key_bindings_gui_smoke as workflow
from point_subject_stylesheet_fixture import Fixture

workflow.Fixture = Fixture


def start():
    workflow.start()


if __name__ == "__main__":
    start()
