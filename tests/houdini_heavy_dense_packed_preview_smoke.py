# SPDX-License-Identifier: Apache-2.0
"""Heavy shared prototype and 100k placements; full Cycles geometry, box viewport."""

import os
import traceback
from houdini_packed_preview_benchmark import Benchmark

os.environ["CYCLES_HOUDINI_PACKED_PREVIEW_COUNT"] = "100000"
os.environ["CYCLES_HOUDINI_PACKED_PREVIEW_ROWS"] = "721"
os.environ["CYCLES_HOUDINI_PREVIEW_MATERIAL"] = "emission"
os.environ["CYCLES_HOUDINI_PREVIEW_LIGHT_TREE"] = "1"


class HeavyDensePreview(Benchmark):
    def __init__(self):
        super().__init__()
        # Isolate capture/renderer scheduling from drawing 100 billion native
        # viewport triangles. Cycles still consumes the full packed details.
        lod = self.f.nested.parm("viewportlod")
        assert lod is not None and "box" in lod.menuItems()
        lod.set("box")
        self.report["fixture"]["native_viewport_representation"] = "Bounding box of the containing packed primitive"
        self.report["fixture"]["cycles_representation"] = (
            "Full shared prototype, 1,036,800 triangles, 100,000 placements"
        )
        self.report["scope"] = (
            __doc__
            + " Scripted HOM edits and actual Qt paint; physical input and universal latency budgets are not certified."
        )
        self.save()

    def finish(self, error=None):
        if error is None:
            try:
                states = [case["final_status"]["scene_lifecycle"] for case in self.report["cases"]]
                assert len(states) == 20, len(states)
                assert all(state["retired_scenes"] <= 1 and state["retired_snapshots"] <= 1 for state in states), states
                heavy = [state for state in states if state["last_retired_objects"] == self.instance_count]
                assert heavy and all(not state["retirement_on_host"] for state in heavy), states
                closed = self.report["closed"]["scene_lifecycle"]
                assert closed["retired_scenes"] == closed["retired_snapshots"] == 0, closed
                self.report["publication_checks"] = dict(
                    bounded_owners=True,
                    background_heavy_retirements=len(heavy),
                    maximum_publication_seconds=max(state["publication_seconds"] for state in states),
                    closed_owners=closed,
                )
            except Exception:
                error = traceback.format_exc()
        super().finish(error)


benchmark = HeavyDensePreview()
