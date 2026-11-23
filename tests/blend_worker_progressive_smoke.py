# SPDX-License-Identifier: Apache-2.0
"""Real Blender progressive host consume, budget and error isolation contract."""

import argparse
from support.package_loader import import_product
import json
from pathlib import Path
import threading

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--blender", required=True)
parser.add_argument("--source", required=True, type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
root = Path(__file__).resolve().parents[1]


def module(name):
    return import_product(name)


worker, cache = module("blend_worker"), module("blend_cache")
source = args.source.resolve()
request = dict(source=str(source), selector_type="OBJECT", selector="Triangle", frame=1)
try:
    result = worker.evaluate_many(source, args.blender, [request], args.output, timeout=60)[0]
    assert not isinstance(result, Exception), str(result)
    frame_bytes = sum(value.nbytes for value in result[1].values())
    worker._inflight = cache.InFlightBudget(frame_bytes)
    calls = []

    def consume(index, value):
        assert threading.current_thread() is threading.main_thread()
        calls.append(index)
        if isinstance(value, Exception):
            return value
        manifest, arrays = value
        assert len(manifest["instances"]) == 1
        assert sum(array.nbytes for array in arrays.values()) <= frame_bytes
        return dict(frame=manifest["frame"], arrays=len(arrays))

    requests = [dict(request, frame=index + 1) for index in range(20)]
    requests.insert(7, dict(request, selector="Missing"))
    results = worker.evaluate_many(source, args.blender, requests, args.output, timeout=60, consume=consume)
    assert len(results) == 21
    assert len(calls) == 21, calls
    assert isinstance(results[7], Exception)
    assert all(isinstance(value, dict) for index, value in enumerate(results) if index != 7), results
    assert worker._inflight.used_bytes == 0, worker._inflight.used_bytes
    assert worker._inflight.peak_bytes == frame_bytes, worker._inflight.peak_bytes
    report = dict(
        result="passed",
        frames=20,
        invalid_selectors=1,
        peak_inflight_bytes=worker._inflight.peak_bytes,
        single_frame_bytes=frame_bytes,
        main_thread_consumption=True,
    )
    (args.output / "progressive-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
finally:
    worker.shutdown_all()
    assert not [thread for thread in threading.enumerate() if thread.name.startswith("CH Blender")]
