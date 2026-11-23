# SPDX-License-Identifier: Apache-2.0
"""Measure native presentation with 512 real file-backed OCIO input transforms.

Run with run-hython.ps1 and CYCLES_HOUDINI_TEST_ARTIFACTS set per candidate.
The null tile device exercises the actual presenting caller without GUI/socket
noise. This measures local disk; network-share latency must be qualified there.
"""

import json
import os
import statistics
import time

import hou
import PyOpenColorIO as OCIO
import cycles_houdini as ch
from support.paths import artifacts_root


destination = artifacts_root() / "color-latency"
destination.mkdir(parents=True, exist_ok=True)
original = OCIO.GetCurrentConfig()
original_path = hou.getenv("OCIO")
count = int(os.environ.get("CH_LATENCY_LUT_COUNT", "512"))


def write_lut(path, gain):
    path.write_text(
        f"Version 1\nFrom 0.0 1.0\nLength 2\nComponents 3\n{{\n0 0 0\n{gain} {gain} {gain}\n}}\n",
        encoding="utf-8",
    )


def timed(call):
    started = time.perf_counter()
    value = call()
    return (time.perf_counter() - started) * 1000, value


def summarize(samples):
    ordered = sorted(samples)
    return dict(
        count=len(samples),
        median_ms=statistics.median(samples),
        p95_ms=ordered[int(len(samples) * 0.95)],
        maximum_ms=max(samples),
        samples_ms=samples,
    )


try:
    config = OCIO.Config.CreateFromStream(original.serialize())
    config.setWorkingDir(original.getWorkingDir())
    for index in range(count):
        path = destination / f"input-{index:04}.spi1d"
        # Distinct processors avoid OCIO's content-deduplication merging
        # metadata for byte-identical transforms into a single file entry.
        write_lut(path, 0.5 + index / 2048)
        space = OCIO.ColorSpace(name=f"Latency Input {index:04}")
        space.setTransform(OCIO.FileTransform(src=str(path.resolve())), OCIO.COLORSPACE_DIR_TO_REFERENCE)
        config.addColorSpace(space)
    config_path = destination / "config.ocio"
    config_path.write_text(config.serialize(), encoding="utf-8")
    hou.putenv("OCIO", str(config_path.resolve()))
    hou.Color.reloadOCIO()
    initial_ms, policy = timed(ch.color_policy)
    checked_processor = OCIO.GetCurrentConfig().getProcessor(f"Latency Input {count - 1:04}", OCIO.ROLE_SCENE_LINEAR)
    print(
        "latency config",
        policy["config_path"],
        list(checked_processor.getProcessorMetadata().getFiles()),
        flush=True,
    )
    with ch.Session(device="CPU", width=64, height=64, samples=1, execution="worker") as session:
        session.wait(session.calibration())
        session.open_native_display("null:")
        first_ms, _ = timed(session.present_native)
        tight = [timed(session.present_native)[0] for _ in range(100)]
        paced = []
        for _ in range(100):
            time.sleep(0.04)
            paced.append(timed(session.present_native)[0])
        # Edit the final file, forcing a complete ledger walk on the old path.
        write_lut(destination / f"input-{count - 1:04}.spi1d", 1.25)
        edit_ms, _ = timed(session.present_native)
        time.sleep(1.05)
        delayed_ms, _ = timed(session.present_native)
        # Registry polling owns synchronous disk refresh; report that cost
        # separately so moving the reload off presentation cannot hide it.
        poll_ms, _ = timed(ch.synchronize_color_configuration)
        refreshed_present_ms, _ = timed(session.present_native)
        fresh_ms, after = timed(ch.color_policy)
        assert after["render_policy_id"] != policy["render_policy_id"], "File edit was not observed"
        # An immediate second edit must still be detected by the explicit API,
        # even if the registry poll just checked every dependency.
        write_lut(destination / f"input-{count - 1:04}.spi1d", 1.5)
        explicit_edit_ms, newest = timed(ch.color_policy)
        assert newest["render_policy_id"] != after["render_policy_id"], "Explicit observation was throttled"
        # Byte-identical LUTs must each remain watched after OCIO merges their
        # processors. Editing the second physical file must still refresh.
        write_lut(destination / f"input-{count - 1:04}.spi1d", 0.5)
        duplicate = ch.color_policy()
        write_lut(destination / f"input-{count - 1:04}.spi1d", 1.75)
        assert ch.color_policy()["render_policy_id"] != duplicate["render_policy_id"], "Duplicate LUT was not watched"
        session.close_native_display()
    report = dict(
        result="passed",
        lut_files=count,
        filesystem="local disk",
        tile_device="null:",
        initial_capture_ms=initial_ms,
        first_present_ms=first_ms,
        tight=summarize(tight),
        paced_25_hz=summarize(paced),
        first_present_after_lut_edit_ms=edit_ms,
        present_after_watch_interval_ms=delayed_ms,
        fresh_policy_ms=fresh_ms,
        explicit_poll_after_lut_edit_ms=poll_ms,
        present_after_explicit_refresh_ms=refreshed_present_ms,
        immediate_explicit_edit_ms=explicit_edit_ms,
        duplicate_lut_edit="detected",
        runtime_identity=json.loads(os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY", "{}")),
    )
    (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in ("tight", "paced_25_hz")}))
    print("tight", summarize(tight) | {"samples_ms": "in report"})
    print("paced_25_hz", summarize(paced) | {"samples_ms": "in report"})
finally:
    if original_path is None:
        hou.unsetenv("OCIO")
    else:
        hou.putenv("OCIO", original_path)
    hou.Color.reloadOCIO()
    OCIO.SetCurrentConfig(original)
