# SPDX-License-Identifier: Apache-2.0
"""Canonical alias batching exercises the production orchestration without Blender."""

import importlib
import sys
import types

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.fixture
def preparation(monkeypatch):
    callbacks = []
    fake_hou = types.SimpleNamespace(
        hipFile=types.SimpleNamespace(eventCallbacks=lambda: callbacks, addEventCallback=callbacks.append)
    )
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    package = importlib.import_module("cycles_houdini.blender")
    previous = getattr(package, "blender_assets", None)
    assets = importlib.import_module("cycles_houdini.blender.blender_assets")
    bounds = importlib.import_module("cycles_houdini.blender.blend_bounds")
    state = types.SimpleNamespace(
        batches=[],
        prepared=0,
        published=[],
        records=0,
        epoch=0,
        version=1,
        behavior="success",
        config_reads=0,
        fail_bounds=False,
        fail_publish=False,
    )

    def record(entry):
        state.records += 1
        if state.fail_bounds:
            raise ValueError("optional bounds fixture failure")
        return dict(schema_version=1, known=True, selector=entry["selector"])

    def publish(reference, frame, entry, **unused):
        if state.fail_publish and entry["known"]:
            raise ValueError("optional publication fixture failure")
        state.published.append((reference, frame, entry))

    monkeypatch.setattr(bounds, "prepared_record", record)
    monkeypatch.setattr(bounds, "publish_record", publish)

    class StaleEvaluationError(RuntimeError):
        pass

    def config_id():
        state.config_reads += 1
        if state.behavior == "late_stale" and state.config_reads == 1:
            state.epoch += 1
        return "working"

    ocio = types.SimpleNamespace(GetCurrentConfig=lambda: types.SimpleNamespace(getCacheID=config_id))
    monkeypatch.setitem(sys.modules, "PyOpenColorIO", ocio)

    def resolve(reference):
        path, selector = reference.split("#object=")
        return "blender.exe", path.replace("$HIP", "/assets"), "OBJECT", selector, None, "working"

    def signature(paths):
        return tuple((path, (3, state.version)) for path in paths)

    def prepare(manifest, arrays, config):
        state.prepared += 1
        if state.behavior == "conversion_stale" and state.prepared == 1:
            state.version += 1
        return dict(selector=manifest["selector"], material_paths={}, instances=[], diagnostics=[])

    def evaluate(source, executable, requests, temp, *, source_config=None, consume=None):
        state.batches.append(requests)
        if state.behavior == "source_failure":
            raise RuntimeError("source failed")
        if state.behavior == "always_stale":
            raise StaleEvaluationError("source changed")
        results = []
        for ordinal, request in enumerate(requests):
            if state.behavior == "selector_failure" and request["selector"] == "A":
                result = ValueError("selector failed")
            else:
                result = (
                    dict(
                        schema_version=1,
                        selector=request["selector"],
                        materials=[],
                        dependency_versions={source: (3, state.version)},
                    ),
                    {},
                )
            results.append(consume(ordinal, result))
        return results

    worker = types.SimpleNamespace(
        StaleEvaluationError=StaleEvaluationError,
        evaluate_many=evaluate,
        launch_signature=lambda *unused: "launch",
        _epoch=lambda source: state.epoch,
        _signature=lambda path: (3, state.version),
    )
    for name, value in dict(
        _directory=None,
        _bundles={},
        _materials={},
        _miss_reasons=dict.fromkeys(
            ("not_resident", "dependency_changed", "launch_changed", "forced_epoch", "material_missing"), 0
        ),
        _resolve_reference=resolve,
        _signature=signature,
        _prepare_result=prepare,
        _share_textures=lambda *unused: None,
        _hip_event=object(),
        blend_worker=worker,
        hou=fake_hou,
    ).items():
        monkeypatch.setattr(assets, name, value)
    state.call = assets.prepare_prototypes
    state.cache = assets._bundles
    state.miss_reasons = assets._miss_reasons
    try:
        yield state
    finally:
        if assets._directory is not None:
            assets._directory.cleanup()
        if previous is None:
            sys.modules.pop("cycles_houdini.blender.blender_assets", None)
            delattr(package, "blender_assets")


SPECS = [
    ("$HIP/asset.blend#object=A", 1.25, True),
    ("/assets/asset.blend#object=A", 1.25, True),
    ("$HIP/asset.blend#object=A", 1.25, True),
    ("/assets/asset.blend#object=B", 1.25, True),
]


def test_cold_warm_aliases_share_one_entry_and_publish_selected_spellings(preparation):
    state = preparation
    results = state.call(SPECS, publish_bounds=True, bounds_specs={SPECS[0], SPECS[1]})
    assert [len(batch) for batch in state.batches] == [2]
    assert state.prepared == 2 and len(state.cache) == 2
    assert results[0] is results[1] is results[2] and results[0] is not results[3]
    assert state.records == 1
    assert [item[0] for item in state.published] == [SPECS[i][0] for i in (0, 1, 2)]
    again = state.call(SPECS)
    assert len(state.batches) == 1 and all(left is right for left, right in zip(results, again))


@pytest.mark.parametrize("behavior,batches", [("selector_failure", 1), ("source_failure", 1), ("always_stale", 3)])
def test_alias_failures_fan_out_after_source_retry(preparation, behavior, batches):
    state = preparation
    state.behavior = behavior
    results = state.call(SPECS)
    assert len(state.batches) == batches and all(len(batch) == 2 for batch in state.batches)
    assert isinstance(results[0], Exception) and results[0] is results[1] is results[2]
    assert all(result is not None for result in results)
    assert isinstance(results[3], dict) == (behavior == "selector_failure")


@pytest.mark.parametrize("behavior", ["conversion_stale", "late_stale"])
def test_dependency_and_late_epoch_retry_retain_alias_publication(preparation, behavior):
    state = preparation
    state.behavior = behavior
    results = state.call(SPECS, publish_bounds=True, bounds_specs={SPECS[1]})
    assert [len(batch) for batch in state.batches] == [2, 2]
    assert all(isinstance(result, dict) for result in results)
    assert results[0] is results[1] is results[2]
    assert len(state.cache) == 2
    assert [(reference, frame) for reference, frame, record in state.published] == [(SPECS[1][0], 1.25)]
    assert results[0]["epoch"] == state.epoch


def test_frame_and_subdivision_states_do_not_alias(preparation):
    specs = [SPECS[0], (SPECS[1][0], 2.5, True), (SPECS[1][0], 1.25, False)]
    results = preparation.call(specs)
    assert len(preparation.batches[0]) == 3 and len({id(result) for result in results}) == 3


def test_resident_frame_reassignment_and_one_fractional_miss(preparation):
    state = preparation
    reference = SPECS[0][0]
    initial = [(reference, frame, True) for frame in (1.0, 15.0)]
    accepted = state.call(initial)
    for _ in range(3):
        reassigned = state.call(list(reversed(initial)))
        assert reassigned == list(reversed(accepted))
        assert len(state.batches) == 1 and state.prepared == 2
    added = state.call(initial + [(reference, 20.25, True)])
    assert [len(batch) for batch in state.batches] == [2, 1]
    assert state.batches[-1][0]["frame"] == 20.25
    assert added[:2] == accepted
    assert state.call(initial) == accepted and state.prepared == 3


def test_renderer_consumers_release_only_their_own_live_samples(preparation, monkeypatch):
    from cycles_houdini.blender.blend_cache import ByteBudgetCache
    from cycles_houdini.blender import blender_assets as assets

    cache = ByteBudgetCache(1, size=lambda value: 1)
    monkeypatch.setattr(assets, "_bundles", cache)
    monkeypatch.setattr(assets, "_retained", {})
    monkeypatch.setattr(assets, "_consumers", {})
    monkeypatch.setattr(assets, "_trim_prototypes", lambda: None)
    cache["frame1"] = object()
    cache["frame15"] = object()
    assets.retain_consumer("ipr", ["frame1"])
    assets.retain_consumer("final", ["frame15"])
    cache.pin(7, ["frame1", "frame15"])
    assets.release_capture(7)
    assert cache.trim()["evicted"] == []
    assets.release_consumer("ipr")
    assert cache.trim()["evicted"] == ["frame1"]
    assert cache.snapshot()["pinned_entries"] == 1
    assets.release_consumer("final")
    assert cache.snapshot()["pinned_entries"] == 0
    # Releasing live ownership preserves a warm entry while under budget.
    assert list(cache) == ["frame15"]


@pytest.mark.parametrize("change", ["dependency", "forced_reload", "launch", "working_config"])
def test_resident_identity_revalidates_external_revisions(preparation, monkeypatch, change):
    from cycles_houdini.blender import blender_assets as assets

    state = preparation
    spec = SPECS[:1]
    original = state.call(spec)[0]
    if change == "dependency":
        state.version += 1
    elif change == "forced_reload":
        state.epoch += 1
    elif change == "launch":
        monkeypatch.setattr(assets.blend_worker, "launch_signature", lambda *unused: "new-launch")
    else:
        resolve = assets._resolve_reference
        monkeypatch.setattr(assets, "_resolve_reference", lambda value: (*resolve(value)[:-1], "new-working"))
        monkeypatch.setitem(
            sys.modules,
            "PyOpenColorIO",
            types.SimpleNamespace(GetCurrentConfig=lambda: types.SimpleNamespace(getCacheID=lambda: "new-working")),
        )
    revised = state.call(spec)[0]
    assert revised is not original and len(state.batches) == 2
    assert state.call(spec)[0] is revised and len(state.batches) == 2
    reason = dict(
        dependency="dependency_changed",
        forced_reload="forced_epoch",
        launch="launch_changed",
        working_config="not_resident",
    )[change]
    assert state.miss_reasons[reason] == (2 if reason == "not_resident" else 1)
    assert sum(state.miss_reasons.values()) == 2


def test_missing_material_binding_has_a_bounded_reason(preparation, monkeypatch):
    from cycles_houdini.blender import blender_assets as assets

    state = preparation
    original = state.call(SPECS[:1])[0]
    original["material_paths"] = {"material": "/old"}
    assets._materials["material"] = {}
    monkeypatch.setattr(assets, "_material_path", lambda value: "/recreated")
    revised = state.call(SPECS[:1])[0]
    assert revised is not original and state.miss_reasons["material_missing"] == 1
    assert len(state.miss_reasons) == 5


def test_eviction_counter_counts_entries_without_adding_history(preparation, monkeypatch):
    from contextlib import nullcontext
    from cycles_houdini.blender.blend_cache import ByteBudgetCache
    from cycles_houdini.blender import blender_assets as assets

    cache = ByteBudgetCache(1, size=lambda value: 1)
    monkeypatch.setattr(assets, "_bundles", cache)
    monkeypatch.setattr(assets, "_meshes", {})
    monkeypatch.setattr(assets, "_topologies", {})
    monkeypatch.setattr(assets.hou, "undos", types.SimpleNamespace(disabler=nullcontext), raising=False)
    cache["old"] = dict(instances=[], material_paths={})
    cache["new"] = dict(instances=[], material_paths={})
    before = assets._metrics["evicted_prototypes"]
    assets._trim_prototypes()
    assert assets._metrics["evicted_prototypes"] == before + 1
    assets._trim_prototypes()
    assert assets._metrics["evicted_prototypes"] == before + 1
    assert list(cache) == ["new"]


def test_worker_diagnostics_use_a_pool_snapshot_before_sampling(preparation, monkeypatch):
    import threading
    from cycles_houdini.blender import blender_assets as assets

    worker = assets.blend_worker
    lock = threading.RLock()
    workers = {}
    sampled = []

    def snapshot(pid):
        assert not lock._is_owned(), "Chunk sampling must not hold the worker-pool lock"
        sampled.append(pid)
        workers.clear()  # Simulate background refresh retirement during sampling.
        return {}

    for pid in (1, 2):
        workers[pid] = types.SimpleNamespace(
            process=types.SimpleNamespace(pid=pid),
            metrics={},
            chunks=types.SimpleNamespace(snapshot=lambda pid=pid: snapshot(pid)),
        )
    monkeypatch.setattr(worker, "_pool_lock", lock, raising=False)
    monkeypatch.setattr(worker, "_workers", workers, raising=False)
    monkeypatch.setattr(assets, "_bundles", types.SimpleNamespace(snapshot=lambda: {}))
    result = assets.cache_diagnostics(workers=True)
    assert sampled == [1, 2]
    assert [item["pid"] for item in result["workers"]] == [1, 2]


@pytest.mark.parametrize("failure", ["fail_bounds", "fail_publish"])
def test_optional_bounds_failure_is_ephemeral_and_recovers(preparation, failure):
    state = preparation
    setattr(state, failure, True)
    results = state.call(SPECS, publish_bounds=True, bounds_specs={SPECS[1]})
    assert all(isinstance(result, dict) for result in results)
    assert results[0] is results[2] and results[0] is not results[1]
    assert state.prepared == 2 and state.records == 1
    assert all(not record["known"] for _, _, record in state.published)
    assert not results[0]["diagnostics"]
    assert results[1]["diagnostics"][0]["code"] == "blend.bounds_unavailable"
    setattr(state, failure, False)
    recovered = state.call(SPECS, publish_bounds=True, bounds_specs={SPECS[1]})
    assert recovered[0] is recovered[1] is recovered[2]
    assert not recovered[1]["diagnostics"] and recovered[1]["bounds_record"]["known"]
    assert state.published[-1][2]["known"] and len(state.batches) == 1
