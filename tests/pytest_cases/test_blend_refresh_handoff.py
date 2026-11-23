# SPDX-License-Identifier: Apache-2.0
"""A grouped bounds evaluation is adopted once by later render preparation."""

import importlib
import sys
import threading
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_batch_aliases_join_the_same_owned_results(monkeypatch):
    bounds = importlib.import_module("cycles_houdini.blender.blend_bounds")
    package = importlib.import_module("cycles_houdini.blender")
    worker = importlib.import_module("cycles_houdini.blender.blend_worker")
    callbacks, batches, adopted, resident = [], [], [], {}
    entered, release = threading.Event(), threading.Event()
    version = (1, 2, 3, 4)

    def resolve(spec):
        reference, frame, native = spec
        source, selector = reference.replace("$HIP", "/assets").split("#object=")
        key = ("blender.exe", source, "OBJECT", selector, frame, None, "working", native)
        return key, dict(source=source, selector=selector, frame=frame)

    def evaluate(source, executable, requests, directory, **unused):
        batches.append([(request["selector"], request["frame"]) for request in requests])
        entered.set()
        assert release.wait(3)
        return [(dict(source=source, dependency_versions={source: version}), {}) for request in requests]

    def accept(key, raw, directory, launch, epoch):
        assert threading.current_thread() is threading.main_thread()
        adopted.append(key)
        resident[key] = dict(cache_key=key)
        return resident[key]

    assets = SimpleNamespace(
        _resolve_spec=resolve, _hip_event=object(), accept_evaluation=accept, resident_evaluation=resident.get
    )
    monkeypatch.setattr(package, "blender_assets", assets, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "hou",
        SimpleNamespace(hipFile=SimpleNamespace(eventCallbacks=lambda: callbacks, addEventCallback=callbacks.append)),
    )
    monkeypatch.setattr(worker, "evaluate_many", evaluate)
    monkeypatch.setattr(worker, "_epoch", lambda source: 0)
    monkeypatch.setattr(worker, "launch_signature", lambda *unused: "launch")
    monkeypatch.setattr(worker, "_signature", lambda path: version)
    monkeypatch.setattr(
        bounds,
        "evaluated_record",
        lambda manifest, arrays, **unused: dict(dependency_versions=manifest["dependency_versions"], known=True),
    )
    monkeypatch.setattr(bounds, "publication_generation", lambda: (0, 0))
    monkeypatch.setattr(bounds, "_refresh_generation", 0)
    monkeypatch.setattr(bounds, "_refresh_job", None)
    monkeypatch.setattr(bounds, "_refresh_running", None)
    specs = [
        ("$HIP/asset.blend#object=A", 1.25, True),
        ("/assets/asset.blend#object=A", 1.25, True),
        ("/assets/asset.blend#object=B", 2.75, True),
    ]
    job = bounds.refresh_many(specs)
    assert entered.wait(3)
    release.set()
    try:
        first_key, second_key = resolve(specs[0])[0], resolve(specs[2])[0]
        first = bounds.join_evaluation(first_key)
        assert bounds.join_evaluation(resolve(specs[1])[0]) is first
        second = bounds.join_evaluation(second_key)
        assert first is not second
        assert batches == [[("A", 1.25), ("B", 2.75)]]
        assert adopted == [first_key, second_key]
        assert not job.raw and not job.directories
    finally:
        release.set()
        assert job.done.wait(3)
        job.close()
