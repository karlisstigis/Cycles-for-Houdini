# SPDX-License-Identifier: Apache-2.0
"""Focused ADR 0005 edit benchmark on the original four-palette fixture and its ordinary-size control."""

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import random
import statistics
import time

import numpy as np
import cycles_houdini as ch
from native_runtime_identity import verify
from stylesheet_storage_fixture import Fixture


# The reference exercises the uncached construction path in the same binary.
# These controls do not turn that binary into an upstream Cycles build.
CONTROLS = (
    "DISABLE_LIGHT_TREE_CACHE",
    "DISABLE_EMPTY_LIGHT_TREE_SPLIT_SKIP",
    "DISABLE_LIGHT_TREE_EMITTER_VIEWS",
    "DISABLE_LIGHT_TREE_RANGE_UPLOAD",
    "DISABLE_RETAINED_LIGHT_TREE_RANGES",
    "DISABLE_RETAINED_LIGHT_UPLOAD",
    "SERIAL_LIGHT_TREE_FLATTEN",
)


@contextmanager
def session(fixture, device, arm):
    keys = ["CYCLES_HOUDINI_" + name for name in CONTROLS]
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            if arm == "uncached":
                os.environ[key] = "1"
            else:
                os.environ.pop(key, None)
        with ch.Session(
            device=device,
            width=fixture.width,
            height=fixture.height,
            samples=1,
            execution="worker",
            threads=2,
            use_texture_cache=False,
        ) as worker:
            yield worker
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def render(worker, fixture):
    start = time.perf_counter()
    frame = worker.wait(worker.apply(fixture.update(), reuse_if_unchanged=True), timeout=120)
    elapsed = (time.perf_counter() - start) * 1000
    assert worker.status()["state"] == "complete" and frame["complete"]
    fixture.check(frame, fixture.offset)
    return elapsed, {name: np.frombuffer(p["pixels"], np.float32).copy() for name, p in frame["planes"].items()}


def pair_trial(fixture, device, arm):
    fixture.styles(offset=0)
    fixture.obj.parm("tx").set(0)
    with session(fixture, device, arm) as worker:
        render(worker, fixture)
        fixture.obj.parm("tx").set(0.015)
        render(worker, fixture)
        fixture.obj.parm("tx").set(0.03)
        transform, _ = render(worker, fixture)
        fixture.styles(offset=0.03)
        emission, planes = render(worker, fixture)
    return {"transform": transform, "emission": emission}, planes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scene", choices=("ordinary", "regression"), required=True)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    verify()
    fixture = Fixture(args.device, heavy=args.scene == "regression")
    data = dict(
        scene=args.scene,
        device=args.device,
        pairs=[],
        reference="same-binary uncached construction",
        scope="SOP capture through complete one-sample frame, excluding GUI paint",
        runtime=json.loads(os.environ["CYCLES_HOUDINI_TEST_IDENTITY"])["manifest_sha256"],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(args.output)
    # A fresh worker renders the *edited* scene with no incremental history.
    fixture.obj.parm("tx").set(0.03)
    fixture.styles(offset=0.03)
    with session(fixture, args.device, "uncached") as worker:
        _, reference = render(worker, fixture)
    rng = random.Random(20261110)
    for _ in range(args.pairs):
        order = ["uncached", "retained"]
        rng.shuffle(order)
        pair = {"order": order, "max_abs_error": {}}
        for arm in order:
            pair[arm], actual = pair_trial(fixture, args.device, arm)
            assert actual.keys() == reference.keys()
            error = {}
            for name, pixels in actual.items():
                np.testing.assert_allclose(pixels, reference[name], rtol=0, atol=1e-5, equal_nan=False, err_msg=name)
                error[name] = float(np.max(np.abs(pixels - reference[name])))
            pair["max_abs_error"][arm] = error
        data["pairs"].append(pair)
        args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for edit in ("transform", "emission"):
        savings = [p["uncached"][edit] - p["retained"][edit] for p in data["pairs"]]
        data[edit] = {"median_ms_saved": statistics.median(savings), "range_ms_saved": [min(savings), max(savings)]}
        for arm in ("uncached", "retained"):
            ms = sorted(p[arm][edit] for p in data["pairs"])
            data[edit][arm] = dict(median_ms=statistics.median(ms), p95_ms=ms[math.ceil(0.95 * len(ms)) - 1])
    data["result"] = "passed"
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
